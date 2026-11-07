# -*- coding: utf-8 -*-
from flask import Flask, render_template_string, request, jsonify, send_from_directory, Response
import subprocess
import os
import re
import signal
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
import json
import sys
import shutil
import urllib.request
import urllib.parse
import urllib.error

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
if os.name == "nt" and OUTPUT_DIR == "/output":
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

HISTORY_FILE = os.path.join(OUTPUT_DIR, "recordings.json")
SCHEDULES_FILE = os.path.join(OUTPUT_DIR, "schedules.json")
NOTIFICATIONS_FILE = os.path.join(OUTPUT_DIR, "notifications.json")
M3U_LIST_FILE = os.environ.get("M3U_LIST_FILE", "lista.m3u")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
GLUETUN_CONTROL_URL = os.environ.get("GLUETUN_CONTROL_URL", "http://127.0.0.1:8000").rstrip("/")

SCHEDULE_START_TOLERANCE = 120  # segundos de margen si otra grabación está terminando
IPTV_BUFFER_PADDING_SEC = 35    # segundos extra en programadas para compensar el buffer inicial del servidor IPTV

history = []
scheduled_jobs = []
notifications_list = []
channels_db = []
channels_groups = []
vpn_countries_list = []

state_lock = threading.Lock()
vpn_lock = threading.Lock()
current_proc = None
stop_requested = False

# Caché y estado de Gluetun VPN
vpn_state_cache = {
    "last_fetch": 0.0,
    "available": False,
    "status": "unknown",
    "public_ip": "",
    "ip_country": "",
    "ip_city": "",
    "configured_country": "",
    "default_country": "",
    "provider": "protonvpn",
    "switching": False,
}

# Países destacados con traducción y bandera para mostrar primero en los selectores
FEATURED_VPN_COUNTRIES = [
    {"value": "Spain",          "label": "🇪🇸 España (Spain)"},
    {"value": "Japan",          "label": "🇯🇵 Japón (Japan)"},
    {"value": "Switzerland",    "label": "🇨🇭 Suiza (Switzerland)"},
    {"value": "Netherlands",    "label": "🇳🇱 Países Bajos (Netherlands)"},
    {"value": "United Kingdom", "label": "🇬🇧 Reino Unido (United Kingdom)"},
    {"value": "France",         "label": "🇫🇷 Francia (France)"},
    {"value": "Germany",        "label": "🇩🇪 Alemania (Germany)"},
    {"value": "Italy",          "label": "🇮🇹 Italia (Italy)"},
    {"value": "Portugal",       "label": "🇵🇹 Portugal (Portugal)"},
    {"value": "United States",  "label": "🇺🇸 Estados Unidos (United States)"},
    {"value": "Romania",        "label": "🇷🇴 Rumanía (Romania)"},
    {"value": "Sweden",         "label": "🇸🇪 Suecia (Sweden)"},
    {"value": "Belgium",        "label": "🇧🇪 Bélgica (Belgium)"},
    {"value": "Ireland",        "label": "🇮🇪 Irlanda (Ireland)"},
    {"value": "Poland",         "label": "🇵🇱 Polonia (Poland)"},
    {"value": "Argentina",      "label": "🇦🇷 Argentina"},
    {"value": "Mexico",         "label": "🇲🇽 México"},
]

status = {
    'running': False,
    'pid': None,
    'progress': 'Listo para grabar',
    'start_time': None,
    'duration_requested': None,
    'filename': None,
    'mode': None,
    'vpn_country': None,
    'last_result': None
}

app = Flask(__name__)
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ─────────────────────────────────────────────
#  Gestión de Zona Horaria y Cambios de Hora (DST)
# ─────────────────────────────────────────────
def _last_sunday(year: int, month: int) -> int:
    """Devuelve el día del último domingo del mes (para marzo y octubre, 31 días)."""
    d = datetime(year, month, 31)
    return 31 - ((d.weekday() + 1) % 7)


def _eu_dst_bounds_utc(year: int):
    """
    Regla oficial europea (Directiva 2000/84/CE):
    - Inicio horario verano (CEST, UTC+2): último domingo de marzo a las 01:00 UTC.
    - Fin horario verano (vuelta a CET, UTC+1): último domingo de octubre a las 01:00 UTC.
    """
    mar_sun = _last_sunday(year, 3)
    oct_sun = _last_sunday(year, 10)
    start_utc = datetime(year, 3, mar_sun, 1, 0, 0, tzinfo=timezone.utc)
    end_utc = datetime(year, 10, oct_sun, 1, 0, 0, tzinfo=timezone.utc)
    return start_utc, end_utc


def get_server_zoneinfo():
    """Intenta obtener ZoneInfo para TZ_NAME; si no hay tzdata en el sistema, usa fallback EU."""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TZ_NAME)
        except Exception:
            pass
    return None


def now_utc() -> datetime:
    """Devuelve el instante actual exacto en UTC (inmune a cambios de hora)."""
    return datetime.now(timezone.utc)


def utc_to_local(dt_utc: datetime) -> datetime:
    """Convierte un datetime UTC a la hora local configurada con su offset exacto (+01:00 o +02:00)."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt_utc.astimezone(timezone.utc)

    zi = get_server_zoneinfo()
    if zi is not None:
        local_dt = dt_utc.astimezone(zi)
        offset = local_dt.utcoffset()
        tz_name = local_dt.tzname() or ("CEST" if offset == timedelta(hours=2) else "CET")
        return local_dt.replace(tzinfo=timezone(offset, tz_name))

    start_utc, end_utc = _eu_dst_bounds_utc(dt_utc.year)
    if start_utc <= dt_utc < end_utc:
        tz = timezone(timedelta(hours=2), "CEST")
    else:
        tz = timezone(timedelta(hours=1), "CET")
    return dt_utc.astimezone(tz)


def now_local() -> datetime:
    """Hora local actual con offset explícito (ej. +02:00 en verano, +01:00 en invierno)."""
    return utc_to_local(now_utc())


def resolve_local_datetime(dt_str: str, fold: int = 0) -> datetime:
    """
    Convierte un string ISO-8601 (con o sin offset) en un datetime consciente de zona horaria
    con offset explícito (+02:00 / +01:00).
    """
    dt = datetime.fromisoformat(dt_str.strip())
    if dt.tzinfo is not None:
        return utc_to_local(dt.astimezone(timezone.utc))

    fold = 1 if int(fold) == 1 else 0
    zi = get_server_zoneinfo()
    if zi is not None:
        aware = dt.replace(tzinfo=zi, fold=fold)
        offset = aware.utcoffset()
        tz_name = aware.tzname() or ("CEST" if offset == timedelta(hours=2) else "CET")
        return dt.replace(tzinfo=timezone(offset, tz_name))

    y = dt.year
    mar_sun = _last_sunday(y, 3)
    oct_sun = _last_sunday(y, 10)
    ambig_start = datetime(y, 10, oct_sun, 2, 0, 0)
    ambig_end = datetime(y, 10, oct_sun, 3, 0, 0)
    spring_start = datetime(y, 3, mar_sun, 2, 0, 0)

    if ambig_start <= dt < ambig_end:
        offset_hours = 1 if fold == 1 else 2
    elif spring_start <= dt < ambig_start:
        offset_hours = 2
    else:
        offset_hours = 1

    tz_name = "CEST" if offset_hours == 2 else "CET"
    return dt.replace(tzinfo=timezone(timedelta(hours=offset_hours), tz_name))


def to_utc(dt_or_str) -> datetime:
    """Convierte un datetime o string ISO a datetime en UTC."""
    if isinstance(dt_or_str, str):
        dt = resolve_local_datetime(dt_or_str, fold=0)
    else:
        dt = dt_or_str
    if dt.tzinfo is None:
        dt = resolve_local_datetime(dt.isoformat(), fold=0)
    return dt.astimezone(timezone.utc)


def real_seconds_between(start_dt, end_dt) -> int:
    """Calcula los segundos reales transcurridos entre dos fechas/horas (teniendo en cuenta cambios de hora)."""
    return int(round((to_utc(end_dt) - to_utc(start_dt)).total_seconds()))


# ─────────────────────────────────────────────
#  Sistema de Logs y Notificaciones Telegram
# ─────────────────────────────────────────────
def sys_log(msg):
    timestamp = now_local().strftime('%Y-%m-%d %H:%M:%S %z')
    print(f"[{timestamp}] {msg}", flush=True)


def send_telegram_message(text):
    """Envía un mensaje de Telegram si las credenciales están configuradas."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({'chat_id': TELEGRAM_CHAT_ID, 'text': text}).encode('utf-8')
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=8) as response:
            pass
    except Exception as e:
        sys_log(f"Error enviando notificación de Telegram: {e}")


# ─────────────────────────────────────────────
#  Integración con Gluetun Control Server (VPN)
# ─────────────────────────────────────────────
def load_vpn_countries():
    """Carga la lista de países disponibles en ProtonVPN desde vpn/servers/protonvpn.json o fallback."""
    global vpn_countries_list
    candidates = [
        os.path.join("vpn", "servers", "protonvpn.json"),
        "/gluetun/servers/protonvpn.json",
    ]
    found_countries = set()
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for srv in data.get("servers", []):
                    c = srv.get("country")
                    if c:
                        found_countries.add(c.strip())
                break
            except Exception as e:
                sys_log(f"Aviso leyendo países VPN ({path}): {e}")

    if not found_countries:
        found_countries = {item["value"] for item in FEATURED_VPN_COUNTRIES}

    featured_names = {item["value"] for item in FEATURED_VPN_COUNTRIES}
    other_countries = sorted(c for c in found_countries if c not in featured_names)
    vpn_countries_list = {
        "featured": FEATURED_VPN_COUNTRIES,
        "others": other_countries
    }


def _gluetun_request(method: str, path: str, payload=None, timeout: float = 4.0):
    url = f"{GLUETUN_CONTROL_URL}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw}


def get_gluetun_info(force: bool = False) -> dict:
    """Obtiene el estado actual de la VPN, IP pública y país configurado en Gluetun (con caché de 4s)."""
    now_ts = time.monotonic()
    if not force and (now_ts - vpn_state_cache["last_fetch"] < 4.0) and vpn_state_cache["last_fetch"] > 0:
        return dict(vpn_state_cache)

    try:
        ip_info = _gluetun_request("GET", "/v1/publicip/ip", timeout=2.5)
        vpn_state_cache["available"] = True
        vpn_state_cache["public_ip"] = ip_info.get("public_ip", "") or ""
        vpn_state_cache["ip_country"] = str(ip_info.get("country", "") or "").strip().title()
        vpn_state_cache["ip_city"] = ip_info.get("city", "") or ""
    except Exception:
        vpn_state_cache["available"] = False

    try:
        st_info = _gluetun_request("GET", "/v1/vpn/status", timeout=2.0)
        vpn_state_cache["status"] = st_info.get("status", "running" if vpn_state_cache["public_ip"] else "unknown")
        vpn_state_cache["available"] = True
    except Exception:
        pass

    try:
        settings_info = _gluetun_request("GET", "/v1/vpn/settings", timeout=2.5)
        prov = settings_info.get("provider", {}) or {}
        if prov.get("name"):
            vpn_state_cache["provider"] = prov.get("name")
        sel = prov.get("server_selection", {}) or {}
        countries = sel.get("countries") or []
        if countries:
            formatted_country = str(countries[0]).strip().title()
            vpn_state_cache["configured_country"] = formatted_country
            if not vpn_state_cache["default_country"]:
                vpn_state_cache["default_country"] = formatted_country
        vpn_state_cache["available"] = True
    except Exception:
        pass

    vpn_state_cache["last_fetch"] = now_ts
    return dict(vpn_state_cache)


def switch_gluetun_country(target_country: str, wait_ready: bool = True, timeout_sec: int = 28):
    """
    Cambia el país activo en Gluetun mediante PUT /v1/vpn/settings y espera
    hasta que el túnel VPN esté levantado en el nuevo país.
    """
    target_country = (target_country or "").strip()
    if not target_country:
        return True, "Sin cambio de país requerido"

    with vpn_lock:
        info = get_gluetun_info(force=True)
        current_cfg = info.get("configured_country", "")
        if current_cfg.lower() == target_country.lower() and info.get("public_ip"):
            sys_log(f"VPN ya se encuentra conectada en {target_country} (IP: {info.get('public_ip')})")
            return True, f"VPN activa en {target_country} ({info.get('public_ip')})"

        old_ip = info.get("public_ip", "")
        vpn_state_cache["switching"] = True
        sys_log(f"Solicitando a Gluetun cambio de país VPN -> '{target_country}'...")

        try:
            payload = {
                "provider": {
                    "server_selection": {
                        "countries": [target_country]
                    }
                }
            }
            _gluetun_request("PUT", "/v1/vpn/settings", payload=payload, timeout=8.0)
            vpn_state_cache["configured_country"] = target_country
        except urllib.error.HTTPError as he:
            vpn_state_cache["switching"] = False
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            msg = f"Gluetun rechazó el cambio (HTTP {he.code}): {err_body or he.reason}"
            sys_log(msg)
            return False, msg
        except Exception as e:
            vpn_state_cache["switching"] = False
            msg = f"No se pudo conectar con la API de Gluetun ({GLUETUN_CONTROL_URL}): {e}"
            sys_log(msg)
            return False, msg

        if not wait_ready:
            vpn_state_cache["switching"] = False
            get_gluetun_info(force=True)
            return True, f"Cambiando VPN a {target_country}..."

        # Esperar unos segundos a que Gluetun reinicie el túnel y obtenga nueva IP pública
        start_wait = time.monotonic()
        time.sleep(2.5)
        while time.monotonic() - start_wait < timeout_sec:
            updated = get_gluetun_info(force=True)
            new_ip = updated.get("public_ip", "")
            st = updated.get("status", "")
            if st == "running" and new_ip and (new_ip != old_ip or (time.monotonic() - start_wait) > 6.0):
                vpn_state_cache["switching"] = False
                sys_log(f"VPN conectada con éxito a {target_country} | Nueva IP: {new_ip} ({updated.get('ip_country')})")
                return True, f"Conectado a {target_country} · IP: {new_ip}"
            time.sleep(1.5)

        vpn_state_cache["switching"] = False
        final_info = get_gluetun_info(force=True)
        if final_info.get("public_ip"):
            return True, f"VPN en {target_country} · IP: {final_info.get('public_ip')}"
        return False, f"Tiempo de espera agotado reconectando VPN a {target_country}"


# ─────────────────────────────────────────────
#  Persistencia y Carga de Datos
# ─────────────────────────────────────────────
def load_m3u_list():
    global channels_db, channels_groups
    channels_db = []
    groups_set = set()

    if not os.path.exists(M3U_LIST_FILE):
        sys_log(f"Aviso: No se encontró {M3U_LIST_FILE}. La pestaña M3U estará vacía.")
        channels_groups = []
        return 0

    sys_log(f"Cargando lista M3U: {M3U_LIST_FILE}...")
    try:
        with open(M3U_LIST_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        current_name = ""
        current_group = ""
        current_logo = ""

        group_re = re.compile(r'group-title="([^"]*)"', re.IGNORECASE)
        logo_re = re.compile(r'tvg-logo="([^"]*)"', re.IGNORECASE)

        for line in lines:
            line = line.strip()
            if line.startswith("#EXTINF:"):
                g_match = group_re.search(line)
                l_match = logo_re.search(line)
                current_group = g_match.group(1).strip() if g_match else ""
                current_logo = l_match.group(1).strip() if l_match else ""
                parts = line.split(",")
                if len(parts) > 1:
                    current_name = parts[-1].strip()
            elif line and not line.startswith("#"):
                if current_name:
                    channels_db.append({
                        "name": current_name,
                        "url": line,
                        "group": current_group,
                        "logo": current_logo
                    })
                    if current_group:
                        groups_set.add(current_group)
                    current_name = ""
                    current_group = ""
                    current_logo = ""

        channels_groups = sorted(groups_set, key=lambda s: s.lower())
        sys_log(f"¡Lista cargada con éxito! Total de canales: {len(channels_db)} ({len(channels_groups)} grupos)")
        return len(channels_db)
    except Exception as e:
        sys_log(f"Error al cargar la lista M3U: {str(e)}")
        return 0


def load_history():
    global history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    else:
        history = []


def save_history():
    with state_lock:
        try:
            tmp_file = HISTORY_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, HISTORY_FILE)
        except Exception as e:
            sys_log(f"Error guardando historial: {e}")


def load_schedules():
    global scheduled_jobs
    if os.path.exists(SCHEDULES_FILE):
        try:
            with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
                scheduled_jobs = json.load(f)
            changed = False
            for job in scheduled_jobs:
                if job.get('status') == 'running':
                    job['status'] = 'failed'
                    job['status_reason'] = 'La app se reinició durante la grabación'
                    changed = True
                for key in ('start_time', 'end_time'):
                    val = job.get(key)
                    if val:
                        try:
                            dt = datetime.fromisoformat(val)
                            if dt.tzinfo is None:
                                job[key] = resolve_local_datetime(val, fold=0).isoformat()
                                changed = True
                        except Exception:
                            pass
            if changed:
                save_schedules()
        except Exception:
            scheduled_jobs = []
    else:
        scheduled_jobs = []


def save_schedules():
    with state_lock:
        try:
            tmp_file = SCHEDULES_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(scheduled_jobs, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, SCHEDULES_FILE)
        except Exception as e:
            sys_log(f"Error guardando programaciones: {e}")


def load_notifications():
    global notifications_list
    if os.path.exists(NOTIFICATIONS_FILE):
        try:
            with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
                notifications_list = json.load(f)
            if not isinstance(notifications_list, list):
                notifications_list = []
        except Exception:
            notifications_list = []
    else:
        notifications_list = []


def save_notifications():
    with state_lock:
        try:
            tmp_file = NOTIFICATIONS_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(notifications_list[-120:], f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, NOTIFICATIONS_FILE)
        except Exception as e:
            sys_log(f"Error guardando notificaciones: {e}")


def emit_app_notification(title: str, body: str, event_type: str = "info", filename: str = ""):
    """
    Registra una notificación para la App Android (unificando eventos de PC y móvil)
    sin interferir con las notificaciones opcionales de Telegram.
    """
    global notifications_list
    with state_lock:
        next_id = (max((int(item.get("id", 0)) for item in notifications_list), default=0) + 1)
        event = {
            "id": next_id,
            "timestamp": now_local().isoformat(),
            "title": title,
            "body": body,
            "event_type": event_type,
            "filename": filename or "",
        }
        notifications_list.append(event)
        if len(notifications_list) > 120:
            notifications_list = notifications_list[-120:]
    save_notifications()
    return event


# ─────────────────────────────────────────────
#  Utilidades
# ─────────────────────────────────────────────
def human_size(num):
    try:
        num = float(num)
    except Exception:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def hhmmss_to_seconds(hhmmss: str) -> int:
    try:
        parts = hhmmss.strip().split(':')
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + int(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + int(s)
        return int(parts[0])
    except Exception:
        return 0


def seconds_to_hhmmss(secs: int) -> str:
    secs = max(0, int(secs))
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def sanitize_basename(name: str, format_ext: str) -> str:
    name = os.path.basename(name.strip())
    if name.lower().endswith("." + format_ext.lower()):
        name = name[:-(len(format_ext) + 1)]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' ._')
    if not name:
        name = f"grabacion_{now_local().strftime('%Y%m%d_%H%M%S')}"
    return name


