"""Configuración de la aplicación, leída de variables de entorno."""
import os
from dataclasses import dataclass, field

APP_VERSION = "2.0.0"

# Contenedores de salida soportados -> nombre del muxer de ffmpeg
FORMAT_MUXERS = {'mp4': 'mp4', 'mkv': 'matroska', 'ts': 'mpegts', 'avi': 'avi'}

# Extensiones que se consideran grabaciones (incluye .webm por si quedan archivos antiguos)
MEDIA_EXTENSIONS = {'.mp4', '.mkv', '.ts', '.avi', '.webm'}

# Protocolos de entrada permitidos. Sin esta lista, ffmpeg aceptaría también file:,
# concat:, etc., y se podrían "grabar" archivos internos del contenedor.
ALLOWED_URL_SCHEMES = {'http', 'https', 'rtmp', 'rtmps', 'rtsp', 'rtsps',
                       'udp', 'rtp', 'srt', 'mms', 'mmsh'}

# Países destacados que se muestran primero en los selectores de VPN
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _default_output_dir() -> str:
    out = os.environ.get("OUTPUT_DIR", "/output")
    # En Windows (desarrollo local) no existe /output: usar ./output junto al proyecto
    if os.name == "nt" and out == "/output":
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
    return out


@dataclass
class Settings:
    output_dir: str = field(default_factory=_default_output_dir)
    m3u_list_file: str = "lista.m3u"
    tz_name: str = "Europe/Madrid"

    app_token: str = ""
    web_threads: int = 16

    telegram_token: str = ""
    telegram_chat_id: str = ""

    vpn_enabled: bool = True
    vpn_provider: str = "protonvpn"
    gluetun_url: str = "http://127.0.0.1:8000"
    gluetun_api_key: str = ""
    vpn_refresh_sec: int = 5
    gluetun_servers_dirs: tuple = ("/gluetun/servers", os.path.join("vpn", "servers"))

    # Segundos de margen para arrancar una programada si otra grabación está terminando
    schedule_start_tolerance: int = 120
    # Segundos extra que graba una programada tras su hora de fin (buffer del servidor)
    schedule_padding_sec: int = 35
    # Espera máxima para cerrar la grabación al apagar (menor que stop_grace_period)
    shutdown_grace_sec: int = 45

    @property
    def auth_enabled(self) -> bool:
        return bool(self.app_token)

    @property
    def history_file(self) -> str:
        return os.path.join(self.output_dir, "recordings.json")

    @property
    def schedules_file(self) -> str:
        return os.path.join(self.output_dir, "schedules.json")

    @property
    def notifications_file(self) -> str:
        return os.path.join(self.output_dir, "notifications.json")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            output_dir=_default_output_dir(),
            m3u_list_file=os.environ.get("M3U_LIST_FILE", "lista.m3u"),
            tz_name=os.environ.get("TZ", "Europe/Madrid"),
            app_token=os.environ.get("APP_TOKEN", "").strip(),
            web_threads=_env_int("WEB_THREADS", 16),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            vpn_enabled=_env_bool("VPN_ENABLED", True),
            vpn_provider=(os.environ.get("VPN_SERVICE_PROVIDER", "").strip().lower() or "protonvpn"),
            gluetun_url=os.environ.get("GLUETUN_CONTROL_URL", "http://127.0.0.1:8000").rstrip("/"),
            gluetun_api_key=os.environ.get("GLUETUN_API_KEY", "").strip(),
            shutdown_grace_sec=_env_int("SHUTDOWN_GRACE_SEC", 45),
        )
