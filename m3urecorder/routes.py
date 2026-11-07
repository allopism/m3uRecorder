"""Rutas de la web y de la API (usadas también por la app Android: no cambiar el formato)."""
import os
import subprocess
import uuid

from flask import Blueprint, Response, current_app, jsonify, render_template, request, send_from_directory

from . import media
from .auth import file_signature
from .recorder import RecordingPlan
from .settings import APP_VERSION, FORMAT_MUXERS, MEDIA_EXTENSIONS
from .timeutils import now_local, now_utc, parse_local, seconds_between, to_utc
from .utils import hhmmss_to_seconds, is_allowed_stream_url, log, sanitize_basename, seconds_to_hhmmss

bp = Blueprint('routes', __name__)

# Truco Chromium: servir .mkv como video/webm para que <video> lo reproduzca sin descargarlo
STREAM_MIME = {'.mp4': 'video/mp4', '.mkv': 'video/webm', '.webm': 'video/webm',
               '.avi': 'video/x-msvideo', '.ts': 'video/mp2t'}


def svc():
    return current_app.extensions['m3u']


def _asset_version() -> str:
    """Cambia cuando cambian el CSS o el JS: el navegador nunca usa una copia vieja en caché."""
    static = os.path.join(os.path.dirname(__file__), 'static')
    try:
        newest = max(os.path.getmtime(os.path.join(static, p)) for p in ('css/app.css', 'js/app.js'))
    except OSError:
        return APP_VERSION
    return f"{APP_VERSION}-{int(newest)}"


def error(msg: str, code: int = 400):
    return jsonify({'error': msg}), code