def ensure_unique_filename(basename: str, format_ext: str) -> str:
    candidate = f"{basename}.{format_ext}"
    pending_names = {
        j.get('filename') for j in scheduled_jobs
        if j.get('status') in ('pending', 'running')
    }
    counter = 2
    while os.path.exists(os.path.join(OUTPUT_DIR, candidate)) or candidate in pending_names:
        candidate = f"{basename}_{counter}.{format_ext}"
        counter += 1
    return candidate


def get_media_duration_seconds(path: str) -> int:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=OUTPUT_DIR, timeout=15
        )
        val = result.stdout.strip()
        if not val or val == "N/A":
            return 0
        return max(0, int(float(val)))
    except Exception:
        return 0


# ─────────────────────────────────────────────
#  Core de Grabación (FFmpeg + Resiliencia VPN)
# ─────────────────────────────────────────────
def _build_ffmpeg_cmd(url, duration_str, filepath, format_ext):
    format_map = {
        'mp4': 'mp4', 'mkv': 'matroska', 'ts': 'mpegts',
        'avi': 'avi', 'webm': 'webm'
    }
    fformat = format_map.get(format_ext, 'mp4')

    cmd = ['ffmpeg', '-y', '-nostdin', '-hide_banner']

    if url.lower().startswith(('http://', 'https://')):
        cmd.extend([
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '10',
            '-rw_timeout', '20000000'
        ])

    cmd.extend([
        '-i', url,
        '-map', '0:v:0?',
        '-map', '0:a?'
    ])

    if format_ext in ('mkv', 'ts'):
        cmd.extend(['-map', '0:s?'])

    cmd.extend([
        '-dn',
        '-c', 'copy',
        '-t', duration_str,
        '-f', fformat,
        filepath
    ])
    return cmd


def _run_ffmpeg_process(cmd, requested_sec, prefix=""):
    global current_proc, status
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=OUTPUT_DIR, text=True, bufsize=1, errors='replace'
    )
    current_proc = proc
    status['pid'] = proc.pid

    proc_start_monotonic = time.monotonic()
    max_allowed_sec = max(30, requested_sec + 90)
    timed_out = threading.Event()

    def _watchdog():
        while proc.poll() is None:
            time.sleep(2)
            if time.monotonic() - proc_start_monotonic > max_allowed_sec:
                timed_out.set()
                sys_log(f"Watchdog: ffmpeg superó el tiempo máximo ({max_allowed_sec}s). Cerrando proceso...")
                try:
                    proc.terminate()
                    time.sleep(5)
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                break

    threading.Thread(target=_watchdog, daemon=True).start()

    for line in proc.stdout:
        line = line.strip()
        if line:
            clean_line = line[-85:]
            status['progress'] = f"{prefix}{clean_line}" if prefix else clean_line

    proc.wait()
    current_proc = None
    return proc, timed_out.is_set()


def _merge_segments_lossless(segment_files, final_filepath, format_ext):
    valid_segments = [p for p in segment_files if os.path.exists(p) and os.path.getsize(p) > 0]
    if not valid_segments:
        return False
    if len(valid_segments) == 1:
        if valid_segments[0] != final_filepath:
            os.replace(valid_segments[0], final_filepath)
        return True

    concat_list_path = final_filepath + ".concat.txt"
    temp_merged = final_filepath + f".merged.{format_ext}"
    try:
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for seg in valid_segments:
                escaped = os.path.abspath(seg).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        cmd = [
            "ffmpeg", "-y", "-nostdin", "-hide_banner",
            "-f", "concat", "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            temp_merged
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=OUTPUT_DIR, timeout=120)
        if res.returncode == 0 and os.path.exists(temp_merged) and os.path.getsize(temp_merged) > 0:
            os.replace(temp_merged, final_filepath)
            for seg in valid_segments:
                if seg != final_filepath and os.path.exists(seg):
                    try:
                        os.remove(seg)
                    except Exception:
                        pass
            return True
        else:
            largest = max(valid_segments, key=lambda p: os.path.getsize(p))
            if largest != final_filepath:
                os.replace(largest, final_filepath)
            return False
    except Exception as e:
        sys_log(f"Error uniendo segmentos: {e}")
        return False
    finally:
        for p in (concat_list_path, temp_merged):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def _evaluate_recording(returncodes, filepath, duration_str, was_stopped=False):
    filesize_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    requested_sec = hhmmss_to_seconds(duration_str)
    media_duration_sec = get_media_duration_seconds(filepath)
    if media_duration_sec == 0 and filesize_bytes > 0 and was_stopped:
        media_duration_sec = 0

    ok_size = filesize_bytes > 0

    if was_stopped and ok_size:
        return "stopped", ["Detenida manualmente por el usuario"], filesize_bytes, media_duration_sec

    ok_duration = requested_sec > 0 and media_duration_sec >= requested_sec * 0.85
    last_rc = returncodes[-1] if returncodes else -1
    status_rec = "ok" if (ok_duration and ok_size) else "failed"

    reasons = []
    if last_rc != 0 and not ok_duration:
        reasons.append(f"ffmpeg_return={last_rc}")
    if not ok_duration:
        reasons.append(f"duración grabada ({seconds_to_hhmmss(media_duration_sec)}) < 85% de {duration_str}")
    if not ok_size:
        reasons.append("archivo vacío (0 B)")
    return status_rec, reasons, filesize_bytes, media_duration_sec


def _prepare_recording_vpn(target_country: str):
    """
    Si la grabación pide un país específico, cambia Gluetun a ese país y devuelve
    el país anterior para poder restaurarlo al terminar.
    """
    target_country = (target_country or "").strip()
    info = get_gluetun_info(force=True)
    previous_country = info.get("configured_country") or info.get("default_country") or ""

    if not target_country:
        return previous_country, False

    if previous_country.lower() == target_country.lower() and info.get("public_ip"):
        return previous_country, False

    status['progress'] = f"🌍 Conectando VPN a {target_country}..."
    ok, msg = switch_gluetun_country(target_country, wait_ready=True, timeout_sec=28)
    if not ok:
        sys_log(f"Aviso cambio VPN a {target_country}: {msg}")
    return previous_country, True


def _restore_recording_vpn(previous_country: str, did_switch: bool, restore_requested: bool):
    if not did_switch or not restore_requested or not previous_country:
        return
    # No restaurar si hay otra grabación programada a punto de arrancar en los próximos 60s
    current_u = now_utc()
    for j in scheduled_jobs:
        if j.get('status') == 'pending':
            try:
                if 0 <= (to_utc(j['start_time']) - current_u).total_seconds() <= 60:
                    return
            except Exception:
                pass
    sys_log(f"Restaurando país VPN anterior: {previous_country}...")
    status['progress'] = f"🌍 Restaurando VPN a {previous_country}..."
    switch_gluetun_country(previous_country, wait_ready=True, timeout_sec=25)


# ─────────────────────────────────────────────
#  Grabación Inmediata y Programada
# ─────────────────────────────────────────────
def run_ffmpeg(url, duration, filename, format_ext, filepath, vpn_country="", restore_vpn=True):
    global status, history, stop_requested
    stop_requested = False
    status['running'] = True
    status['mode'] = 'immediate'
    status['filename'] = filename
    status['vpn_country'] = vpn_country or None
    status['progress'] = "Preparando grabación en directo..."

    previous_country = ""
    did_switch_vpn = False

    try:
        if vpn_country:
            previous_country, did_switch_vpn = _prepare_recording_vpn(vpn_country)

        active_vpn_info = get_gluetun_info(force=False)
        active_country_label = vpn_country or active_vpn_info.get("configured_country") or active_vpn_info.get("ip_country") or ""

        start_dt = now_local()
        start_utc = start_dt.astimezone(timezone.utc)
        requested_sec = hhmmss_to_seconds(duration)
        target_end_utc = start_utc + timedelta(seconds=requested_sec)

        status['start_time'] = start_dt.isoformat()
        status['duration_requested'] = requested_sec
        status['progress'] = "Iniciando grabación en directo..."

        sys_log(f"Iniciando grabación INMEDIATA ({filename}) | Duración: {duration} ({requested_sec}s) | VPN: {active_country_label or 'Actual'}")
        emit_app_notification(
            title="🔴 Grabación en directo iniciada",
            body=f"{filename} · Duración: {duration}" + (f" · 🌍 {active_country_label}" if active_country_label else ""),
            event_type="recording_started",
            filename=filename
        )

        segment_files = []
        returncodes = []
        attempt = 0
        accumulated_media_sec = 0

        while not stop_requested:
            remaining_media_sec = requested_sec - accumulated_media_sec
            remaining_wall_sec = int(round((target_end_utc - now_utc()).total_seconds()))
            if remaining_media_sec <= 3 or remaining_wall_sec <= 3 or attempt >= 8:
                break

            attempt += 1
            seg_path = filepath if attempt == 1 else f"{filepath}.part{attempt}.{format_ext}"
            seg_dur_str = seconds_to_hhmmss(remaining_media_sec)
            cmd = _build_ffmpeg_cmd(url, seg_dur_str, seg_path, format_ext)

            if attempt == 1:
                sys_log(f"Comando FFmpeg: {' '.join(cmd)}")
            else:
                sys_log(f"Reintento #{attempt} para {filename} (faltan {seg_dur_str} de vídeo)...")
                status['progress'] = f"Reconectando stream (intento #{attempt})..."

            attempt_start = time.monotonic()
            proc, _ = _run_ffmpeg_process(cmd, remaining_media_sec, prefix="")
            returncodes.append(proc.returncode)

            seg_media_sec = 0
            if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
                segment_files.append(seg_path)
                seg_media_sec = get_media_duration_seconds(seg_path)
                if seg_media_sec == 0:
                    seg_media_sec = max(1, int(round(time.monotonic() - attempt_start)))
                accumulated_media_sec += seg_media_sec
            elif os.path.exists(seg_path):
                try:
                    os.remove(seg_path)
                except Exception:
                    pass

            if stop_requested:
                break

            # Si FFmpeg terminó limpiamente habiendo cumplido la duración de vídeo pedida, no reconectar
            if proc.returncode == 0 and (seg_media_sec >= remaining_media_sec - 3 or accumulated_media_sec >= requested_sec - 3):
                break

            remaining_after = int(round((target_end_utc - now_utc()).total_seconds()))
            if remaining_after <= 15 or (requested_sec - accumulated_media_sec) <= 5:
                break

            time.sleep(4)

        if len(segment_files) > 1:
            status['progress'] = "Uniendo fragmentos de grabación..."
            _merge_segments_lossless(segment_files, filepath, format_ext)
        elif len(segment_files) == 1 and segment_files[0] != filepath:
            os.replace(segment_files[0], filepath)

        end_dt = now_local()
        elapsed_real_sec = real_seconds_between(start_dt, end_dt)

        status_rec, reasons, filesize_bytes, media_duration_sec = _evaluate_recording(
            returncodes, filepath, duration, was_stopped=stop_requested
        )
        if media_duration_sec == 0 and filesize_bytes > 0:
            media_duration_sec = elapsed_real_sec

        reason_str = "; ".join(reasons)
        sys_log(f"Finalizada Inmediata: {filename} | Estado: {status_rec} | Tamaño: {human_size(filesize_bytes)} | Detalles: {reason_str}")

        final_progress_msg = ""
        if status_rec == "ok":
            final_progress_msg = f"✅ Grabación completada ({human_size(filesize_bytes)})"
            emit_app_notification(
                title="✅ Grabación completada",
                body=f"{filename} · {seconds_to_hhmmss(media_duration_sec)} · {human_size(filesize_bytes)}"
                     + (f" · 🌍 {active_country_label}" if active_country_label else ""),
                event_type="recording_ok",
                filename=filename
            )
            send_telegram_message(
                f"✅ Grabación en directo finalizada con éxito:\n"
                f"📁 {filename}\n"
                f"⏱️ Duración: {seconds_to_hhmmss(media_duration_sec)}\n"
                f"💾 Tamaño: {human_size(filesize_bytes)}"
                + (f"\n🌍 VPN: {active_country_label}" if active_country_label else "")
            )
        elif status_rec == "stopped":
            final_progress_msg = f"⏹️ Grabación detenida ({human_size(filesize_bytes)})"
            emit_app_notification(
                title="⏹️ Grabación detenida",
                body=f"{filename} · {seconds_to_hhmmss(media_duration_sec)} · {human_size(filesize_bytes)}",
                event_type="recording_stopped",
                filename=filename
            )
            send_telegram_message(
                f"⏹️ Grabación en directo detenida por el usuario:\n"
                f"📁 {filename}\n"
                f"⏱️ Duración: {seconds_to_hhmmss(media_duration_sec)}\n"
                f"💾 Tamaño: {human_size(filesize_bytes)}"
            )
        else:
            final_progress_msg = f"❌ Error: {reason_str}"
            emit_app_notification(
                title="❌ Error en grabación",
                body=f"{filename} · {reason_str}",
                event_type="recording_failed",
                filename=filename
            )
            send_telegram_message(
                f"❌ Grabación en directo interrumpida/fallida:\n"
                f"📁 {filename}\n"
                f"⚠️ Motivo: {reason_str}"
            )

        status['filename'] = filename
        status['last_result'] = {"status": status_rec, "reason": reason_str}

        history.append({
            "filename": filename,
            "url": url,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "duration_requested": duration,
            "duration_seconds": elapsed_real_sec,
            "media_duration_seconds": media_duration_sec,
            "format": format_ext,
            "filesize_bytes": filesize_bytes,
            "filesize_human": human_size(filesize_bytes),
            "status": status_rec,
            "status_reason": reason_str,
            "vpn_country": active_country_label,
            "type": "immediate"
        })
        save_history()

        _restore_recording_vpn(previous_country, did_switch_vpn, restore_vpn)
        status['progress'] = final_progress_msg
    except Exception as e:
        sys_log(f"Excepción en Inmediata: {str(e)}")
        status['progress'] = f'Excepción: {str(e)}'
        status['last_result'] = {"status": "failed", "reason": f'Excepción: {str(e)}'}
        send_telegram_message(f"❌ Excepción en grabación en directo:\n📁 {filename}\n⚠️ {str(e)}")
        _restore_recording_vpn(previous_country, did_switch_vpn, restore_vpn)
    finally:
        status['running'] = False
        status['pid'] = None
        status['start_time'] = None
        status['duration_requested'] = None
        status['mode'] = None
        status['vpn_country'] = None
        stop_requested = False


def run_scheduled_ffmpeg(job, initial_duration_str):
    global status, history, scheduled_jobs, stop_requested
    stop_requested = False
    status['mode'] = 'scheduled'
    status['filename'] = job['filename']
    req_vpn_country = (job.get('vpn_country') or '').strip()
    restore_vpn = bool(job.get('restore_vpn', True))
    status['vpn_country'] = req_vpn_country or None
    status['progress'] = f"[Programada] Preparando: {job['filename']}..."

    previous_country = job.get('_pre_switched_from') or ""
    did_switch_vpn = bool(job.get('_pre_switched_done', False))

    try:
        if req_vpn_country and not did_switch_vpn:
            previous_country, did_switch_vpn = _prepare_recording_vpn(req_vpn_country)

        active_vpn_info = get_gluetun_info(force=False)
        active_country_label = req_vpn_country or active_vpn_info.get("configured_country") or active_vpn_info.get("ip_country") or ""

        start_dt = now_local()
        end_target_utc = to_utc(job['end_time'])
        total_requested_sec = max(1, int(round((end_target_utc - start_dt.astimezone(timezone.utc)).total_seconds())))
        duration_str = seconds_to_hhmmss(total_requested_sec)

        status['start_time'] = start_dt.isoformat()
        status['duration_requested'] = total_requested_sec
        status['progress'] = f"[Programada] Iniciando: {job['filename']}..."

        filepath = os.path.join(OUTPUT_DIR, job['filename'])
        sys_log(
            f"Iniciando grabación PROGRAMADA ({job['filename']}) | "
            f"Inicio real: {start_dt.isoformat()} | Fin objetivo UTC: {end_target_utc.isoformat()} | "
            f"Duración real a grabar: {duration_str} ({total_requested_sec}s) | VPN: {active_country_label or 'Actual'}"
        )
        emit_app_notification(
            title="🔴 Grabación programada iniciada",
            body=f"{job['filename']} · Duración: {duration_str}" + (f" · 🌍 {active_country_label}" if active_country_label else ""),
            event_type="recording_started",
            filename=job['filename']
        )

        segment_files = []
        returncodes = []
        attempt = 0

        while not stop_requested:
            remaining_wall_sec = int(round((end_target_utc - now_utc()).total_seconds()))
            if remaining_wall_sec <= 3 or attempt >= 10:
                break

            attempt += 1
            ffmpeg_target_sec = remaining_wall_sec + IPTV_BUFFER_PADDING_SEC
            seg_path = filepath if attempt == 1 else f"{filepath}.part{attempt}.{job['format']}"
            seg_dur_str = seconds_to_hhmmss(ffmpeg_target_sec)
            cmd = _build_ffmpeg_cmd(job['url'], seg_dur_str, seg_path, job['format'])

            if attempt == 1:
                sys_log(f"Comando FFmpeg Programada (+{IPTV_BUFFER_PADDING_SEC}s colchón buffer IPTV): {' '.join(cmd)}")
            else:
                sys_log(f"Reintento #{attempt} en Programada ({job['filename']}), restan {seg_dur_str}...")
                status['progress'] = f"[Programada] Reconectando (#{attempt})..."

            attempt_start = time.monotonic()
            proc, _ = _run_ffmpeg_process(cmd, ffmpeg_target_sec, prefix="[Programada] ")
            returncodes.append(proc.returncode)

            seg_media_sec = 0
            if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
                segment_files.append(seg_path)
                seg_media_sec = get_media_duration_seconds(seg_path)
                if seg_media_sec == 0:
                    seg_media_sec = max(1, int(round(time.monotonic() - attempt_start)))
            elif os.path.exists(seg_path):
                try:
                    os.remove(seg_path)
                except Exception:
                    pass

            if stop_requested:
                break

            if proc.returncode == 0 and seg_media_sec >= ffmpeg_target_sec - 3:
                break

            remaining_after = int(round((end_target_utc - now_utc()).total_seconds()))
            if remaining_after <= 15:
                break

            time.sleep(5)

        if len(segment_files) > 1:
            status['progress'] = "[Programada] Uniendo segmentos..."
            _merge_segments_lossless(segment_files, filepath, job['format'])
        elif len(segment_files) == 1 and segment_files[0] != filepath:
            os.replace(segment_files[0], filepath)

        end_dt = now_local()
        elapsed_real_sec = real_seconds_between(start_dt, end_dt)

        status_rec, reasons, filesize_bytes, media_duration_sec = _evaluate_recording(
            returncodes, filepath, duration_str, was_stopped=stop_requested
        )
        if media_duration_sec == 0 and filesize_bytes > 0:
            media_duration_sec = elapsed_real_sec

        reason_str = "; ".join(reasons)
        sys_log(f"Finalizada Programada: {job['filename']} | Estado: {status_rec} | Tamaño: {human_size(filesize_bytes)} | Detalles: {reason_str}")

        job['status'] = status_rec
        job['status_reason'] = reason_str
        job['actual_start'] = start_dt.isoformat()
        job['actual_end'] = end_dt.isoformat()
        job['filesize_human'] = human_size(filesize_bytes)
        job['filesize_bytes'] = filesize_bytes
        save_schedules()

        final_progress_msg = ""
        if status_rec == "ok":
            emit_app_notification(
                title="✅ Grabación programada completada",
                body=f"{job['filename']} · {seconds_to_hhmmss(media_duration_sec)} · {human_size(filesize_bytes)}"
                     + (f" · 🌍 {active_country_label}" if active_country_label else ""),
                event_type="recording_ok",
                filename=job['filename']
            )
            send_telegram_message(
                f"✅ Grabación programada finalizada con éxito:\n"
                f"📁 {job['filename']}\n"
                f"⏱️ Duración: {seconds_to_hhmmss(media_duration_sec)}\n"
                f"💾 Tamaño: {human_size(filesize_bytes)}"
                + (f"\n🌍 VPN: {active_country_label}" if active_country_label else "")
            )
            final_progress_msg = f"[Programada] ✅ Completado ({human_size(filesize_bytes)})"
        elif status_rec == "stopped":
            emit_app_notification(
                title="⏹️ Grabación programada detenida",
                body=f"{job['filename']} · {seconds_to_hhmmss(media_duration_sec)} · {human_size(filesize_bytes)}",
                event_type="recording_stopped",
                filename=job['filename']
            )
            send_telegram_message(
                f"⏹️ Grabación programada detenida manualmente:\n"
                f"📁 {job['filename']}\n"
                f"⏱️ Duración: {seconds_to_hhmmss(media_duration_sec)}\n"
                f"💾 Tamaño: {human_size(filesize_bytes)}"
            )
            final_progress_msg = f"[Programada] ⏹️ Detenida ({human_size(filesize_bytes)})"
        else:
            emit_app_notification(
                title="❌ Error en grabación programada",
                body=f"{job['filename']} · {reason_str}",
                event_type="recording_failed",
                filename=job['filename']
            )
            send_telegram_message(
                f"❌ Error en grabación programada:\n"
                f"📁 {job['filename']}\n"
                f"⚠️ Motivo: {reason_str}"
            )
            final_progress_msg = f"[Programada] ❌ Error: {reason_str}"

        status['filename'] = job['filename']
        status['last_result'] = {"status": status_rec, "reason": reason_str}

        history.append({
            "filename": job['filename'],
            "url": job['url'],
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "duration_requested": duration_str,
            "duration_seconds": elapsed_real_sec,
            "media_duration_seconds": media_duration_sec,
            "format": job['format'],
            "filesize_bytes": filesize_bytes,
            "filesize_human": human_size(filesize_bytes),
            "status": status_rec,
            "status_reason": reason_str,
            "vpn_country": active_country_label,
            "type": "scheduled",
            "scheduled_start": job['start_time'],
            "scheduled_end": job['end_time'],
        })
        save_history()

        _restore_recording_vpn(previous_country, did_switch_vpn, restore_vpn)
        status['progress'] = final_progress_msg
    except Exception as e:
        reason_str = f'Excepción: {str(e)}'
        sys_log(f"Excepción en Programada: {reason_str}")
        job['status'] = 'failed'
        job['status_reason'] = reason_str
        save_schedules()

        send_telegram_message(f"❌ Excepción fatal en grabación programada:\n📁 {job['filename']}\n⚠️ Error: {str(e)}")

        _restore_recording_vpn(previous_country, did_switch_vpn, restore_vpn)
        status['progress'] = f'[Programada] {reason_str}'
        status['last_result'] = {"status": "failed", "reason": reason_str}
    finally:
        status['running'] = False
        status['pid'] = None
        status['start_time'] = None
        status['duration_requested'] = None
        status['mode'] = None
        status['vpn_country'] = None
        stop_requested = False


def scheduler_loop():
    global status
    sys_log(f"Servicio de programador (scheduler_loop) arrancado. Hora servidor: {now_local().isoformat()}")
    while True:
        time.sleep(2)
        current_utc = now_utc()
        changed = False

        for job in scheduled_jobs:
            if job.get('status') != 'pending':
                continue
            try:
                start_utc = to_utc(job['start_time'])
                end_utc = to_utc(job['end_time'])
            except Exception as e:
                sys_log(f"Error parseando fechas de programación {job.get('filename')}: {e}")
                continue

            secs_until_start = (start_utc - current_utc).total_seconds()

            # Pre-conectar al país VPN 25 segundos antes de la hora de inicio si el grabador está libre
            req_vpn = (job.get('vpn_country') or '').strip()
            if req_vpn and not status['running'] and 0 < secs_until_start <= 25 and not job.get('_pre_switched_done'):
                sys_log(f"Pre-conectando VPN a '{req_vpn}' antes de iniciar tarea programada {job['filename']}...")
                prev_c, did_sw = _prepare_recording_vpn(req_vpn)
                job['_pre_switched_from'] = prev_c
                job['_pre_switched_done'] = did_sw
                status['progress'] = f"🌍 VPN lista en {req_vpn} (esperando inicio de {job['filename']})"

            if current_utc < start_utc:
                continue

            remaining_secs = int(round((end_utc - current_utc).total_seconds()))

            if remaining_secs <= 0:
                job['status'] = 'failed'
                job['status_reason'] = 'Tiempo expirado sin poder iniciar'
                changed = True
                send_telegram_message(
                    f"❌ Grabación programada no iniciada (tiempo expirado):\n📁 {job.get('filename')}"
                )
                continue

            overdue_secs = int(round((current_utc - start_utc).total_seconds()))

            if status['running'] and overdue_secs > SCHEDULE_START_TOLERANCE:
                job['status'] = 'failed'
                job['status_reason'] = f'No se pudo iniciar (grabador ocupado >{SCHEDULE_START_TOLERANCE}s)'
                changed = True
                send_telegram_message(
                    f"❌ Grabación programada no iniciada (grabador ocupado):\n📁 {job.get('filename')}"
                )
                continue

            if status['running']:
                continue

            sys_log(f"¡Hora alcanzada! Lanzando tarea programada: {job['filename']} (Duración real: {seconds_to_hhmmss(remaining_secs)})")
            job['status'] = 'running'
            status['running'] = True
            changed = True
            save_schedules()
            duration_str = seconds_to_hhmmss(remaining_secs)
            threading.Thread(
                target=run_scheduled_ffmpeg,
                args=(job, duration_str),
                daemon=True
            ).start()

        if changed:
            save_schedules()