def _int_arg(value, default=0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _media_path(filename: str):
    safe = os.path.basename(filename)
    return safe, os.path.join(svc().settings.output_dir, safe)


# ── Página principal ──────────────────────────
@bp.route('/')
def index():
    s = svc()
    return render_template(
        'index.html',
        version=_asset_version(),
        ready_status=s.recorder.status['progress'],
        now_ts=now_local().strftime('%Y%m%d_%H%M%S'),
        channels_count=len(s.channels.channels),
        groups_count=len(s.channels.groups),
        m3u_file=s.settings.m3u_list_file,
        auth_enabled=s.settings.auth_enabled,
    )


# ── VPN ───────────────────────────────────────
@bp.route('/api/vpn/countries')
def api_vpn_countries():
    return jsonify(svc().vpn.countries)


@bp.route('/api/vpn/status')
def api_vpn_status():
    return jsonify(svc().vpn.info(force=True))


@bp.route('/api/vpn/country', methods=['POST'])
def api_vpn_change_country():
    s = svc()
    if s.recorder.is_busy():
        return error('No se puede cambiar el país manualmente mientras hay una grabación en curso')
    country = (request.form.get('country') or '').strip()
    if not country:
        return error('País no especificado')
    ok, msg = s.vpn.switch_country(country, wait_ready=True, timeout_sec=28)
    if not ok:
        return error(msg, 500)
    # Cambio manual desde la barra superior: pasa a ser también el país por defecto
    s.vpn.cache["default_country"] = country
    return jsonify({'message': msg, 'vpn': s.vpn.info()})


# ── Canales ───────────────────────────────────
@bp.route('/api/channels')
def api_channels():
    return jsonify(svc().channels.search(request.args.get('q', ''), request.args.get('group', '')))


@bp.route('/api/channels/groups')
def api_channels_groups():
    return jsonify(svc().channels.groups)


@bp.route('/api/channels/reload', methods=['POST'])
def api_channels_reload():
    ch = svc().channels
    return jsonify({'count': ch.load(), 'groups': len(ch.groups)})


# ── Grabación inmediata ───────────────────────
@bp.route('/record', methods=['POST'])
def record():
    s = svc()
    url = request.form.get('url', '').strip()
    duration = request.form.get('duration', '').strip()
    format_ext = request.form.get('format', 'mp4').strip().lower()
    vpn_country = request.form.get('vpn_country', '').strip()
    requested = hhmmss_to_seconds(duration)

    if not url or requested <= 0:
        return error('URL o duración inválida')
    if not is_allowed_stream_url(url):
        return error('Protocolo de URL no permitido')
    if format_ext not in FORMAT_MUXERS:
        return error(f'Formato no soportado: {format_ext}')
    # Validar todo antes de reservar: si no, un error dejaría el grabador "ocupado"
    if not s.recorder.try_reserve():
        return error('Ya hay una grabación en curso')

    try:
        basename = sanitize_basename(request.form.get('filename', ''), format_ext)
        filename = s.recorder.unique_filename(basename, format_ext, reserve=True)
        s.recorder.start(RecordingPlan(kind='immediate', url=url, filename=filename, format=format_ext,
                                       vpn_country=vpn_country, requested_sec=requested))
    except Exception:
        s.recorder.cancel_reservation()
        raise
    return jsonify({'message': f'Iniciada grabación: {filename}' + (f" (VPN: {vpn_country})" if vpn_country else "")})


@bp.route('/stop', methods=['POST'])
def stop():
    svc().recorder.request_stop()
    return jsonify({'message': 'Deteniendo grabación de forma segura...'})


@bp.route('/status')
def get_status():
    s = svc()
    resp = dict(s.recorder.status)
    resp['server_time'] = now_local().isoformat()
    resp['schedules_pending'] = len(s.schedules.active())
    resp['history_count'] = len(s.history.items)
    resp['latest_notification_id'] = s.notifications.latest_id()
    resp['vpn'] = s.vpn.info()
    return jsonify(resp)


@bp.route('/api/notifications')
def api_notifications():
    """Notificaciones para la app Android (eventos desde since_id)."""
    store = svc().notifications
    return jsonify({'latest_id': store.latest_id(),
                    'events': store.since(_int_arg(request.args.get('since_id')))})


# ── Imágenes ──────────────────────────────────
@bp.route('/logo/<logo_id>')
def channel_logo(logo_id):
    s = svc()
    if not logo_id.isalnum():
        return error('Logo no válido', 404)
    hit = s.logos.get(logo_id, s.channels.logo_url(logo_id))
    if not hit:
        return error('Sin logo', 404)
    path, mime = hit
    resp = send_from_directory(os.path.dirname(path), os.path.basename(path), mimetype=mime, max_age=7 * 86400)
    # Un SVG abierto directamente podría ejecutar scripts: se sirve aislado
    resp.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@bp.route('/thumb/<path:filename>')
def recording_thumb(filename):
    s = svc()
    safe, _ = _media_path(filename)
    if os.path.splitext(safe)[1].lower() not in MEDIA_EXTENSIONS or s.recorder.is_in_progress(safe):
        return error('Sin miniatura', 404)
    thumb = s.thumbnails.get(safe)
    if not thumb:
        return error('Sin miniatura', 404)
    return send_from_directory(os.path.dirname(thumb), os.path.basename(thumb), mimetype='image/jpeg', max_age=86400)


# ── Archivos ──────────────────────────────────
@bp.route('/download/<path:filename>')
def download(filename):
    safe, _ = _media_path(filename)
    return send_from_directory(svc().settings.output_dir, safe, as_attachment=True)


@bp.route('/stream/<path:filename>')
def stream_video(filename):
    """Sirve el archivo con soporte de HTTP Range: el reproductor puede saltar sin descargarlo todo."""
    safe, _ = _media_path(filename)
    mime = STREAM_MIME.get(os.path.splitext(safe)[1].lower(), 'video/mp4')
    return send_from_directory(svc().settings.output_dir, safe, as_attachment=False,
                               conditional=True, mimetype=mime)


@bp.route('/api/media-info/<path:filename>')
def api_media_info(filename):
    safe, path = _media_path(filename)
    if not os.path.exists(path):
        return error('Archivo no encontrado', 404)
    sig = file_signature(safe) if svc().settings.auth_enabled else ""
    try:
        info = media.media_info(path)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        info = {"duration": media.probe_duration(path), "video_codec": "",
                "audio_tracks": [{"index": 0, "label": "Pista 1 · Principal"}], "warning": str(e)}
    info["sig"] = sig
    return jsonify(info)


@bp.route('/stream-track/<path:filename>')
def stream_video_track(filename):
    """Vídeo + la pista de audio elegida, generado al vuelo sin recodificar ni escribir en disco."""
    safe, path = _media_path(filename)
    if not os.path.exists(path):
        return error('Archivo no encontrado', 404)
    track = max(0, _int_arg(request.args.get('track')))
    try:
        start = max(0.0, float(request.args.get('start', 0) or 0))
    except ValueError:
        start = 0.0
    cmd = media.track_stream_cmd(path, track, start)

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
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()

    return Response(generate(), mimetype="video/webm")


# ── Historial ─────────────────────────────────
@bp.route('/history')
def get_history():
    return jsonify(svc().history.snapshot())


@bp.route('/history/clear', methods=['POST'])
def clear_history():
    svc().history.clear()
    return jsonify({'message': 'Historial vaciado correctamente'})


@bp.route('/history/delete/<path:filename>', methods=['POST'])
def delete_recording(filename):
    s = svc()
    safe, path = _media_path(filename)
    # Solo vídeos: sin esto se podía borrar recordings.json o schedules.json desde la URL
    if os.path.splitext(safe)[1].lower() not in MEDIA_EXTENSIONS:
        return error('Solo se pueden borrar archivos de vídeo')
    if s.recorder.is_in_progress(safe):
        return error('No se puede borrar una grabación en curso')
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            return error(f'Error eliminando archivo: {e}', 500)
    s.thumbnails.remove(safe)
    s.history.remove_where(lambda it: it.get('filename') == safe)
    return jsonify({'message': f'Grabación eliminada: {safe}'})


# ── Programaciones ────────────────────────────
@bp.route('/schedule', methods=['POST'])
def create_schedule():
    s = svc()
    f = request.form
    url = f.get('url', '').strip()
    start_str, end_str = f.get('start', '').strip(), f.get('end', '').strip()
    start_fold, end_fold = _int_arg(f.get('start_fold')), _int_arg(f.get('end_fold'))
    raw_name = f.get('filename', '').strip()
    format_ext = f.get('format', 'mp4').strip().lower()
    vpn_country = f.get('vpn_country', '').strip()

    if not all([url, start_str, end_str, raw_name]):
        return error('Faltan campos obligatorios')
    if not is_allowed_stream_url(url):
        return error('Protocolo de URL no permitido')
    if format_ext not in FORMAT_MUXERS:
        return error(f'Formato no soportado: {format_ext}')

    try:
        start_dt = parse_local(start_str, fold=start_fold)
        end_dt = parse_local(end_str, fold=end_fold)
        # Fin en la hora repetida de octubre: si con la 1ª ocurrencia queda antes del inicio, usar la 2ª
        if seconds_between(start_dt, end_dt) <= 0 and end_fold == 0:
            alt = parse_local(end_str, fold=1)
            if seconds_between(start_dt, alt) > 0:
                end_dt = alt
    except ValueError as e:
        return error(f'Formato de fecha inválido: {e}')

    duration = seconds_between(start_dt, end_dt)
    if duration <= 0:
        return error('La hora de fin debe ser posterior a la hora de inicio')
    s_utc, e_utc = to_utc(start_dt), to_utc(end_dt)
    # Si el inicio ya pasó pero el fin no, se acepta y arranca en cuanto pueda
    if (e_utc - now_utc()).total_seconds() < 10:
        return error('La hora de fin ya ha pasado')
    clash = s.schedules.find_overlap(s_utc, e_utc)
    if clash:
        return error(f"Se solapa en horario con otra programación activa ({clash.get('filename')})")

    filename = s.recorder.unique_filename(sanitize_basename(raw_name, format_ext), format_ext)
    channel = s.channels.find_by_url(url) or {}
    job = {
        "id": str(uuid.uuid4()), "url": url, "filename": filename, "format": format_ext,
        "start_time": start_dt.isoformat(), "end_time": end_dt.isoformat(),
        "duration_seconds": duration, "vpn_country": vpn_country, "restore_vpn": True,
        "status": "pending", "status_reason": "", "created_at": now_local().isoformat(),
        "actual_start": None, "actual_end": None,
        "channel": channel.get("name", ""), "logo_id": channel.get("logo_id", ""),
    }
    s.schedules.append(job)
    vpn_txt = f" | VPN: {vpn_country}" if vpn_country else ""
    log(f"Nueva programación creada: {filename} | Inicio: {start_dt.isoformat()} -> Fin: {end_dt.isoformat()} "
        f"| Duración real: {seconds_to_hhmmss(duration)}{vpn_txt}")
    s.notifier.app("📅 Nueva grabación programada",
                   f"{filename} · Inicio: {start_dt.strftime('%d/%m %H:%M')} ({seconds_to_hhmmss(duration)})"
                   + (f" · 🌍 {vpn_country}" if vpn_country else ""),
                   "schedule_created", filename)
    return jsonify({'message': f'Programada ({seconds_to_hhmmss(duration)} reales): {filename}{vpn_txt}'})


@bp.route('/schedules')
def get_schedules():
    return jsonify(svc().schedules.snapshot())


@bp.route('/schedules/cancel/<job_id>', methods=['POST'])
def cancel_schedule(job_id):
    store = svc().schedules
    job = store.get(job_id)
    if not job:
        return error('No encontrada', 404)
    if job['status'] != 'pending':
        return error('Solo se pueden cancelar tareas pendientes')
    job['status'] = 'cancelled'
    job['status_reason'] = 'Cancelada por el usuario'
    store.save()
    return jsonify({'message': 'Cancelada'})


@bp.route('/schedules/delete/<job_id>', methods=['POST'])
def delete_schedule(job_id):
    store = svc().schedules
    job = store.get(job_id)
    if not job:
        return error('No encontrada', 404)
    if job['status'] in store.ACTIVE:
        return error('Cancela o detén la programación primero')
    store.remove_where(lambda j: j.get('id') == job_id)
    return jsonify({'message': 'Eliminado'})


@bp.route('/schedules/clear', methods=['POST'])
def clear_schedules():
    svc().schedules.clear_finished()
    return jsonify({'message': 'Limpiadas'})