# ─────────────────────────────────────────────
#  HTML Template (Modern Dark UI + DST + VPN Control)
# ─────────────────────────────────────────────
HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>M3U8 Stream Recorder · DVR + VPN</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="theme-color" content="#090d16">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-base: #090d16;
    --bg-surface: #111827;
    --bg-card: #161f33;
    --bg-input: #0b1120;
    --border-subtle: rgba(148, 163, 184, 0.14);
    --border-focus: #3b82f6;
    --text-primary: #f1f5f9;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --accent-blue: #3b82f6;
    --accent-indigo: #6366f1;
    --accent-emerald: #10b981;
    --accent-amber: #f59e0b;
    --accent-rose: #f43f5e;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: radial-gradient(circle at 15% 0%, #172554 0%, var(--bg-base) 45%);
    background-attachment: fixed;
    color: var(--text-primary);
    min-height: 100vh;
    padding: 24px 16px 60px;
  }

  .app-shell {
    max-width: 1200px;
    margin: 0 auto;
    background: rgba(17, 24, 39, 0.85);
    backdrop-filter: blur(16px);
    border-radius: 20px;
    border: 1px solid var(--border-subtle);
    box-shadow: 0 25px 60px -15px rgba(0, 0, 0, 0.7);
    overflow: hidden;
  }

  /* ── Topbar / Header ── */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: nowrap;
    gap: 14px;
    padding: 18px 28px;
    background: linear-gradient(135deg, rgba(30, 58, 138, 0.55) 0%, rgba(17, 24, 39, 0.9) 100%);
    border-bottom: 1px solid var(--border-subtle);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }
  .brand-icon {
    width: 42px;
    height: 42px;
    flex-shrink: 0;
    border-radius: 11px;
    background: linear-gradient(135deg, var(--accent-blue), var(--accent-indigo));
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    box-shadow: 0 8px 20px rgba(59, 130, 246, 0.35);
  }
  .brand-text h1 {
    font-size: 19px;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: #fff;
    white-space: nowrap;
  }
  .brand-text p {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .topbar-pills {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: nowrap;
    flex-shrink: 0;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    background: rgba(15, 23, 42, 0.75);
    border: 1px solid var(--border-subtle);
    color: #e2e8f0;
    white-space: nowrap;
  }
  .pill-vpn {
    border-color: rgba(16, 185, 129, 0.35);
    background: rgba(6, 78, 59, 0.25);
    padding: 4px 8px 4px 11px;
  }
  .vpn-quick-select {
    width: auto;
    max-width: 135px;
    padding: 3px 6px;
    font-size: 11.5px;
    font-weight: 600;
    border-radius: 7px;
    background: rgba(15, 23, 42, 0.9);
    border: 1px solid rgba(148, 163, 184, 0.25);
    color: #a7f3d0;
    cursor: pointer;
  }
  .pill-clock {
    font-family: 'JetBrains Mono', monospace;
    color: #93c5fd;
    border-color: rgba(59, 130, 246, 0.3);
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent-emerald);
    box-shadow: 0 0 8px var(--accent-emerald);
  }
  .dot.recording {
    background: var(--accent-rose);
    box-shadow: 0 0 10px var(--accent-rose);
    animation: pulse 1.2s infinite;
  }
  .dot.switching {
    background: var(--accent-amber);
    box-shadow: 0 0 10px var(--accent-amber);
    animation: pulse 0.9s infinite;
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.35); opacity: 0.65; }
  }

  /* ── Global Live Status Banner ── */
  .live-banner {
    margin: 20px 28px 0;
    padding: 18px 22px;
    border-radius: 14px;
    background: linear-gradient(135deg, rgba(30, 41, 59, 0.9), rgba(15, 23, 42, 0.95));
    border: 1px solid var(--border-subtle);
    transition: all 0.3s ease;
  }
  .live-banner.is-recording {
    background: linear-gradient(135deg, rgba(136, 19, 55, 0.35), rgba(30, 41, 59, 0.95));
    border-color: rgba(244, 63, 94, 0.45);
    box-shadow: 0 10px 30px rgba(244, 63, 94, 0.12);
  }
  .live-banner.is-success {
    background: linear-gradient(135deg, rgba(6, 95, 70, 0.35), rgba(15, 23, 42, 0.95));
    border-color: rgba(16, 185, 129, 0.4);
  }
  .live-banner.is-error {
    background: linear-gradient(135deg, rgba(127, 29, 29, 0.4), rgba(15, 23, 42, 0.95));
    border-color: rgba(244, 63, 94, 0.45);
  }
  .live-banner-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  .live-status-main {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 14px;
    font-weight: 600;
    color: #f8fafc;
    word-break: break-all;
  }
  .live-metrics {
    display: flex;
    align-items: center;
    gap: 12px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12.5px;
    color: #cbd5e1;
  }
  .progress-track {
    background: rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    height: 8px;
    margin-top: 12px;
    overflow: hidden;
  }
  .progress-fill {
    background: linear-gradient(90deg, var(--accent-blue), var(--accent-indigo), #ec4899);
    height: 100%;
    width: 0%;
    transition: width 0.5s ease;
    border-radius: 999px;
  }

  /* ── Navigation Tabs ── */
  .nav-tabs {
    display: flex;
    gap: 6px;
    padding: 16px 28px 0;
    border-bottom: 1px solid var(--border-subtle);
    overflow-x: auto;
  }
  .nav-tab {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 13px 20px;
    border: none;
    background: transparent;
    color: var(--text-secondary);
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all 0.2s ease;
    white-space: nowrap;
    border-radius: 10px 10px 0 0;
  }
  .nav-tab:hover {
    color: var(--text-primary);
    background: rgba(255, 255, 255, 0.03);
  }
  .nav-tab.active {
    color: #60a5fa;
    border-bottom-color: var(--accent-blue);
    background: rgba(59, 130, 246, 0.08);
  }
  .tab-count {
    font-size: 11px;
    padding: 2px 7px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.16);
    color: #cbd5e1;
    font-weight: 700;
  }
  .nav-tab.active .tab-count {
    background: rgba(59, 130, 246, 0.25);
    color: #93c5fd;
  }

  /* ── Main Content & Cards ── */
  .content { padding: 26px 28px 32px; }
  .panel-card {
    background: var(--bg-card);
    border: 1px solid var(--border-subtle);
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 24px;
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 20px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--border-subtle);
  }
  .card-title {
    font-size: 16.5px;
    font-weight: 700;
    color: #f8fafc;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  /* ── Forms & Inputs ── */
  .form-group { margin-bottom: 18px; }
  .form-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
  }
  .form-row-3 {
    display: grid;
    grid-template-columns: 1.2fr 0.8fr 1.2fr;
    gap: 18px;
  }
  label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 600;
    margin-bottom: 7px;
    color: #cbd5e1;
    font-size: 13px;
  }
  input, select {
    width: 100%;
    padding: 11px 14px;
    border: 1px solid rgba(148, 163, 184, 0.22);
    border-radius: 10px;
    font-size: 14.5px;
    font-family: 'Inter', sans-serif;
    transition: all 0.2s;
    background: var(--bg-input);
    color: var(--text-primary);
  }
  input:focus, select:focus {
    outline: none;
    border-color: var(--border-focus);
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
  }
  .mono-input { font-family: 'JetBrains Mono', monospace; }

  .preset-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }
  .chip {
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    background: rgba(59, 130, 246, 0.12);
    border: 1px solid rgba(59, 130, 246, 0.25);
    color: #93c5fd;
    cursor: pointer;
    transition: all 0.15s;
    flex: none;
  }
  .chip:hover {
    background: rgba(59, 130, 246, 0.25);
    transform: translateY(-1px);
  }

  /* DST Ambiguity Selector & Summary Box */
  .dst-fold-box {
    margin-top: 8px;
    padding: 10px 12px;
    border-radius: 10px;
    background: rgba(245, 158, 11, 0.12);
    border: 1px solid rgba(245, 158, 11, 0.35);
    font-size: 12.5px;
    color: #fde68a;
  }
  .dst-fold-options {
    display: flex;
    gap: 8px;
    margin-top: 7px;
    flex-wrap: wrap;
  }
  .fold-btn {
    flex: 1;
    padding: 7px 10px;
    border-radius: 8px;
    border: 1px solid rgba(245, 158, 11, 0.35);
    background: rgba(15, 23, 42, 0.7);
    color: #cbd5e1;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .fold-btn.active {
    background: #d97706;
    color: #fff;
    border-color: #f59e0b;
  }

  .schedule-preview {
    padding: 14px 18px;
    border-radius: 12px;
    background: rgba(15, 23, 42, 0.75);
    border: 1px solid rgba(59, 130, 246, 0.28);
    margin-bottom: 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    font-size: 13.5px;
  }
  .schedule-preview.has-dst {
    background: linear-gradient(135deg, rgba(120, 53, 15, 0.32), rgba(15, 23, 42, 0.9));
    border-color: rgba(245, 158, 11, 0.45);
  }

  /* ── Buttons ── */
  .btn-group { display: flex; gap: 12px; margin-top: 10px; flex-wrap: wrap; }
  button {
    padding: 11px 18px;
    border: none;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    font-family: 'Inter', sans-serif;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
  }
  .btn-primary {
    background: linear-gradient(135deg, #2563eb 0%, #4f46e5 100%);
    color: white;
    box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
  }
  .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(37, 99, 235, 0.45); }
  .btn-secondary {
    background: rgba(51, 65, 85, 0.7);
    color: #e2e8f0;
    border: 1px solid rgba(148, 163, 184, 0.2);
  }
  .btn-secondary:hover { background: rgba(71, 85, 105, 0.9); }
  .btn-danger {
    background: rgba(225, 29, 72, 0.85);
    color: white;
  }
  .btn-danger:hover { background: #be123c; }
  .btn-warning {
    background: #d97706;
    color: white;
  }
  .btn-warning:hover { background: #b45309; }
  .btn-success {
    background: #059669;
    color: white;
  }
  .btn-success:hover { background: #047857; }
  .btn-sm {
    padding: 6px 11px;
    font-size: 12.5px;
    border-radius: 8px;
  }

  /* ── Terminal Log & Download ── */
  .log-box {
    background: #070b14;
    padding: 14px;
    border-radius: 10px;
    height: 150px;
    overflow-y: auto;
    font-size: 12.5px;
    font-family: 'JetBrains Mono', monospace;
    border: 1px solid var(--border-subtle);
    color: #94a3b8;
    line-height: 1.6;
  }
  .download-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 16px;
    padding: 14px 18px;
    background: rgba(5, 150, 105, 0.18);
    border: 1px solid rgba(16, 185, 129, 0.4);
    color: #a7f3d0;
    text-decoration: none;
    border-radius: 12px;
    font-weight: 600;
    transition: all 0.2s;
  }
  .download-banner:hover {
    background: rgba(5, 150, 105, 0.28);
    transform: translateY(-1px);
  }

  /* ── Tables ── */
  .table-wrap {
    overflow-x: auto;
    border-radius: 12px;
    border: 1px solid var(--border-subtle);
    background: rgba(11, 17, 32, 0.65);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th {
    background: rgba(30, 41, 59, 0.85);
    color: #cbd5e1;
    padding: 13px 14px;
    text-align: left;
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-bottom: 1px solid var(--border-subtle);
  }
  td {
    padding: 12px 14px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.08);
    color: #e2e8f0;
    vertical-align: middle;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255, 255, 255, 0.025); }

  /* Badges */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 600;
    white-space: nowrap;
  }
  .badge-pending   { background: rgba(59, 130, 246, 0.18); color: #93c5fd; border: 1px solid rgba(59, 130, 246, 0.3); }
  .badge-running   { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.35); }
  .badge-ok        { background: rgba(16, 185, 129, 0.18); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.3); }
  .badge-stopped   { background: rgba(245, 158, 11, 0.18); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.35); }
  .badge-failed    { background: rgba(244, 63, 94, 0.18); color: #fda4af; border: 1px solid rgba(244, 63, 94, 0.35); }
  .badge-cancelled { background: rgba(100, 116, 139, 0.2); color: #94a3b8; border: 1px solid rgba(100, 116, 139, 0.3); }
  .badge-scheduled { background: rgba(99, 102, 241, 0.18); color: #c7d2fe; }
  .badge-immediate { background: rgba(14, 165, 233, 0.18); color: #7dd3fc; }
  .badge-tz        { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; padding: 2px 6px; border-radius: 5px; background: rgba(148, 163, 184, 0.14); color: #cbd5e1; }
  .badge-vpn       { font-size: 11px; padding: 2px 7px; border-radius: 6px; background: rgba(16, 185, 129, 0.15); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.3); }

  .countdown {
    font-family: 'JetBrains Mono', monospace;
    color: #fbbf24;
    font-weight: 600;
    font-size: 12.5px;
  }

  /* Stats strip */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px;
    margin-bottom: 20px;
  }
  .stat-card {
    background: rgba(15, 23, 42, 0.65);
    border: 1px solid var(--border-subtle);
    border-radius: 12px;
    padding: 14px 16px;
  }
  .stat-label { font-size: 12px; color: var(--text-secondary); }
  .stat-value { font-size: 20px; font-weight: 700; color: #f8fafc; margin-top: 4px; font-family: 'JetBrains Mono', monospace; }

  /* Search & Filter bar */
  .search-bar {
    display: flex;
    gap: 10px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .search-bar input { flex: 2; min-width: 220px; }
  .search-bar select { flex: 1; min-width: 160px; }

  /* Toast Notification */
  .toast-container {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .toast {
    padding: 12px 18px;
    border-radius: 10px;
    background: #1e293b;
    border: 1px solid rgba(148, 163, 184, 0.25);
    color: #f8fafc;
    font-size: 13.5px;
    font-weight: 500;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
    animation: slideIn 0.2s ease;
  }
  @keyframes slideIn {
    from { transform: translateY(15px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }

  /* ── Cinema Video Player Modal & Custom Pro Control Bar ── */
  .player-modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 9500;
    background: rgba(3, 7, 18, 0.88);
    backdrop-filter: blur(10px);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .player-modal-backdrop.open { display: flex; }
  .player-modal {
    width: 100%;
    max-width: 1040px;
    background: #0f172a;
    border: 1px solid rgba(148, 163, 184, 0.22);
    border-radius: 18px;
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.85);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .player-modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 20px;
    background: #111827;
    border-bottom: 1px solid var(--border-subtle);
  }
  .player-modal-title {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 15px;
    font-weight: 700;
    color: #f8fafc;
    min-width: 0;
  }
  .player-modal-title span.fname {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .player-close-btn {
    background: rgba(51, 65, 85, 0.6);
    color: #cbd5e1;
    border: 1px solid rgba(148, 163, 184, 0.2);
    padding: 6px 12px;
    border-radius: 8px;
    font-size: 13px;
    cursor: pointer;
  }
  .player-close-btn:hover { background: #e11d48; color: #fff; border-color: #e11d48; }

  .video-stage {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #000;
    overflow: hidden;
    user-select: none;
  }
  .video-stage video {
    width: 100%;
    height: 100%;
    display: block;
    background: #000;
  }

  /* Custom Pro Controls Overlay inside Video Stage */
  .v-controls {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    padding: 28px 18px 14px;
    background: linear-gradient(to top, rgba(0, 0, 0, 0.9) 0%, rgba(0, 0, 0, 0.55) 60%, transparent 100%);
    transition: opacity 0.25s ease;
    display: flex;
    flex-direction: column;
    gap: 10px;
    z-index: 20;
  }
  .video-stage.hide-controls .v-controls {
    opacity: 0;
    pointer-events: none;
  }
  .video-stage.hide-controls { cursor: none; }

  /* Timeline Scrubber */
  .v-timeline {
    position: relative;
    height: 7px;
    background: rgba(255, 255, 255, 0.22);
    border-radius: 999px;
    cursor: pointer;
    transition: height 0.15s;
  }
  .v-timeline:hover { height: 10px; }
  .v-timeline-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, #3b82f6, #6366f1);
    border-radius: 999px;
    position: relative;
  }
  .v-timeline-thumb {
    position: absolute;
    right: -6px;
    top: 50%;
    transform: translateY(-50%);
    width: 13px;
    height: 13px;
    border-radius: 50%;
    background: #fff;
    box-shadow: 0 0 8px rgba(59, 130, 246, 0.9);
    opacity: 0;
    transition: opacity 0.15s;
  }
  .v-timeline:hover .v-timeline-thumb { opacity: 1; }
  .v-timeline-tooltip {
    position: absolute;
    bottom: 16px;
    transform: translateX(-50%);
    background: rgba(15, 23, 42, 0.95);
    color: #f8fafc;
    padding: 3px 7px;
    border-radius: 5px;
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
    pointer-events: none;
    display: none;
    border: 1px solid rgba(148, 163, 184, 0.25);
  }
  .v-timeline:hover .v-timeline-tooltip { display: block; }

  .v-bar-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .v-bar-left, .v-bar-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .v-btn {
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: #f8fafc;
    padding: 6px 11px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }
  .v-btn:hover {
    background: rgba(59, 130, 246, 0.35);
    border-color: rgba(59, 130, 246, 0.6);
    transform: none;
  }
  .v-time {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12.5px;
    color: #e2e8f0;
    margin-left: 4px;
  }
  .v-vol-slider {
    width: 75px;
    height: 5px;
    padding: 0;
    accent-color: #3b82f6;
    cursor: pointer;
  }

  /* Pop-up menus inside Video Player (Audio Tracks & Speed) */
  .v-menu-wrap { position: relative; }
  .v-popup-menu {
    position: absolute;
    bottom: calc(100% + 10px);
    right: 0;
    min-width: 240px;
    background: rgba(15, 23, 42, 0.96);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(148, 163, 184, 0.28);
    border-radius: 12px;
    padding: 8px;
    box-shadow: 0 15px 35px rgba(0, 0, 0, 0.7);
    display: none;
    flex-direction: column;
    gap: 4px;
    z-index: 35;
  }
  .v-popup-menu.open { display: flex; }
  .v-popup-title {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #94a3b8;
    padding: 4px 8px 6px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.15);
    margin-bottom: 2px;
  }
  .v-menu-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    width: 100%;
    padding: 8px 10px;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: #e2e8f0;
    font-size: 12.5px;
    font-weight: 500;
    text-align: left;
    cursor: pointer;
  }
  .v-menu-item:hover { background: rgba(59, 130, 246, 0.2); }
  .v-menu-item.active {
    background: rgba(59, 130, 246, 0.28);
    color: #93c5fd;
    font-weight: 700;
  }

  .player-modal-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    padding: 13px 20px;
    background: #111827;
    border-top: 1px solid var(--border-subtle);
    font-size: 12.5px;
    color: var(--text-secondary);
  }
  .player-footer-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: #0b1120; }
  ::-webkit-scrollbar-thumb { background: #334155; border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: #475569; }

  .tab-icon { font-size: 15px; line-height: 1; }
  .tab-label-short { display: none; }

  @media (max-width: 860px) {
    .topbar, .topbar-pills { flex-wrap: wrap; }
    .form-row, .form-row-3 { grid-template-columns: 1fr; }
    .topbar, .content { padding: 18px; }
    .live-banner { margin: 14px 18px 0; }
    .nav-tabs { padding: 12px 18px 0; }
  }

  /* ── Adaptación Nativa para Pantallas Móviles (<= 768px) ── */
  @media (max-width: 768px) {
    body {
      padding: 0 0 calc(78px + env(safe-area-inset-bottom, 0px)) 0;
    }
    .app-shell {
      border-radius: 0;
      border: none;
      min-height: 100vh;
      backdrop-filter: none;
      -webkit-backdrop-filter: none;
      overflow: visible;
      box-shadow: none;
    }

    /* Topbar compacto en móvil */
    .topbar {
      padding: 12px 14px;
      gap: 10px;
      flex-direction: column;
      align-items: stretch;
    }
    .brand-icon {
      width: 34px;
      height: 34px;
      font-size: 17px;
      border-radius: 9px;
    }
    .brand-text h1 { font-size: 16.5px; }
    .brand-text p { font-size: 11px; }
    .topbar-pills {
      width: 100%;
      justify-content: space-between;
      gap: 6px;
      flex-wrap: nowrap;
    }
    .pill {
      padding: 5px 9px;
      font-size: 11px;
      gap: 5px;
    }
    .pill-vpn {
      flex: 1;
      min-width: 0;
      justify-content: space-between;
      padding: 4px 7px;
    }
    .vpn-quick-select {
      max-width: 108px;
      font-size: 11px;
      padding: 2px 4px;
      min-height: 26px;
    }

    /* Banner de estado en vivo */
    .live-banner {
      margin: 12px 12px 0;
      padding: 12px 14px;
      border-radius: 12px;
    }
    .live-status-main { font-size: 13px; }
    .live-metrics { width: 100%; justify-content: space-between; }

    /* Barra de navegación inferior fija tipo App Móvil */
    .nav-tabs {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      width: 100%;
      z-index: 9000;
      background: rgba(11, 17, 32, 0.96);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      border-top: 1px solid rgba(148, 163, 184, 0.22);
      border-bottom: none;
      padding: 6px 6px calc(6px + env(safe-area-inset-bottom, 0px));
      gap: 4px;
      justify-content: space-around;
      box-shadow: 0 -8px 25px rgba(0, 0, 0, 0.65);
    }
    .nav-tab {
      flex: 1;
      flex-direction: column;
      justify-content: center;
      gap: 3px;
      padding: 7px 4px;
      font-size: 11px;
      border-radius: 10px;
      border-bottom: none;
      position: relative;
    }
    .nav-tab.active {
      background: rgba(59, 130, 246, 0.16);
      color: #60a5fa;
    }
    .tab-icon { font-size: 18px; }
    .tab-label-full { display: none; }
    .tab-label-short { display: inline; }
    .tab-count {
      position: absolute;
      top: 3px;
      right: 12%;
      font-size: 9.5px;
      padding: 1px 5px;
    }

    /* Contenido y Tarjetas */
    .content { padding: 12px; }
    .panel-card {
      padding: 15px 14px;
      border-radius: 14px;
      margin-bottom: 14px;
    }
    .card-header {
      margin-bottom: 14px;
      padding-bottom: 10px;
      gap: 8px;
    }
    .card-title { font-size: 15px; }

    /* Formularios táctiles (16px evita zoom automático en iOS/Android) */
    .form-group { margin-bottom: 14px; }
    .form-row, .form-row-3 { gap: 12px; }
    input, select {
      font-size: 16px;
      padding: 11px 12px;
      min-height: 44px;
    }
    .chip {
      padding: 7px 11px;
      font-size: 12.5px;
      border-radius: 8px;
    }
    .btn-group {
      flex-direction: column;
      gap: 10px;
    }
    .btn-group button {
      width: 100%;
      min-height: 46px;
      font-size: 15px;
    }
    .search-bar {
      flex-direction: column;
      gap: 8px;
    }
    .search-bar input, .search-bar select, .search-bar button {
      width: 100%;
      min-width: 0;
    }
    .stats-grid {
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 14px;
    }
    .stat-card { padding: 11px 12px; }
    .stat-value { font-size: 17px; }

    /* Transformar tablas anchas en Tarjetas Verticales Táctiles */
    .table-wrap {
      border: none;
      background: transparent;
      overflow-x: visible;
    }
    table, tbody { display: block; width: 100%; }
    thead { display: none; }
    tbody tr {
      display: block;
      background: rgba(11, 17, 32, 0.85);
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 13px;
      padding: 12px 13px;
      margin-bottom: 10px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
    }
    tbody td {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 6px 0;
      border-bottom: 1px solid rgba(148, 163, 184, 0.08);
      text-align: right;
      font-size: 13px;
    }
    tbody td:last-child { border-bottom: none; }
    tbody td[data-label]::before {
      content: attr(data-label);
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      color: var(--text-secondary);
      text-align: left;
      flex-shrink: 0;
    }
    tbody td.cell-main {
      display: block;
      text-align: left;
      padding-top: 0;
      padding-bottom: 9px;
      margin-bottom: 4px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.16);
    }
    tbody td.cell-main::before { display: none; }
    tbody td.cell-actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: stretch;
      gap: 8px;
      padding-top: 10px;
      margin-top: 2px;
    }
    tbody td.cell-actions::before { display: none; }
    tbody td.cell-actions button,
    tbody td.cell-actions a {
      flex: 1;
      min-height: 38px;
      justify-content: center;
      font-size: 13px;
    }

    /* Toasts por encima de la barra inferior en móvil */
    .toast-container {
      bottom: calc(82px + env(safe-area-inset-bottom, 0px));
      left: 12px;
      right: 12px;
    }
    .toast { text-align: center; }

    /* Modal de Vídeo Modo Cine adaptado a móvil */
    .player-modal-backdrop { padding: 8px; }
    .player-modal { border-radius: 14px; }
    .player-modal-header { padding: 10px 12px; }
    .player-modal-title { font-size: 13.5px; }
    .v-controls { padding: 18px 10px 10px; gap: 8px; }
    .v-bar-row { flex-wrap: wrap; gap: 6px; }
    .v-bar-left, .v-bar-right { gap: 5px; }
    .v-btn { padding: 6px 8px; font-size: 12px; }
    .v-vol-slider { display: none; }
    .v-time { font-size: 11px; }
    .v-popup-menu { min-width: 210px; right: 0; }
    .player-modal-footer {
      padding: 10px 12px;
      flex-direction: column;
      align-items: stretch;
    }
    .player-footer-actions {
      width: 100%;
      display: flex;
    }
    .player-footer-actions a,
    .player-footer-actions button {
      flex: 1;
      justify-content: center;
    }
  }
</style>
</head>
<body>
<div class="app-shell">

  <!-- Top Header -->
  <div class="topbar">
    <div class="brand">
      <div class="brand-icon">🎬</div>
      <div class="brand-text">
        <h1>M3U8 Stream Recorder</h1>
        <p>Grabación directa (-c copy) · ProtonVPN &amp; resistente a cambios de hora</p>
      </div>
    </div>
    <div class="topbar-pills">
      <!-- Selector y Estado VPN en vivo -->
      <div class="pill pill-vpn" id="vpnPill" title="Estado de Gluetun VPN">
        <span class="dot" id="vpnDot"></span>
        <span id="vpnStatusText">🛡️ VPN</span>
        <select id="topbarVpnSelect" class="vpn-quick-select" onchange="changeGlobalVpnCountry(this.value)" title="Cambiar país activo de la VPN">
          <option value="">🌍 País...</option>
        </select>
      </div>

      <div class="pill pill-clock" title="Hora local actual y zona horaria activa">
        <span>🕐</span>
        <span id="serverClock">--:--:--</span>
      </div>
      <div class="pill" id="globalStatePill">
        <span class="dot" id="globalDot"></span>
        <span id="globalStateText">Listo</span>
      </div>
    </div>
  </div>

  <!-- Global Live Recording Status Banner -->
  <div id="statusBanner" class="live-banner">
    <div class="live-banner-top">
      <div class="live-status-main">
        <span id="statusIcon">🟢</span>
        <span id="statusText">READY_STATUS</span>
      </div>
      <div class="live-metrics">
        <span id="liveTimer" style="display:none;">00:00:00 / 00:00:00</span>
        <button id="globalStopBtn" class="btn-danger btn-sm" style="display:none;" onclick="stopRecord()">⏹️ Detener grabación</button>
      </div>
    </div>
    <div class="progress-track" id="progressContainer" style="display:none;">
      <div class="progress-fill" id="progressBar"></div>
    </div>
  </div>

  <!-- Navigation Tabs -->
  <div class="nav-tabs">
    <button id="tabRecord" class="nav-tab active" onclick="showTab('record')">
      <span class="tab-icon">📹</span>
      <span class="tab-label-full">Grabador Directo</span>
      <span class="tab-label-short">Directo</span>
    </button>
    <button id="tabSchedule" class="nav-tab" onclick="showTab('schedule')">
      <span class="tab-icon">📅</span>
      <span class="tab-label-full">Programar</span>
      <span class="tab-label-short">Programar</span>
      <span class="tab-count" id="badgeSchedulesCount">0</span>
    </button>
    <button id="tabChannels" class="nav-tab" onclick="showTab('channels')">
      <span class="tab-icon">📺</span>
      <span class="tab-label-full">Canales M3U</span>
      <span class="tab-label-short">Canales</span>
      <span class="tab-count" id="badgeChannelsCount">CHANNELS_COUNT</span>
    </button>
    <button id="tabHistory" class="nav-tab" onclick="showTab('history')">
      <span class="tab-icon">📜</span>
      <span class="tab-label-full">Historial</span>
      <span class="tab-label-short">Historial</span>
      <span class="tab-count" id="badgeHistoryCount">0</span>
    </button>
  </div>

  <div class="content">

    <!-- TAB 1: GRABADOR DIRECTO -->
    <div id="recordView">
      <div class="panel-card">
        <div class="card-header">
          <div class="card-title">⚡ Nueva grabación al vuelo</div>
          <span style="font-size:12px;color:var(--text-secondary)">Calidad original intacta · Reconexión automática activa</span>
        </div>

        <form id="form">
          <div class="form-group">
            <label><span>🔗 Enlace del Stream (M3U8 / TS / HTTP)</span></label>
            <input type="url" id="url" placeholder="https://ejemplo.com/live/canal.m3u8" required>
          </div>

          <div class="form-row">
            <div class="form-group">
              <label>
                <span>⏱️ Duración (HH:MM:SS)</span>
                <span style="font-weight:400;font-size:11.5px;color:var(--text-secondary)">Elige o escribe</span>
              </label>
              <input type="text" id="duration" class="mono-input" value="01:00:00" pattern="\d{2,}:\d{2}:\d{2}" required>
              <div class="preset-chips">
                <button type="button" class="chip" onclick="setDirectDuration('00:15:00')">15m</button>
                <button type="button" class="chip" onclick="setDirectDuration('00:30:00')">30m</button>
                <button type="button" class="chip" onclick="setDirectDuration('01:00:00')">1h</button>
                <button type="button" class="chip" onclick="setDirectDuration('01:30:00')">1h 30m</button>
                <button type="button" class="chip" onclick="setDirectDuration('02:00:00')">2h</button>
                <button type="button" class="chip" onclick="setDirectDuration('03:00:00')">3h</button>
                <button type="button" class="chip" onclick="setDirectDuration('04:00:00')">4h</button>
              </div>
            </div>

            <div class="form-group">
              <label>
                <span>🌍 País VPN para esta grabación</span>
                <span style="font-weight:400;font-size:11.5px;color:#6ee7b7">Vuelve al país previo al acabar</span>
              </label>
              <select id="rVpnCountry">
                <option value="">🛡️ Usar país actual de la VPN (Sin cambiar)</option>
              </select>
              <div class="preset-chips">
                <button type="button" class="chip" onclick="document.getElementById('rVpnCountry').value='Spain'">🇪🇸 España</button>
                <button type="button" class="chip" onclick="document.getElementById('rVpnCountry').value='Japan'">🇯🇵 Japón</button>
                <button type="button" class="chip" onclick="document.getElementById('rVpnCountry').value='Switzerland'">🇨🇭 Suiza</button>
                <button type="button" class="chip" onclick="document.getElementById('rVpnCountry').value='Netherlands'">🇳🇱 Países Bajos</button>
                <button type="button" class="chip" onclick="document.getElementById('rVpnCountry').value=''">🛡️ Actual</button>
              </div>
            </div>
          </div>

          <div class="form-row">
            <div class="form-group">
              <label>
                <span>📝 Nombre del archivo (sin extensión)</span>
                <span style="cursor:pointer;color:#60a5fa;font-size:12px;" onclick="refreshDefaultFilename()">🔄 Actualizar hora</span>
              </label>
              <input type="text" id="filename" value="grabacion_NOWTS" required>
            </div>

            <div class="form-group">
              <label><span>📦 Contenedor de salida</span></label>
              <select id="format">
                <option value="mp4">MP4 (.mp4) — Estándar universal</option>
                <option value="mkv">MKV (.mkv) — Soporta subtítulos DVB y multipista</option>
                <option value="ts">MPEG-TS (.ts) — Volcado puro de transporte</option>
                <option value="avi">AVI (.avi)</option>
                <option value="webm">WebM (.webm)</option>
              </select>
            </div>
          </div>

          <div class="btn-group">
            <button type="submit" class="btn-primary" style="flex:2;">▶️ Iniciar Grabación Ahora</button>
            <button type="button" class="btn-secondary" style="flex:1;" onclick="stopRecord()">⏹️ Detener</button>
          </div>
        </form>

        <a id="download" class="download-banner" style="display:none;"></a>
      </div>

      <div class="panel-card" style="margin-bottom:0;">
        <div class="card-header" style="margin-bottom:12px;padding-bottom:10px;">
          <div class="card-title" style="font-size:14px;">🖥️ Consola de eventos</div>
          <button type="button" class="btn-secondary btn-sm" onclick="document.getElementById('log').innerHTML=''">🧹 Limpiar</button>
        </div>
        <div id="log" class="log-box"></div>
      </div>
    </div>

    <!-- TAB 2: PROGRAMAR GRABACIÓN -->
    <div id="scheduleView" style="display:none;">
      <div class="panel-card">
        <div class="card-header">
          <div class="card-title">📅 Programar nueva grabación</div>
          <span style="font-size:12px;color:#93c5fd;">🌙 Compatible con cambios de horario Verano ↔ Invierno &amp; Cambio de país VPN</span>
        </div>

        <form id="scheduleForm">
          <div class="form-group">
            <label><span>🔗 Enlace del Stream (M3U8 / TS / HTTP)</span></label>
            <input type="url" id="sUrl" placeholder="https://ejemplo.com/live/canal.m3u8" required>
          </div>

          <div class="form-row">
            <div class="form-group">
              <label>
                <span>🕐 Fecha y hora de Inicio</span>
                <span id="sStartTzBadge" class="badge-tz"></span>
              </label>
              <input type="datetime-local" id="sStart" required onchange="onScheduleDatesChanged()" oninput="onScheduleDatesChanged()">
              <div class="preset-chips">
                <button type="button" class="chip" onclick="setScheduleStartOffset(2)">En 2 min</button>
                <button type="button" class="chip" onclick="setScheduleStartOffset(15)">En 15 min</button>
                <button type="button" class="chip" onclick="setScheduleStartOffset(60)">En 1h</button>
              </div>
              <div id="sStartAmbiguousBox" class="dst-fold-box" style="display:none;">
                <div>⚠️ <strong>Cambio de hora:</strong> Esta hora ocurre dos veces esta noche. ¿Cuál quieres usar?</div>
                <div class="dst-fold-options">
                  <button type="button" id="sStartFold0" class="fold-btn active" onclick="setFold('start', 0)">☀️ 1ª vez · Horario verano (UTC+2)</button>
                  <button type="button" id="sStartFold1" class="fold-btn" onclick="setFold('start', 1)">❄️ 2ª vez · Horario invierno (UTC+1)</button>
                </div>
              </div>
            </div>

            <div class="form-group">
              <label>
                <span>🕑 Fecha y hora de Fin</span>
                <span id="sEndTzBadge" class="badge-tz"></span>
              </label>
              <input type="datetime-local" id="sEnd" required onchange="onScheduleDatesChanged()" oninput="onScheduleDatesChanged()">
              <div class="preset-chips">
                <button type="button" class="chip" onclick="addScheduleDuration(30)">+30m reales</button>
                <button type="button" class="chip" onclick="addScheduleDuration(60)">+1h real</button>
                <button type="button" class="chip" onclick="addScheduleDuration(90)">+1h 30m</button>
                <button type="button" class="chip" onclick="addScheduleDuration(120)">+2h reales</button>
                <button type="button" class="chip" onclick="addScheduleDuration(180)">+3h reales</button>
                <button type="button" class="chip" onclick="addScheduleDuration(240)">+4h reales</button>
              </div>
              <div id="sEndAmbiguousBox" class="dst-fold-box" style="display:none;">
                <div>⚠️ <strong>Cambio de hora:</strong> Esta hora ocurre dos veces esta noche. ¿Cuál quieres usar?</div>
                <div class="dst-fold-options">
                  <button type="button" id="sEndFold0" class="fold-btn active" onclick="setFold('end', 0)">☀️ 1ª vez · Horario verano (UTC+2)</button>
                  <button type="button" id="sEndFold1" class="fold-btn" onclick="setFold('end', 1)">❄️ 2ª vez · Horario invierno (UTC+1)</button>
                </div>
              </div>
            </div>
          </div>

          <!-- Resumen en vivo de duración y cambio de hora -->
          <div id="schedulePreview" class="schedule-preview">
            <div id="schedulePreviewText">Selecciona inicio y fin para calcular la duración real.</div>
            <div id="schedulePreviewDuration" class="mono-input" style="font-weight:700;color:#60a5fa;font-size:15px;">--:--:--</div>
          </div>

          <div class="form-row-3">
            <div class="form-group">
              <label><span>📝 Nombre del archivo</span></label>
              <input type="text" id="sFilename" required>
            </div>
            <div class="form-group">
              <label><span>📦 Contenedor</span></label>
              <select id="sFormat">
                <option value="mp4">MP4 (.mp4)</option>
                <option value="mkv">MKV (.mkv)</option>
                <option value="ts">MPEG-TS (.ts)</option>
                <option value="avi">AVI (.avi)</option>
                <option value="webm">WebM (.webm)</option>
              </select>
            </div>
            <div class="form-group">
              <label>
                <span>🌍 País VPN para esta tarea</span>
                <span style="font-weight:400;font-size:11px;color:#6ee7b7">Auto-restaura al acabar</span>
              </label>
              <select id="sVpnCountry">
                <option value="">🛡️ Mantener país actual</option>
              </select>
            </div>
          </div>

          <div class="btn-group">
            <button type="submit" class="btn-primary" style="width:100%;">📅 Guardar programación</button>
          </div>
        </form>
      </div>

      <div class="panel-card" style="margin-bottom:0;">
        <div class="card-header">
          <div class="card-title">📋 Cola de grabaciones programadas</div>
          <div style="display:flex;gap:8px;">
            <button class="btn-secondary btn-sm" onclick="loadSchedules()">🔄 Actualizar</button>
            <button class="btn-danger btn-sm" onclick="clearSchedules()">🗑️ Limpiar finalizadas</button>
          </div>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Archivo / VPN</th>
                <th>Inicio programado</th>
                <th>Fin programado</th>
                <th>Duración real</th>
                <th>Fmt</th>
                <th>Estado</th>
                <th>Cuenta atrás / Detalle</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody id="scheduleBody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 3: CANALES M3U -->
    <div id="channelsView" style="display:none;">
      <div class="panel-card" style="margin-bottom:0;">
        <div class="card-header">
          <div>
            <div class="card-title">📺 Explorador de Canales M3U</div>
            <div style="font-size:12.5px;color:var(--text-secondary);margin-top:4px;" id="m3uStatusInfo">STATUS_M3U</div>
          </div>
          <button class="btn-secondary btn-sm" onclick="reloadM3uServer()">🔄 Recargar archivo lista.m3u</button>
        </div>

        <div class="search-bar">
          <input type="text" id="searchInput" placeholder="🔍 Escribe el nombre del canal (ej. La 1, Movistar, DAZN, Eurosport...)" oninput="debouncedSearch()" onkeypress="if(event.key === 'Enter') searchChannels()">
          <select id="groupFilter" onchange="searchChannels()">
            <option value="">📂 Todas las categorías</option>
          </select>
          <button class="btn-primary" onclick="searchChannels()">Buscar</button>
        </div>

        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:12.5px;color:var(--text-secondary);">
          <span>Selecciona un canal para copiar su enlace o enviarlo directamente al grabador.</span>
          <span id="resultsInfo"></span>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Canal</th>
                <th>Categoría</th>
                <th style="width:270px;text-align:right;">Acciones rápidas</th>
              </tr>
            </thead>
            <tbody id="channelsBody">
              <tr><td colspan="3" style="text-align:center;color:var(--text-muted);padding:36px;">Escribe para buscar o selecciona una categoría</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 4: HISTORIAL -->
    <div id="historyView" style="display:none;">
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-label">Total grabaciones</div>
          <div class="stat-value" id="statTotal">0</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Completadas con éxito</div>
          <div class="stat-value" style="color:var(--accent-emerald);" id="statOk">0</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Fallidas / Interrumpidas</div>
          <div class="stat-value" style="color:var(--accent-rose);" id="statFailed">0</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Espacio ocupado</div>
          <div class="stat-value" style="color:#60a5fa;" id="statSize">0 B</div>
        </div>
      </div>

      <div class="panel-card" style="margin-bottom:0;">
        <div class="card-header">
          <div class="card-title">📜 Histórico de grabaciones</div>
          <div style="display:flex;gap:8px;">
            <button class="btn-secondary btn-sm" onclick="loadHistory()">🔄 Recargar</button>
            <button class="btn-danger btn-sm" onclick="clearHistory()">🗑️ Vaciar registro</button>
          </div>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Archivo / VPN</th>
                <th>Modo</th>
                <th>Inicio real</th>
                <th>Dur. pedida</th>
                <th>Dur. grabada</th>
                <th>Tamaño</th>
                <th>Estado</th>
                <th style="text-align:right;">Acciones</th>
              </tr>
            </thead>
            <tbody id="historyBody"></tbody>
          </table>
        </div>
      </div>
    </div>

  </div>
</div>

<div class="toast-container" id="toastContainer"></div>

<!-- Modal Modo Cine con Reproductor Pro y Selector Multipista de Audio -->
<div class="player-modal-backdrop" id="playerModalBackdrop" onclick="if(event.target===this) closeVideoPlayer()">
  <div class="player-modal">
    <div class="player-modal-header">
      <div class="player-modal-title">
        <span>🎬</span>
        <span class="fname" id="playerModalTitle">Reproductor</span>
        <span class="badge-tz" id="playerCodecBadge" style="display:none;"></span>
      </div>
      <button type="button" class="player-close-btn" onclick="closeVideoPlayer()">✕ Cerrar (Esc)</button>
    </div>

    <div class="video-stage" id="videoStage" onmousemove="onPlayerMouseMove()" ontouchstart="onPlayerMouseMove()" ondblclick="togglePlayerFullscreen()">
      <video id="proVideo" playsinline preload="metadata" onclick="togglePlayPause()"></video>

      <div class="v-controls" id="vControls" onclick="event.stopPropagation()" ondblclick="event.stopPropagation()">
        <div class="v-timeline" id="vTimeline" onmousemove="onTimelineHover(event)" onclick="onTimelineSeek(event)">
          <div class="v-timeline-fill" id="vTimelineFill">
            <div class="v-timeline-thumb"></div>
          </div>
          <div class="v-timeline-tooltip" id="vTimelineTooltip">00:00:00</div>
        </div>

        <div class="v-bar-row">
          <div class="v-bar-left">
            <button type="button" class="v-btn" id="vPlayBtn" onclick="togglePlayPause()" title="Play / Pausa (Espacio)">▶️</button>
            <button type="button" class="v-btn" onclick="skipPlayerTime(-10)" title="Retroceder 10s (←)">⏪ 10s</button>
            <button type="button" class="v-btn" onclick="skipPlayerTime(10)" title="Adelantar 10s (→)">10s ⏩</button>
            <button type="button" class="v-btn" id="vMuteBtn" onclick="togglePlayerMute()" title="Silenciar (M)">🔊</button>
            <input type="range" class="v-vol-slider" id="vVolSlider" min="0" max="1" step="0.05" value="1" oninput="onVolumeChange(this.value)">
            <span class="v-time" id="vTimeLabel">00:00:00 / 00:00:00</span>
          </div>

          <div class="v-bar-right">
            <!-- Selector integrado de Pista de Audio -->
            <div class="v-menu-wrap">
              <button type="button" class="v-btn" id="vAudioBtn" onclick="togglePlayerPopup('vAudioMenu')" title="Seleccionar pista de audio">
                🎧 <span id="vAudioBtnText">Audio</span>
              </button>
              <div class="v-popup-menu" id="vAudioMenu">
                <div class="v-popup-title">🎧 Pistas de audio</div>
                <div id="vAudioMenuItems"></div>
              </div>
            </div>

            <!-- Selector integrado de Velocidad -->
            <div class="v-menu-wrap">
              <button type="button" class="v-btn" id="vSpeedBtn" onclick="togglePlayerPopup('vSpeedMenu')" title="Velocidad de reproducción">
                ⚡ <span id="vSpeedBtnText">1x</span>
              </button>
              <div class="v-popup-menu" id="vSpeedMenu" style="min-width:130px;">
                <div class="v-popup-title">⚡ Velocidad</div>
                <button type="button" class="v-menu-item" onclick="setPlayerSpeed(0.75)"><span>0.75x</span></button>
                <button type="button" class="v-menu-item active" onclick="setPlayerSpeed(1)"><span>1x (Normal)</span></button>
                <button type="button" class="v-menu-item" onclick="setPlayerSpeed(1.25)"><span>1.25x</span></button>
                <button type="button" class="v-menu-item" onclick="setPlayerSpeed(1.5)"><span>1.5x</span></button>
                <button type="button" class="v-menu-item" onclick="setPlayerSpeed(2)"><span>2x</span></button>
              </div>
            </div>

            <button type="button" class="v-btn" onclick="togglePlayerFullscreen()" title="Pantalla completa (F)">⛶</button>
          </div>
        </div>
      </div>
    </div>

    <div class="player-modal-footer">
      <div id="playerModeInfo">⚡ Streaming directo desde disco (0% CPU · 0 bytes extra)</div>
      <div class="player-footer-actions">
        <a id="playerVlcBtn" href="#" class="btn-warning btn-sm" style="text-decoration:none;">🟠 Abrir en VLC</a>
        <button type="button" class="btn-secondary btn-sm" onclick="copyCurrentStreamUrl()">🔗 Copiar URL Stream</button>
        <a id="playerDownloadBtn" href="#" class="btn-success btn-sm" style="text-decoration:none;">⬇️ Descargar</a>
      </div>
    </div>
  </div>
</div>

<script>
// ── Utilidades generales y Toasts ──────────────────────────────────
function showToast(msg, type = 'info') {
  const c = document.getElementById('toastContainer');
  const t = document.createElement('div');
  t.className = 'toast';
  if (type === 'success') t.style.borderColor = '#10b981';
  if (type === 'error')   t.style.borderColor = '#f43f5e';
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.remove(); }, 3500);
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function pad2(n) { return String(n).padStart(2, '0'); }

function secsToHHMMSS(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  return pad2(Math.floor(s / 3600)) + ':' + pad2(Math.floor((s % 3600) / 60)) + ':' + pad2(s % 60);
}

function formatBytes(bytes) {
  let b = Number(bytes) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return b.toFixed(i === 0 ? 0 : 2) + ' ' + units[i];
}

// ── Reloj en vivo con indicador de zona horaria (Verano/Invierno) ──
function updateClock() {
  const now = new Date();
  const offsetMin = -now.getTimezoneOffset();
  const sign = offsetMin >= 0 ? '+' : '-';
  const absH = Math.floor(Math.abs(offsetMin) / 60);
  const tzLabel = `UTC${sign}${absH}`;
  document.getElementById('serverClock').textContent =
    now.toLocaleTimeString('es-ES', { hour12: false }) + ' · ' + tzLabel;
}
setInterval(updateClock, 1000);
updateClock();

// ── Carga y Control de Países VPN (Gluetun) ────────────────────────
async function initVpnCountries() {
  try {
    const res = await fetch('/api/vpn/countries');
    const data = await res.json();
    const selects = [
      { el: document.getElementById('topbarVpnSelect'), defaultLabel: '🌍 País...' },
      { el: document.getElementById('rVpnCountry'),     defaultLabel: '🛡️ Usar país actual de la VPN (Sin cambiar)' },
      { el: document.getElementById('sVpnCountry'),     defaultLabel: '🛡️ Mantener país actual de la VPN' }
    ];

    selects.forEach(({ el, defaultLabel }) => {
      if (!el) return;
      el.innerHTML = `<option value="">${defaultLabel}</option>`;
      const grpFav = document.createElement('optgroup');
      grpFav.label = '⭐ Países frecuentes';
      (data.featured || []).forEach(item => {
        const opt = document.createElement('option');
        opt.value = item.value;
        opt.textContent = item.label;
        grpFav.appendChild(opt);
      });
      el.appendChild(grpFav);

      if ((data.others || []).length > 0) {
        const grpAll = document.createElement('optgroup');
        grpAll.label = '🌐 Todos los países ProtonVPN';
        data.others.forEach(c => {
          const opt = document.createElement('option');
          opt.value = c;
          opt.textContent = c;
          grpAll.appendChild(opt);
        });
        el.appendChild(grpAll);
      }
    });
  } catch (e) {}
}
initVpnCountries();

async function changeGlobalVpnCountry(country) {
  const sel = document.getElementById('topbarVpnSelect');
  if (!country) return;
  const vDot = document.getElementById('vpnDot');
  const vTxt = document.getElementById('vpnStatusText');
  vDot.className = 'dot switching';
  vTxt.textContent = `⏳ Conectando a ${country}...`;
  showToast(`Cambiando VPN a ${country}... (tarda ~5-8s)`, 'info');

  try {
    const fd = new FormData();
    fd.append('country', country);
    const res = await fetch('/api/vpn/country', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) {
      showToast('❌ ' + data.error, 'error');
    } else {
      showToast('✅ ' + (data.message || `Conectado a ${country}`), 'success');
      log('🌍 VPN: ' + (data.message || country));
    }
  } catch (e) {
    showToast('Error al cambiar el país de la VPN', 'error');
  } finally {
    sel.value = '';
    checkServerStatus();
  }
}

// ── Gestión de Pestañas ────────────────────────────────────────────
function showTab(tab) {
  ['record', 'schedule', 'channels', 'history'].forEach(t => {
    document.getElementById(t + 'View').style.display = t === tab ? 'block' : 'none';
    const id = 'tab' + t.charAt(0).toUpperCase() + t.slice(1);
    const btn = document.getElementById(id);
    if (btn) btn.classList.toggle('active', t === tab);
  });
  if (window.innerWidth <= 768) {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  if (tab === 'history')  loadHistory();
  if (tab === 'schedule') { initScheduleDefaults(); loadSchedules(); }
  if (tab === 'channels') {
    loadChannelGroups();
    if (document.getElementById('searchInput').value === '' && document.getElementById('channelsBody').children.length <= 1) {
      searchChannels();
    }
  }
}

function log(msg) {
  const d = document.getElementById('log');
  const time = new Date().toLocaleTimeString('es-ES', { hour12: false });
  d.innerHTML += `<span style="color:#64748b">[${time}]</span> ${escapeHtml(msg)}<br>`;
  d.scrollTop = d.scrollHeight;
}

// ── Motor de Fechas y Cambios de Hora (DST Verano/Invierno) ────────
let scheduleFolds = { start: 0, end: 0 };

function parseLocalCandidates(localStr) {
  if (!localStr || !localStr.includes('T')) return [];
  const [dPart, tPart] = localStr.split('T');
  const [Y, M, D] = dPart.split('-').map(Number);
  const [h, m] = tPart.split(':').map(Number);

  const base = new Date(Y, M - 1, D, h, m, 0, 0).getTime();
  const candidates = [];
  [-3600000, 0, 3600000].forEach(delta => {
    const dt = new Date(base + delta);
    if (
      dt.getFullYear() === Y &&
      dt.getMonth() === M - 1 &&
      dt.getDate() === D &&
      dt.getHours() === h &&
      dt.getMinutes() === m
    ) {
      if (!candidates.some(c => c.getTime() === dt.getTime())) {
        candidates.push(dt);
      }
    }
  });
  candidates.sort((a, b) => a.getTime() - b.getTime());
  return candidates;
}

function toIsoWithOffset(dateObj) {
  const tzOffsetMin = -dateObj.getTimezoneOffset();
  const sign = tzOffsetMin >= 0 ? '+' : '-';
  const pad = n => String(Math.floor(Math.abs(n))).padStart(2, '0');
  return dateObj.getFullYear() +
    '-' + pad(dateObj.getMonth() + 1) +
    '-' + pad(dateObj.getDate()) +
    'T' + pad(dateObj.getHours()) +
    ':' + pad(dateObj.getMinutes()) +
    ':' + pad(dateObj.getSeconds()) +
    sign + pad(tzOffsetMin / 60) + ':' + pad(tzOffsetMin % 60);
}

function formatOffsetBadge(dateObj) {
  if (!dateObj || isNaN(dateObj.getTime())) return '';
  const offH = -dateObj.getTimezoneOffset() / 60;
  const sign = offH >= 0 ? '+' : '';
  const season = offH === 2 ? 'Horario verano' : (offH === 1 ? 'Horario invierno' : '');
  return `UTC${sign}${offH}${season ? ' · ' + season : ''}`;
}

function setFold(which, foldVal) {
  scheduleFolds[which] = foldVal;
  document.getElementById(which === 'start' ? 'sStartFold0' : 'sEndFold0').classList.toggle('active', foldVal === 0);
  document.getElementById(which === 'start' ? 'sStartFold1' : 'sEndFold1').classList.toggle('active', foldVal === 1);
  onScheduleDatesChanged();
}

function resolveInputDate(localStr, which) {
  const cands = parseLocalCandidates(localStr);
  const ambigBox = document.getElementById(which === 'start' ? 'sStartAmbiguousBox' : 'sEndAmbiguousBox');
  if (cands.length > 1) {
    ambigBox.style.display = 'block';
    const idx = scheduleFolds[which] === 1 ? 1 : 0;
    return { date: cands[idx], ambiguous: true, candidates: cands };
  } else {
    ambigBox.style.display = 'none';
    if (cands.length === 1) {
      return { date: cands[0], ambiguous: false, candidates: cands };
    }
    const fallback = new Date(localStr);
    return { date: fallback, ambiguous: false, candidates: [fallback] };
  }
}

function onScheduleDatesChanged() {
  const sStartStr = document.getElementById('sStart').value;
  const sEndStr   = document.getElementById('sEnd').value;
  const preview   = document.getElementById('schedulePreview');
  const pText     = document.getElementById('schedulePreviewText');
  const pDur      = document.getElementById('schedulePreviewDuration');

  if (!sStartStr || !sEndStr) {
    pText.textContent = 'Selecciona fecha de inicio y fin.';
    pDur.textContent = '--:--:--';
    return;
  }

  const startRes = resolveInputDate(sStartStr, 'start');
  let endRes     = resolveInputDate(sEndStr, 'end');

  if (endRes.ambiguous && endRes.date <= startRes.date && endRes.candidates[1] > startRes.date && scheduleFolds.end === 0) {
    scheduleFolds.end = 1;
    document.getElementById('sEndFold0').classList.remove('active');
    document.getElementById('sEndFold1').classList.add('active');
    endRes = resolveInputDate(sEndStr, 'end');
  }

  document.getElementById('sStartTzBadge').textContent = formatOffsetBadge(startRes.date);
  document.getElementById('sEndTzBadge').textContent   = formatOffsetBadge(endRes.date);

  const diffSecs = Math.round((endRes.date.getTime() - startRes.date.getTime()) / 1000);
  if (diffSecs <= 0) {
    preview.classList.remove('has-dst');
    pText.innerHTML = '<span style="color:#fda4af">⚠️ La hora de fin debe ser posterior a la hora de inicio.</span>';
    pDur.textContent = '00:00:00';
    return;
  }

  const startOff = -startRes.date.getTimezoneOffset();
  const endOff   = -endRes.date.getTimezoneOffset();
  pDur.textContent = secsToHHMMSS(diffSecs);

  if (startOff !== endOff) {
    preview.classList.add('has-dst');
    const diffHours = (startOff - endOff) / 60;
    const msgExtra = diffHours > 0
      ? `Incluye cambio al horario de invierno (+${diffHours}h real de grabación garantizada)`
      : `Incluye cambio al horario de verano (${diffHours}h real)`;
    pText.innerHTML = `<strong style="color:#fde68a">🌙 ¡Cambio de hora detectado!</strong> ${msgExtra}`;
  } else {
    preview.classList.remove('has-dst');
    pText.innerHTML = `✅ Grabación programada lista · Duración total en tiempo real:`;
  }
}

function fmtDatetimeLocal(d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
    'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

function setScheduleStartOffset(minutesFromNow) {
  const start = new Date(Date.now() + minutesFromNow * 60000);
  document.getElementById('sStart').value = fmtDatetimeLocal(start);
  onScheduleDatesChanged();
}

function addScheduleDuration(realMinutes) {
  const sStartStr = document.getElementById('sStart').value;
  const startRes = sStartStr ? resolveInputDate(sStartStr, 'start') : { date: new Date(Date.now() + 300000) };
  if (!sStartStr) {
    document.getElementById('sStart').value = fmtDatetimeLocal(startRes.date);
  }
  const targetEnd = new Date(startRes.date.getTime() + realMinutes * 60000);
  document.getElementById('sEnd').value = fmtDatetimeLocal(targetEnd);

  const endCands = parseLocalCandidates(document.getElementById('sEnd').value);
  if (endCands.length > 1) {
    const isSecond = Math.abs(endCands[1].getTime() - targetEnd.getTime()) < Math.abs(endCands[0].getTime() - targetEnd.getTime());
    setFold('end', isSecond ? 1 : 0);
  } else {
    onScheduleDatesChanged();
  }
}

// ── Buscador de Canales M3U ────────────────────────────────────────
let searchTimer = null;
function debouncedSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(searchChannels, 260);
}

async function loadChannelGroups() {
  const sel = document.getElementById('groupFilter');
  if (sel.options.length > 1) return;
  try {
    const res = await fetch('/api/channels/groups');
    const groups = await res.json();
    groups.forEach(g => {
      const opt = document.createElement('option');
      opt.value = g;
      opt.textContent = '📂 ' + g;
      sel.appendChild(opt);
    });
  } catch (e) {}
}

async function reloadM3uServer() {
  try {
    const res = await fetch('/api/channels/reload', { method: 'POST' });
    const data = await res.json();
    document.getElementById('badgeChannelsCount').textContent = data.count || 0;
    document.getElementById('m3uStatusInfo').innerHTML = data.count > 0
      ? `<strong style="color:#6ee7b7">✅ Lista recargada con ${data.count} canales.</strong>`
      : `<strong style="color:#fda4af">⚠️ No se encontró lista.m3u o está vacía.</strong>`;
    showToast(`Lista M3U actualizada (${data.count} canales)`, 'success');
    const sel = document.getElementById('groupFilter');
    sel.innerHTML = '<option value="">📂 Todas las categorías</option>';
    await loadChannelGroups();
    searchChannels();
  } catch (e) {
    showToast('Error al recargar lista M3U', 'error');
  }
}

let lastChannelsData = [];
async function searchChannels() {
  const query = document.getElementById('searchInput').value.trim();
  const group = document.getElementById('groupFilter').value;
  const body  = document.getElementById('channelsBody');
  const info  = document.getElementById('resultsInfo');

  body.innerHTML = '<tr><td colspan="3" style="text-align:center;padding:30px">⏳ Buscando canales...</td></tr>';

  try {
    const params = new URLSearchParams({ q: query, group: group });
    const res = await fetch('/api/channels?' + params.toString());
    const data = await res.json();
    lastChannelsData = data;
    body.innerHTML = '';

    if (data.length === 0) {
      body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--text-muted);padding:34px">No se encontraron canales con ese criterio</td></tr>';
      info.textContent = '0 resultados';
      return;
    }

    info.textContent = `Mostrando ${data.length} canales`;
    data.forEach((ch, idx) => {
      const tr = document.createElement('tr');
      const groupBadge = ch.group
        ? `<span class="badge badge-scheduled">${escapeHtml(ch.group)}</span>`
        : '<span style="color:var(--text-muted);font-size:12px;">General</span>';
      tr.innerHTML = `
        <td class="cell-main">
          <div style="font-weight:600;color:#f8fafc;">${escapeHtml(ch.name)}</div>
          <div style="font-size:11.5px;color:var(--text-muted);word-break:break-all;margin-top:2px;">${escapeHtml(ch.url)}</div>
        </td>
        <td data-label="Categoría">${groupBadge}</td>
        <td class="cell-actions" style="text-align:right;white-space:nowrap;">
          <button class="btn-success btn-sm" onclick="copyChannel(${idx})">📋 Copiar</button>
          <button class="btn-primary btn-sm" onclick="recordChannel(${idx})">▶️ Grabar</button>
          <button class="btn-warning btn-sm" onclick="scheduleChannel(${idx})">📅 Programar</button>
        </td>
      `;
      body.appendChild(tr);
    });
  } catch (e) {
    body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#f43f5e;padding:30px">Error de conexión</td></tr>';
  }
}

function copyChannel(idx) {
  const ch = lastChannelsData[idx];
  if (!ch) return;
  copyToClipboard(ch.url, ch.name);
}

function recordChannel(idx) {
  const ch = lastChannelsData[idx];
  if (!ch) return;
  document.getElementById('url').value = ch.url;
  document.getElementById('filename').value = formatFilenameForChannel(ch.name);
  showTab('record');
  showToast(`Canal "${ch.name}" cargado en el grabador`, 'info');
}

function scheduleChannel(idx) {
  const ch = lastChannelsData[idx];
  if (!ch) return;
  document.getElementById('sUrl').value = ch.url;
  document.getElementById('sFilename').value = 'prog_' + formatFilenameForChannel(ch.name);
  showTab('schedule');
  showToast(`Canal "${ch.name}" listo para programar`, 'info');
}

function copyToClipboard(text, label = '') {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => {
      showToast(`Enlace copiado${label ? ': ' + label : ''}`, 'success');
    }).catch(err => {
      showToast('Error al copiar: ' + err, 'error');
    });
  } else {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    try {
      document.execCommand('copy');
      showToast(`Enlace copiado${label ? ': ' + label : ''}`, 'success');
    } catch (err) {
      showToast('No se pudo copiar al portapapeles', 'error');
    }
    document.body.removeChild(textArea);
  }
}

function formatFilenameForChannel(name) {
  let safeName = name.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]/gi, '_').toLowerCase();
  safeName = safeName.replace(/_+/g, '_').replace(/^_|_$/g, '');
  const now = new Date();
  const tag = now.getFullYear() + pad2(now.getMonth() + 1) + pad2(now.getDate()) + '_' + pad2(now.getHours()) + pad2(now.getMinutes());
  return (safeName || 'canal') + '_' + tag;
}

function setDirectDuration(val) {
  document.getElementById('duration').value = val;
}

function refreshDefaultFilename() {
  const now = new Date();
  const tag = now.getFullYear() + pad2(now.getMonth() + 1) + pad2(now.getDate()) + '_' + pad2(now.getHours()) + pad2(now.getMinutes()) + pad2(now.getSeconds());
  document.getElementById('filename').value = 'grabacion_' + tag;
}

// ── Grabación Inmediata y Estado en Vivo ───────────────────────────
let wasRunning = false;
let lastLoggedVpnIp = '';

async function submitForm() {
  const fd = new FormData();
  fd.append('url',         document.getElementById('url').value.trim());
  fd.append('duration',    document.getElementById('duration').value.trim());
  fd.append('filename',    document.getElementById('filename').value.trim());
  fd.append('format',      document.getElementById('format').value);
  fd.append('vpn_country', document.getElementById('rVpnCountry').value);
  const res  = await fetch('/record', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.error) {
    log('❌ ' + data.error);
    showToast(data.error, 'error');
    return;
  }
  log('▶️ ' + (data.message || 'Grabación iniciada'));
  showToast(data.message || 'Grabación iniciada', 'success');
  checkServerStatus();
}
document.getElementById('form').onsubmit = e => { e.preventDefault(); submitForm(); };

async function stopRecord() {
  const res  = await fetch('/stop', { method: 'POST' });
  const data = await res.json();
  log('⏹️ ' + (data.message || 'Detención solicitada'));
  showToast(data.message || 'Deteniendo grabación...', 'info');
  setTimeout(checkServerStatus, 800);
}

async function checkServerStatus() {
  try {
    const res  = await fetch('/status');
    const data = await res.json();
    const banner = document.getElementById('statusBanner');
    const dot    = document.getElementById('globalDot');
    const gText  = document.getElementById('globalStateText');
    const sIcon  = document.getElementById('statusIcon');
    const sText  = document.getElementById('statusText');
    const pCont  = document.getElementById('progressContainer');
    const pBar   = document.getElementById('progressBar');
    const timer  = document.getElementById('liveTimer');
    const stopBtn = document.getElementById('globalStopBtn');

    // Actualizar Píldora VPN compacta en el Topbar y registrar IP en la consola inferior
    if (data.vpn) {
      const vDot  = document.getElementById('vpnDot');
      const vTxt  = document.getElementById('vpnStatusText');
      const vPill = document.getElementById('vpnPill');
      if (data.vpn.switching) {
        vDot.className = 'dot switching';
        vTxt.textContent = `⏳ ${data.vpn.configured_country || 'VPN'}...`;
      } else if (data.vpn.public_ip) {
        vDot.className = 'dot';
        const cName = data.vpn.configured_country || data.vpn.ip_country || 'VPN';
        vTxt.textContent = `🛡️ ${cName}`;
        vPill.title = `VPN conectada en ${cName} · IP pública: ${data.vpn.public_ip}`;
        if (data.vpn.public_ip !== lastLoggedVpnIp) {
          lastLoggedVpnIp = data.vpn.public_ip;
          log(`🛡️ VPN activa en ${cName} · IP pública: ${data.vpn.public_ip}`);
        }
      } else if (data.vpn.available) {
        vDot.className = 'dot switching';
        vTxt.textContent = `🛡️ ${data.vpn.configured_country || 'VPN'}`;
      } else {
        vDot.className = 'dot';
        vTxt.textContent = '🛡️ VPN';
      }
    }

    if (data.schedules_pending != null) {
      document.getElementById('badgeSchedulesCount').textContent = data.schedules_pending;
    }
    if (data.history_count != null) {
      document.getElementById('badgeHistoryCount').textContent = data.history_count;
    }

    sText.textContent = data.progress || 'Listo';

    if (data.running) {
      wasRunning = true;
      banner.className = 'live-banner is-recording';
      dot.className = 'dot recording';
      gText.textContent = data.mode === 'scheduled' ? 'REC · Prog' : 'REC';
      sIcon.textContent = '🔴';
      pCont.style.display = 'block';
      stopBtn.style.display = 'inline-flex';

      if (data.start_time && data.duration_requested) {
        const elapsedSec = Math.max(0, Math.round((Date.now() - new Date(data.start_time).getTime()) / 1000));
        const totalSec   = Math.max(1, Number(data.duration_requested));
        const pct        = Math.min(100, (elapsedSec / totalSec) * 100);
        pBar.style.width = pct.toFixed(1) + '%';
        timer.style.display = 'inline';
        timer.textContent = `${secsToHHMMSS(elapsedSec)} / ${secsToHHMMSS(totalSec)} (${pct.toFixed(0)}%)`;
      } else {
        timer.style.display = 'none';
      }
    } else {
      dot.className = 'dot';
      gText.textContent = 'Listo';
      pCont.style.display = 'none';
      timer.style.display = 'none';
      stopBtn.style.display = 'none';

      if (data.last_result && data.last_result.status === 'ok') {
        banner.className = 'live-banner is-success';
        sIcon.textContent = '✅';
      } else if (data.last_result && data.last_result.status === 'failed') {
        banner.className = 'live-banner is-error';
        sIcon.textContent = '❌';
      } else {
        banner.className = 'live-banner';
        sIcon.textContent = '🟢';
      }

      if (wasRunning) {
        wasRunning = false;
        if (data.last_result) {
          if (data.last_result.status === 'ok') {
            log('✅ Finalizado: ' + (data.filename || ''));
            showToast('Grabación finalizada correctamente', 'success');
          } else if (data.last_result.status === 'stopped') {
            log('⏹️ Detenida: ' + (data.filename || ''));
          } else {
            log('❌ Error: ' + (data.last_result.reason || ''));
            showToast('La grabación terminó con errores', 'error');
          }
        }
        if (data.filename) {
          const dl = document.getElementById('download');
          dl.href = '/download/' + encodeURIComponent(data.filename);
          dl.innerHTML = `<span>⬇️ Descargar última grabación: <strong>${escapeHtml(data.filename)}</strong></span><span>Descargar archivo →</span>`;
          dl.style.display = 'flex';
        }
        loadSchedules();
        loadHistory();
      }
    }
  } catch (e) {}
}

setInterval(checkServerStatus, 2000);
checkServerStatus();

// ── Historial ──────────────────────────────────────────────────────
async function loadHistory() {
  const res  = await fetch('/history');
  const data = await res.json();
  const body = document.getElementById('historyBody');
  body.innerHTML = '';

  document.getElementById('badgeHistoryCount').textContent = data.length;
  document.getElementById('statTotal').textContent = data.length;
  document.getElementById('statOk').textContent = data.filter(x => x.status === 'ok' || x.status === 'stopped').length;
  document.getElementById('statFailed').textContent = data.filter(x => x.status === 'failed').length;
  const totalBytes = data.reduce((acc, x) => acc + (Number(x.filesize_bytes) || 0), 0);
  document.getElementById('statSize').textContent = formatBytes(totalBytes);

  if (data.length === 0) {
    body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:34px">Aún no hay grabaciones registradas</td></tr>';
    return;
  }

  data.slice().reverse().forEach(item => {
    const tr = document.createElement('tr');
    const typeBadge = item.type === 'scheduled'
      ? '<span class="badge badge-scheduled">📅 Programada</span>'
      : '<span class="badge badge-immediate">▶️ Directo</span>';

    let statusBadge = '';
    if (item.status === 'ok') {
      statusBadge = '<span class="badge badge-ok">✅ Completada</span>';
    } else if (item.status === 'stopped') {
      statusBadge = '<span class="badge badge-stopped">⏹️ Detenida</span>';
    } else {
      statusBadge = `<span class="badge badge-failed" title="${escapeHtml(item.status_reason)}">❌ Fallida</span>`;
      if (item.status_reason) {
        statusBadge += `<div style="font-size:11px;color:#fda4af;margin-top:3px;">${escapeHtml(item.status_reason)}</div>`;
      }
    }

    const realDur = item.media_duration_seconds != null
      ? secsToHHMMSS(item.media_duration_seconds)
      : (item.duration_seconds != null ? secsToHHMMSS(item.duration_seconds) : '--:--:--');

    const hasFile = (Number(item.filesize_bytes) || 0) > 0;
    const playBtn = hasFile
      ? `<button class="btn-primary btn-sm" title="Ver grabación aquí" onclick="openVideoPlayer('${escapeHtml(item.filename)}')">▶️ Ver</button> `
      : '';
    const dlBtn = hasFile
      ? `<a href="/download/${encodeURIComponent(item.filename)}" class="btn-success btn-sm" style="text-decoration:none;display:inline-flex;align-items:center;" title="Descargar">⬇️</a> `
      : '';

    const vpnTag = item.vpn_country
      ? `<span class="badge-vpn">🌍 ${escapeHtml(item.vpn_country)}</span>`
      : '';

    tr.innerHTML = `
      <td class="cell-main">
        <div style="font-weight:600;color:#f8fafc;">${escapeHtml(item.filename)}</div>
        <div style="display:flex;gap:6px;align-items:center;margin-top:3px;">
          <span style="font-size:11px;color:var(--text-muted);">${escapeHtml((item.format || '').toUpperCase())}</span>
          ${vpnTag}
        </div>
      </td>
      <td data-label="Modo">${typeBadge}</td>
      <td data-label="Inicio" style="font-size:12.5px;">${new Date(item.start).toLocaleString('es-ES')}</td>
      <td data-label="Dur. pedida" class="mono-input" style="font-size:12.5px;">${escapeHtml(item.duration_requested || '')}</td>
      <td data-label="Dur. grabada" class="mono-input" style="font-size:12.5px;">${realDur}</td>
      <td data-label="Tamaño" class="mono-input" style="font-size:12.5px;">${escapeHtml(item.filesize_human || '0 B')}</td>
      <td data-label="Estado">${statusBadge}</td>
      <td class="cell-actions" style="text-align:right;white-space:nowrap;">
        ${playBtn}${dlBtn}
        <button class="btn-danger btn-sm" title="Eliminar grabación" onclick="deleteRecording('${escapeHtml(item.filename)}')">🗑️</button>
      </td>
    `;
    body.appendChild(tr);
  });
}

async function deleteRecording(filename) {
  if (!confirm(`¿Eliminar el archivo "${filename}" y su entrada del historial?`)) return;
  const res  = await fetch('/history/delete/' + encodeURIComponent(filename), { method: 'POST' });
  const data = await res.json();
  showToast(data.message || 'Grabación eliminada', 'info');
  loadHistory();
}

async function clearHistory() {
  if (!confirm('¿Vaciar el registro del historial?')) return;
  const res  = await fetch('/history/clear', { method: 'POST' });
  const data = await res.json();
  showToast(data.message || 'Historial vaciado', 'info');
  loadHistory();
}

// ── Programador ────────────────────────────────────────────────────
function initScheduleDefaults() {
  const now   = new Date();
  const start = new Date(now.getTime() + 5 * 60 * 1000);
  const end   = new Date(start.getTime() + 60 * 60 * 1000);

  if (!document.getElementById('sStart').value) document.getElementById('sStart').value = fmtDatetimeLocal(start);
  if (!document.getElementById('sEnd').value)   document.getElementById('sEnd').value   = fmtDatetimeLocal(end);
  if (!document.getElementById('sFilename').value) {
    const tag = start.getFullYear() + pad2(start.getMonth() + 1) + pad2(start.getDate()) +
      '_' + pad2(start.getHours()) + pad2(start.getMinutes());
    document.getElementById('sFilename').value = 'prog_' + tag;
  }
  onScheduleDatesChanged();
}

document.getElementById('scheduleForm').onsubmit = async e => {
  e.preventDefault();
  const sStart = document.getElementById('sStart').value;
  const sEnd   = document.getElementById('sEnd').value;
  if (!sStart || !sEnd) { showToast('Completa las fechas de inicio y fin', 'error'); return; }

  const startRes = resolveInputDate(sStart, 'start');
  const endRes   = resolveInputDate(sEnd, 'end');

  if (endRes.date <= startRes.date) {
    showToast('La hora de fin debe ser posterior a la de inicio', 'error');
    return;
  }
  if (startRes.date < new Date(Date.now() - 30000)) {
    if (!confirm('⚠️ La hora de inicio ya ha pasado (comenzará inmediatamente). ¿Programar de todos modos?')) return;
  }

  const fd = new FormData();
  fd.append('url',         document.getElementById('sUrl').value.trim());
  fd.append('start',       toIsoWithOffset(startRes.date));
  fd.append('end',         toIsoWithOffset(endRes.date));
  fd.append('start_fold',  scheduleFolds.start);
  fd.append('end_fold',    scheduleFolds.end);
  fd.append('filename',    document.getElementById('sFilename').value.trim());
  fd.append('format',      document.getElementById('sFormat').value);
  fd.append('vpn_country', document.getElementById('sVpnCountry').value);

  const res  = await fetch('/schedule', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.error) {
    showToast(data.error, 'error');
    return;
  }
  showToast(data.message || 'Grabación programada con éxito', 'success');
  document.getElementById('sStart').value = '';
  document.getElementById('sEnd').value = '';
  document.getElementById('sFilename').value = '';
  document.getElementById('sVpnCountry').value = '';
  scheduleFolds = { start: 0, end: 0 };
  initScheduleDefaults();
  loadSchedules();
};

function formatDateTimeWithTz(isoStr) {
  const dt = new Date(isoStr);
  const str = dt.toLocaleString('es-ES', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
  });
  const m = String(isoStr).match(/([+-]\d{2}:\d{2})$/);
  const tzTag = m ? ` <span class="badge-tz">UTC${m[1].replace(':00', '')}</span>` : '';
  return str + tzTag;
}

async function loadSchedules() {
  const res  = await fetch('/schedules');
  const data = await res.json();
  const body = document.getElementById('scheduleBody');
  body.innerHTML = '';

  const pendingCount = data.filter(j => j.status === 'pending' || j.status === 'running').length;
  document.getElementById('badgeSchedulesCount').textContent = pendingCount;

  if (data.length === 0) {
    body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:34px">No hay grabaciones programadas</td></tr>';
    return;
  }

  data.slice().reverse().forEach(job => {
    const tr = document.createElement('tr');
    const startDt = new Date(job.start_time);
    const endDt   = new Date(job.end_time);
    const durSecs = Math.max(0, Math.round((endDt.getTime() - startDt.getTime()) / 1000));

    const badge = {
      pending:   '<span class="badge badge-pending">🕐 Pendiente</span>',
      running:   '<span class="badge badge-running">🔴 Grabando</span>',
      ok:        '<span class="badge badge-ok">✅ Completada</span>',
      stopped:   '<span class="badge badge-stopped">⏹️ Detenida</span>',
      failed:    '<span class="badge badge-failed">❌ Fallida</span>',
      cancelled: '<span class="badge badge-cancelled">🚫 Cancelada</span>',
    }[job.status] || `<span class="badge">${escapeHtml(job.status)}</span>`;

    let actions = '';
    if (job.status === 'pending') {
      actions += `<button class="btn-warning btn-sm" title="Cancelar programación" onclick="cancelSchedule('${job.id}')">🚫 Cancelar</button> `;
    } else if (job.status === 'running') {
      actions += `<button class="btn-danger btn-sm" title="Detener grabación en curso" onclick="stopRecord()">⏹️ Detener</button> `;
    } else {
      if (job.status === 'ok' || job.status === 'stopped') {
        actions += `<button class="btn-primary btn-sm" title="Ver grabación" onclick="openVideoPlayer('${escapeHtml(job.filename)}')">▶️ Ver</button> `;
        actions += `<a href="/download/${encodeURIComponent(job.filename)}" class="btn-success btn-sm" style="text-decoration:none;display:inline-flex;align-items:center;" title="Descargar">⬇️</a> `;
      }
      actions += `<button class="btn-danger btn-sm" title="Borrar registro" onclick="deleteSchedule('${job.id}')">🗑️</button>`;
    }

    let infoCell = '';
    if (job.status === 'pending') {
      infoCell = `<span class="countdown" data-start="${escapeHtml(job.start_time)}">...</span>`;
    } else if (job.status === 'running') {
      infoCell = `<span class="countdown" style="color:#6ee7b7;" data-end="${escapeHtml(job.end_time)}">En curso...</span>`;
    } else if (job.status === 'ok' || job.status === 'stopped') {
      infoCell = `<span style="color:#a7f3d0;font-size:12px;">${escapeHtml(job.filesize_human || 'Finalizada')}</span>`;
    } else if (job.status === 'failed' && job.status_reason) {
      infoCell = `<span style="color:#fda4af;font-size:12px;">${escapeHtml(job.status_reason)}</span>`;
    }

    const vpnTag = job.vpn_country
      ? `<div style="margin-top:3px;"><span class="badge-vpn">🌍 VPN: ${escapeHtml(job.vpn_country)}</span></div>`
      : `<div style="margin-top:3px;font-size:11px;color:var(--text-muted);">🛡️ VPN actual</div>`;

    tr.innerHTML = `
      <td class="cell-main">
        <strong>${escapeHtml(job.filename)}</strong>
        ${vpnTag}
      </td>
      <td data-label="Inicio" style="font-size:12.5px;">${formatDateTimeWithTz(job.start_time)}</td>
      <td data-label="Fin" style="font-size:12.5px;">${formatDateTimeWithTz(job.end_time)}</td>
      <td data-label="Duración" class="mono-input" style="font-size:12.5px;">${secsToHHMMSS(durSecs)}</td>
      <td data-label="Formato"><span class="badge-tz">${escapeHtml((job.format || '').toUpperCase())}</span></td>
      <td data-label="Estado">${badge}</td>
      <td data-label="Detalle">${infoCell}</td>
      <td class="cell-actions" style="white-space:nowrap;text-align:right;">${actions}</td>
    `;
    body.appendChild(tr);
  });
  updateCountdowns();
}

function updateCountdowns() {
  const now = Date.now();
  document.querySelectorAll('.countdown[data-start]').forEach(el => {
    const diff = Math.round((new Date(el.dataset.start).getTime() - now) / 1000);
    el.textContent = diff > 0 ? '⏳ En ' + secsToHHMMSS(diff) : '🔜 Iniciando...';
  });
  document.querySelectorAll('.countdown[data-end]').forEach(el => {
    const diff = Math.round((new Date(el.dataset.end).getTime() - now) / 1000);
    el.textContent = diff > 0 ? '⏺ Restan ' + secsToHHMMSS(diff) : '🏁 Finalizando...';
  });
}
setInterval(updateCountdowns, 1000);

setInterval(() => {
  if (document.getElementById('scheduleView').style.display !== 'none') loadSchedules();
}, 6000);

async function cancelSchedule(id) {
  if (!confirm('¿Cancelar esta grabación programada?')) return;
  await fetch('/schedules/cancel/' + id, { method: 'POST' });
  showToast('Programación cancelada', 'info');
  loadSchedules();
}

async function deleteSchedule(id) {
  await fetch('/schedules/delete/' + id, { method: 'POST' });
  showToast('Registro eliminado', 'info');
  loadSchedules();
}

async function clearSchedules() {
  if (!confirm('¿Limpiar de la lista todas las programaciones ya finalizadas/canceladas?')) return;
  await fetch('/schedules/clear', { method: 'POST' });
  showToast('Programaciones antiguas limpiadas', 'info');
  loadSchedules();
}

// ── Reproductor de Vídeo Modal "Modo Cine" + Multipista de Audio ───
let pState = {
  open: false,
  filename: '',
  duration: 0,
  audioTracks: [],
  activeTrackIdx: 0,
  isPipeMode: false,
  pipeStartOffset: 0,
  hideTimer: null
};

const proVideo = document.getElementById('proVideo');

async function openVideoPlayer(filename) {
  pState.open = true;
  pState.filename = filename;
  pState.duration = 0;
  pState.audioTracks = [];
  pState.activeTrackIdx = 0;
  pState.isPipeMode = filename.toLowerCase().endsWith('.ts');
  pState.pipeStartOffset = 0;

  document.getElementById('playerModalTitle').textContent = filename;
  document.getElementById('playerCodecBadge').style.display = 'none';
  document.getElementById('playerModalBackdrop').classList.add('open');

  const directUrl = window.location.origin + '/stream/' + encodeURIComponent(filename);
  document.getElementById('playerVlcBtn').href = 'vlc://' + directUrl;
  document.getElementById('playerDownloadBtn').href = '/download/' + encodeURIComponent(filename);

  // Consultar metadatos y pistas de audio reales con ffprobe
  try {
    const res = await fetch('/api/media-info/' + encodeURIComponent(filename));
    const info = await res.json();
    pState.duration = Number(info.duration) || 0;
    pState.audioTracks = info.audio_tracks || [];

    const badge = document.getElementById('playerCodecBadge');
    const parts = [];
    if (info.video_codec) parts.push(info.video_codec.toUpperCase());
    if (pState.audioTracks.length > 0) parts.push(`${pState.audioTracks.length} audio${pState.audioTracks.length > 1 ? 's' : ''}`);
    if (parts.length > 0) {
      badge.textContent = parts.join(' · ');
      badge.style.display = 'inline-block';
    }
  } catch (e) {
    pState.audioTracks = [{ index: 0, label: 'Pista 1 · Principal' }];
  }

  renderAudioTracksMenu();
  loadPlayerSource(0, 0);
}

function renderAudioTracksMenu() {
  const container = document.getElementById('vAudioMenuItems');
  const btnText   = document.getElementById('vAudioBtnText');
  container.innerHTML = '';

  const tracks = pState.audioTracks.length > 0
    ? pState.audioTracks
    : [{ index: 0, label: 'Pista 1 · Principal' }];

  btnText.textContent = tracks.length > 1
    ? `Pista ${pState.activeTrackIdx + 1} (${tracks.length})`
    : 'Pista 1';

  tracks.forEach((tr, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'v-menu-item' + (i === pState.activeTrackIdx ? ' active' : '');
    btn.innerHTML = `<span>${escapeHtml(tr.label)}</span><span>${i === pState.activeTrackIdx ? '✓' : ''}</span>`;
    btn.onclick = () => selectAudioTrack(i);
    container.appendChild(btn);
  });
}

function loadPlayerSource(trackIdx, seekSeconds = 0) {
  pState.activeTrackIdx = trackIdx;
  const isTs = pState.filename.toLowerCase().endsWith('.ts');
  const modeInfo = document.getElementById('playerModeInfo');

  // Si es la Pista 1 (índice 0) y no es .ts, usamos lectura directa de disco (HTTP Range 206)
  // Si el usuario selecciona Pista 2, 3... (o archivo .ts), usamos tubería en memoria (-c copy pipe:1, 0 bytes en disco)
  if (trackIdx === 0 && !isTs) {
    pState.isPipeMode = false;
    pState.pipeStartOffset = 0;
    proVideo.src = '/stream/' + encodeURIComponent(pState.filename);
    proVideo.currentTime = Math.max(0, seekSeconds);
    modeInfo.textContent = '⚡ Streaming directo desde disco (0% CPU · 0 bytes extra)';
  } else {
    pState.isPipeMode = true;
    pState.pipeStartOffset = Math.max(0, seekSeconds);
    const params = new URLSearchParams({
      track: String(trackIdx),
      start: String(Math.floor(pState.pipeStartOffset))
    });
    proVideo.src = '/stream-track/' + encodeURIComponent(pState.filename) + '?' + params.toString();
    modeInfo.textContent = `🎧 Reproduciendo Pista ${trackIdx + 1} en memoria (-c copy · 0 bytes en disco)`;
  }

  renderAudioTracksMenu();
  proVideo.play().catch(() => {});
}

function getEffectiveCurrentTime() {
  if (pState.isPipeMode) {
    return pState.pipeStartOffset + (Number(proVideo.currentTime) || 0);
  }
  return Number(proVideo.currentTime) || 0;
}

function getEffectiveDuration() {
  if (pState.duration > 0) return pState.duration;
  if (proVideo.duration && isFinite(proVideo.duration)) return proVideo.duration;
  return 0;
}

function selectAudioTrack(trackIdx) {
  document.getElementById('vAudioMenu').classList.remove('open');
  if (trackIdx === pState.activeTrackIdx) return;

  const currentSec = getEffectiveCurrentTime();
  // Si el navegador soporta audioTracks nativo en modo directo, cambiar también ahí si procede
  if (!pState.isPipeMode && proVideo.audioTracks && proVideo.audioTracks.length > trackIdx) {
    for (let i = 0; i < proVideo.audioTracks.length; i++) {
      proVideo.audioTracks[i].enabled = (i === trackIdx);
    }
    pState.activeTrackIdx = trackIdx;
    renderAudioTracksMenu();
    showToast(`Cambiado a Pista ${trackIdx + 1}`, 'info');
    return;
  }

  loadPlayerSource(trackIdx, currentSec);
  showToast(`Cambiado a Pista de audio ${trackIdx + 1}`, 'info');
}

function seekPlayerTo(targetSec) {
  const dur = getEffectiveDuration();
  if (dur > 0) targetSec = Math.max(0, Math.min(dur - 1, targetSec));
  else targetSec = Math.max(0, targetSec);

  if (!pState.isPipeMode) {
    proVideo.currentTime = targetSec;
  } else {
    loadPlayerSource(pState.activeTrackIdx, targetSec);
  }
}

function skipPlayerTime(deltaSec) {
  seekPlayerTo(getEffectiveCurrentTime() + deltaSec);
}

function togglePlayPause() {
  closeAllPlayerPopups();
  if (proVideo.paused) proVideo.play().catch(() => {});
  else proVideo.pause();
}

proVideo.addEventListener('play', () => {
  document.getElementById('vPlayBtn').textContent = '⏸️';
  onPlayerMouseMove();
});
proVideo.addEventListener('pause', () => {
  document.getElementById('vPlayBtn').textContent = '▶️';
  document.getElementById('videoStage').classList.remove('hide-controls');
});
proVideo.addEventListener('timeupdate', () => {
  const cur = getEffectiveCurrentTime();
  const dur = getEffectiveDuration();
  document.getElementById('vTimeLabel').textContent = `${secsToHHMMSS(cur)} / ${secsToHHMMSS(dur)}`;
  const pct = dur > 0 ? Math.min(100, (cur / dur) * 100) : 0;
  document.getElementById('vTimelineFill').style.width = pct.toFixed(2) + '%';
});

function onTimelineHover(e) {
  const rect = document.getElementById('vTimeline').getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const dur = getEffectiveDuration();
  const tt = document.getElementById('vTimelineTooltip');
  tt.style.left = (ratio * 100) + '%';
  tt.textContent = secsToHHMMSS(ratio * dur);
}

function onTimelineSeek(e) {
  const rect = document.getElementById('vTimeline').getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const dur = getEffectiveDuration();
  if (dur > 0) seekPlayerTo(ratio * dur);
}

function togglePlayerMute() {
  proVideo.muted = !proVideo.muted;
  document.getElementById('vMuteBtn').textContent = proVideo.muted ? '🔇' : '🔊';
}

function onVolumeChange(val) {
  proVideo.volume = Number(val);
  proVideo.muted = (Number(val) === 0);
  document.getElementById('vMuteBtn').textContent = proVideo.muted ? '🔇' : '🔊';
}

function togglePlayerPopup(menuId) {
  ['vAudioMenu', 'vSpeedMenu'].forEach(id => {
    const el = document.getElementById(id);
    if (id === menuId) el.classList.toggle('open');
    else el.classList.remove('open');
  });
}

function closeAllPlayerPopups() {
  ['vAudioMenu', 'vSpeedMenu'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('open');
  });
}

function setPlayerSpeed(rate) {
  proVideo.playbackRate = rate;
  document.getElementById('vSpeedBtnText').textContent = rate + 'x';
  const menu = document.getElementById('vSpeedMenu');
  menu.querySelectorAll('.v-menu-item').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.startsWith(rate + 'x'));
  });
  menu.classList.remove('open');
}

function togglePlayerFullscreen() {
  const stage = document.getElementById('videoStage');
  if (!document.fullscreenElement) {
    if (stage.requestFullscreen) stage.requestFullscreen();
  } else {
    if (document.exitFullscreen) document.exitFullscreen();
  }
}

function onPlayerMouseMove() {
  const stage = document.getElementById('videoStage');
  stage.classList.remove('hide-controls');
  clearTimeout(pState.hideTimer);
  if (!proVideo.paused) {
    pState.hideTimer = setTimeout(() => {
      closeAllPlayerPopups();
      stage.classList.add('hide-controls');
    }, 2600);
  }
}

function copyCurrentStreamUrl() {
  const url = window.location.origin + '/stream/' + encodeURIComponent(pState.filename);
  copyToClipboard(url, pState.filename);
}

function closeVideoPlayer() {
  pState.open = false;
  clearTimeout(pState.hideTimer);
  closeAllPlayerPopups();
  proVideo.pause();
  proVideo.removeAttribute('src');
  proVideo.load();
  if (document.fullscreenElement && document.exitFullscreen) {
    document.exitFullscreen().catch(() => {});
  }
  document.getElementById('playerModalBackdrop').classList.remove('open');
}

document.addEventListener('keydown', e => {
  if (!pState.open) return;
  if (e.key === 'Escape') { closeVideoPlayer(); }
  else if (e.key === ' ' && document.activeElement.tagName !== 'BUTTON') { e.preventDefault(); togglePlayPause(); }
  else if (e.key === 'ArrowLeft')  { e.preventDefault(); skipPlayerTime(-10); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); skipPlayerTime(10); }
  else if (e.key.toLowerCase() === 'f') { e.preventDefault(); togglePlayerFullscreen(); }
  else if (e.key.toLowerCase() === 'm') { e.preventDefault(); togglePlayerMute(); }
});
</script>
</body>
</html>
'''


# ─────────────────────────────────────────────
#  Rutas Flask
# ─────────────────────────────────────────────
@app.route('/')
def index():
    now_str = now_local().strftime('%Y%m%d_%H%M%S')
    html = HTML_TEMPLATE.replace('READY_STATUS', status['progress'])
    html = html.replace('NOWTS', now_str)
    html = html.replace('CHANNELS_COUNT', str(len(channels_db)))

    if len(channels_db) > 0:
        m3u_status = f"<strong style='color:#6ee7b7'>✅ Lista cargada con {len(channels_db)} canales ({len(channels_groups)} categorías).</strong>"
    else:
        m3u_status = f"<strong style='color:#fda4af'>⚠️ No se encontró el archivo {M3U_LIST_FILE} en el servidor.</strong>"

    html = html.replace('STATUS_M3U', m3u_status)
    return html


@app.route('/api/vpn/countries')
def api_vpn_countries():
    return jsonify(vpn_countries_list)


@app.route('/api/vpn/status')
def api_vpn_status():
    return jsonify(get_gluetun_info(force=True))


@app.route('/api/vpn/country', methods=['POST'])
def api_vpn_change_country():
    if status['running']:
        return jsonify({'error': 'No se puede cambiar el país manualmente mientras hay una grabación en curso'}), 400
    country = (request.form.get('country') or '').strip()
    if not country:
        return jsonify({'error': 'País no especificado'}), 400

    ok, msg = switch_gluetun_country(country, wait_ready=True, timeout_sec=28)
    if not ok:
        return jsonify({'error': msg}), 500
    # Si el usuario lo cambia manualmente desde la barra superior, actualizar también el país por defecto
    vpn_state_cache["default_country"] = country
    return jsonify({'message': msg, 'vpn': get_gluetun_info(force=False)})


@app.route('/api/channels')
def api_channels():
    query = request.args.get('q', '').strip().lower()
    group = request.args.get('group', '').strip().lower()

    if not query and not group:
        return jsonify(channels_db[:100])

    results = []
    for c in channels_db:
        if group and c.get('group', '').lower() != group:
            continue
        if query and query not in c['name'].lower() and query not in c.get('group', '').lower():
            continue
        results.append(c)
        if len(results) >= 120:
            break
    return jsonify(results)


@app.route('/api/channels/groups')
def api_channels_groups():
    return jsonify(channels_groups)


@app.route('/api/channels/reload', methods=['POST'])
def api_channels_reload():
    count = load_m3u_list()
    return jsonify({'count': count, 'groups': len(channels_groups)})


@app.route('/record', methods=['POST'])
def record():
    if status['running']:
        return jsonify({'error': 'Ya hay una grabación en curso'}), 400
    url = request.form.get('url', '').strip()
    duration = request.form.get('duration', '').strip()
    raw_name = request.form.get('filename', '').strip()
    format_ext = request.form.get('format', 'mp4').strip().lower()
    vpn_country = request.form.get('vpn_country', '').strip()

    if not url or hhmmss_to_seconds(duration) <= 0:
        return jsonify({'error': 'URL o duración inválida'}), 400

    basename = sanitize_basename(raw_name, format_ext)
    filename = ensure_unique_filename(basename, format_ext)
    filepath = os.path.join(OUTPUT_DIR, filename)

    threading.Thread(
        target=run_ffmpeg,
        args=(url, duration, filename, format_ext, filepath, vpn_country, True),
        daemon=True
    ).start()
    vpn_msg = f" (VPN: {vpn_country})" if vpn_country else ""
    return jsonify({'message': f'Iniciada grabación: {filename}{vpn_msg}'})


@app.route('/stop', methods=['POST'])
def stop():
    global stop_requested, current_proc
    stop_requested = True
    pid = status.get('pid')
    proc = current_proc
    if proc is not None and proc.poll() is None:
        try:
            if os.name == 'nt':
                proc.terminate()
            else:
                os.kill(proc.pid, signal.SIGINT)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        status['progress'] += ' (finalizando archivo...)'
    elif pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass
    return jsonify({'message': 'Deteniendo grabación de forma segura...'})


@app.route('/status')
def get_status():
    resp = dict(status)
    resp['server_time'] = now_local().isoformat()
    resp['schedules_pending'] = sum(1 for j in scheduled_jobs if j.get('status') in ('pending', 'running'))
    resp['history_count'] = len(history)
    resp['latest_notification_id'] = notifications_list[-1]['id'] if notifications_list else 0
    resp['vpn'] = get_gluetun_info(force=False)
    return jsonify(resp)


@app.route('/api/notifications')
def api_notifications():
    """Devuelve las notificaciones del servidor para la App Android (PC + Móvil unificadas)."""
    try:
        since_id = int(request.args.get('since_id', 0) or 0)
    except Exception:
        since_id = 0
    latest_id = notifications_list[-1]['id'] if notifications_list else 0
    events = [n for n in notifications_list if int(n.get('id', 0)) > since_id]
    return jsonify({
        'latest_id': latest_id,
        'events': events
    })


@app.route('/download/<path:filename>')
def download(filename):
    safe_name = os.path.basename(filename)
    return send_from_directory(OUTPUT_DIR, safe_name, as_attachment=True)


@app.route('/stream/<path:filename>')
def stream_video(filename):
    """
    Sirve el archivo directamente desde disco soportando HTTP Range (206 Partial Content)
    para reproducción nativa en navegador con barra de tiempo completa y 0% CPU.
    """
    safe_name = os.path.basename(filename)
    ext = os.path.splitext(safe_name)[1].lower()
    # Truco Chromium: servir .mkv con mimetype video/webm para que <video> lo reproduzca inline sin descargarlo
    mime_map = {
        '.mp4': 'video/mp4',
        '.mkv': 'video/webm',
        '.webm': 'video/webm',
        '.avi': 'video/x-msvideo',
        '.ts': 'video/mp2t'
    }
    mime = mime_map.get(ext, 'video/mp4')
    return send_from_directory(OUTPUT_DIR, safe_name, as_attachment=False, conditional=True, mimetype=mime)


@app.route('/api/media-info/<path:filename>')
def api_media_info(filename):
    """Inspecciona con ffprobe la duración y las pistas de audio del archivo grabado."""
    safe_name = os.path.basename(filename)
    filepath = os.path.join(OUTPUT_DIR, safe_name)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Archivo no encontrado'}), 404

    lang_names = {
        'spa': 'Español', 'es': 'Español', 'esp': 'Español',
        'eng': 'Inglés / VO', 'en': 'Inglés / VO',
        'qaa': 'Versión Original', 'vo': 'Versión Original',
        'cat': 'Catalán', 'glg': 'Gallego', 'eus': 'Euskera',
        'jpn': 'Japonés', 'ja': 'Japonés',
        'fra': 'Francés', 'deu': 'Alemán', 'ita': 'Italiano', 'por': 'Portugués'
    }

    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=index,codec_name,codec_type,channels:stream_tags=language,title",
                "-of", "json", filepath
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=OUTPUT_DIR, timeout=10
        )
        info = json.loads(res.stdout or "{}")
        duration = float((info.get("format") or {}).get("duration") or 0)
        video_codec = ""
        audio_tracks = []
        audio_idx = 0

        for st in info.get("streams", []):
            ctype = st.get("codec_type")
            if ctype == "video" and not video_codec:
                video_codec = st.get("codec_name", "")
            elif ctype == "audio":
                tags = st.get("tags") or {}
                raw_lang = (tags.get("language") or "").strip().lower()
                raw_title = (tags.get("title") or "").strip()
                codec = (st.get("codec_name") or "audio").upper()
                ch = st.get("channels")
                ch_str = f"{ch}ch" if ch else ""

                lang_label = raw_title or lang_names.get(raw_lang) or (raw_lang.upper() if raw_lang and raw_lang != "und" else f"Audio {audio_idx + 1}")
                meta = " · ".join(x for x in [codec, ch_str] if x)
                full_label = f"Pista {audio_idx + 1} · {lang_label}" + (f" ({meta})" if meta else "")

                audio_tracks.append({
                    "index": audio_idx,
                    "stream_index": st.get("index"),
                    "codec": codec,
                    "channels": ch,
                    "lang": raw_lang,
                    "label": full_label
                })
                audio_idx += 1

        if not audio_tracks:
            audio_tracks = [{"index": 0, "label": "Pista 1 · Principal"}]

        return jsonify({
            "duration": duration,
            "video_codec": video_codec,
            "audio_tracks": audio_tracks
        })
    except Exception as e:
        return jsonify({
            "duration": get_media_duration_seconds(filepath),
            "video_codec": "",
            "audio_tracks": [{"index": 0, "label": "Pista 1 · Principal"}],
            "warning": str(e)
        })


@app.route('/stream-track/<path:filename>')
def stream_video_track(filename):
    """
    Sirve en memoria (pipe:1, 0 bytes en disco y 0% recodificación con -c copy)
    el vídeo junto con la pista de audio seleccionada (ej. Pista 2, 3...) desde el segundo indicado.
    """
    safe_name = os.path.basename(filename)
    filepath = os.path.join(OUTPUT_DIR, safe_name)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Archivo no encontrado'}), 404

    track_idx = max(0, int(request.args.get('track', 0) or 0))
    start_sec = max(0.0, float(request.args.get('start', 0) or 0))

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start_sec:.2f}",
        "-i", filepath,
        "-map", "0:v:0?",
        "-map", f"0:a:{track_idx}?",
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "matroska",
        "pipe:1"
    ]

    def generate():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=65536)
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    return Response(generate(), mimetype="video/webm")


@app.route('/history')
def get_history():
    return jsonify(history)


@app.route('/history/clear', methods=['POST'])
def clear_history():
    global history
    history = []
    save_history()
    return jsonify({'message': 'Historial vaciado correctamente'})


@app.route('/history/delete/<path:filename>', methods=['POST'])
def delete_recording(filename):
    global history
    safe_name = os.path.basename(filename)
    filepath = os.path.join(OUTPUT_DIR, safe_name)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as e:
            return jsonify({'error': f'Error eliminando archivo: {str(e)}'}), 500
    history = [item for item in history if item.get('filename') != safe_name]
    save_history()
    return jsonify({'message': f'Grabación eliminada: {safe_name}'})


@app.route('/schedule', methods=['POST'])
def create_schedule():
    url = request.form.get('url', '').strip()
    start_str = request.form.get('start', '').strip()
    end_str = request.form.get('end', '').strip()
    start_fold = int(request.form.get('start_fold', 0) or 0)
    end_fold = int(request.form.get('end_fold', 0) or 0)
    raw_name = request.form.get('filename', '').strip()
    format_ext = request.form.get('format', 'mp4').strip().lower()
    vpn_country = request.form.get('vpn_country', '').strip()

    if not all([url, start_str, end_str, raw_name]):
        return jsonify({'error': 'Faltan campos obligatorios'}), 400

    try:
        start_dt = resolve_local_datetime(start_str, fold=start_fold)
        end_dt = resolve_local_datetime(end_str, fold=end_fold)

        if real_seconds_between(start_dt, end_dt) <= 0 and end_fold == 0:
            end_dt_alt = resolve_local_datetime(end_str, fold=1)
            if real_seconds_between(start_dt, end_dt_alt) > 0:
                end_dt = end_dt_alt
    except Exception as e:
        return jsonify({'error': f'Formato de fecha inválido: {e}'}), 400

    duration_real = real_seconds_between(start_dt, end_dt)
    if duration_real <= 0:
        return jsonify({'error': 'La hora de fin debe ser posterior a la hora de inicio'}), 400

    s_utc = to_utc(start_dt)
    e_utc = to_utc(end_dt)
    for existing in scheduled_jobs:
        if existing.get('status') in ('pending', 'running'):
            try:
                ex_s = to_utc(existing['start_time'])
                ex_e = to_utc(existing['end_time'])
                if s_utc < ex_e and e_utc > ex_s:
                    return jsonify({
                        'error': f"Se solapa en horario con otra programación activa ({existing.get('filename')})"
                    }), 400
            except Exception:
                pass

    basename = sanitize_basename(raw_name, format_ext)
    filename = ensure_unique_filename(basename, format_ext)

    job = {
        "id":            str(uuid.uuid4()),
        "url":           url,
        "filename":      filename,
        "format":        format_ext,
        "start_time":    start_dt.isoformat(),
        "end_time":      end_dt.isoformat(),
        "duration_seconds": duration_real,
        "vpn_country":   vpn_country,
        "restore_vpn":   True,
        "status":        "pending",
        "status_reason": "",
        "created_at":    now_local().isoformat(),
        "actual_start":  None,
        "actual_end":    None,
    }
    scheduled_jobs.append(job)
    save_schedules()
    vpn_info_str = f" | VPN: {vpn_country}" if vpn_country else ""
    sys_log(
        f"Nueva programación creada: {filename} | "
        f"Inicio: {start_dt.isoformat()} -> Fin: {end_dt.isoformat()} | "
        f"Duración real: {seconds_to_hhmmss(duration_real)}{vpn_info_str}"
    )
    emit_app_notification(
        title="📅 Nueva grabación programada",
        body=f"{filename} · Inicio: {start_dt.strftime('%d/%m %H:%M')} ({seconds_to_hhmmss(duration_real)})"
             + (f" · 🌍 {vpn_country}" if vpn_country else ""),
        event_type="schedule_created",
        filename=filename
    )
    return jsonify({
        'message': f'Programada ({seconds_to_hhmmss(duration_real)} reales): {filename}{vpn_info_str}'
    })


@app.route('/schedules')
def get_schedules():
    return jsonify(scheduled_jobs)


@app.route('/schedules/cancel/<job_id>', methods=['POST'])
def cancel_schedule(job_id):
    for job in scheduled_jobs:
        if job['id'] == job_id:
            if job['status'] != 'pending':
                return jsonify({'error': 'Solo se pueden cancelar tareas pendientes'}), 400
            job['status'] = 'cancelled'
            job['status_reason'] = 'Cancelada por el usuario'
            save_schedules()
            return jsonify({'message': 'Cancelada'})
    return jsonify({'error': 'No encontrada'}), 404


@app.route('/schedules/delete/<job_id>', methods=['POST'])
def delete_schedule(job_id):
    global scheduled_jobs
    job = next((j for j in scheduled_jobs if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'No encontrada'}), 404
    if job['status'] in ('pending', 'running'):
        return jsonify({'error': 'Cancela o detén la programación primero'}), 400
    scheduled_jobs = [j for j in scheduled_jobs if j['id'] != job_id]
    save_schedules()
    return jsonify({'message': 'Eliminado'})


@app.route('/schedules/clear', methods=['POST'])
def clear_schedules():
    global scheduled_jobs
    scheduled_jobs = [j for j in scheduled_jobs if j['status'] in ('pending', 'running')]
    save_schedules()
    return jsonify({'message': 'Limpiadas'})


# ─────────────────────────────────────────────
#  Arranque
# ─────────────────────────────────────────────
load_vpn_countries()
load_m3u_list()
load_history()
load_schedules()
load_notifications()
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == '__main__':
    sys_log("Iniciando aplicación Flask...")
    app.run(host='0.0.0.0', port=5000, debug=False)
