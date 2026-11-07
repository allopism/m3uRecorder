"""
Todo lo que habla con ffmpeg y ffprobe.

Estrategia de grabación: ffmpeg copia el stream sin recodificar a archivos .ts
intermedios (MPEG-TS sigue siendo reproducible aunque se corte de golpe) y, al
terminar, finalize_recording() los une y los convierte al formato pedido.
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time

from .settings import FORMAT_MUXERS
from .utils import log, remove_quietly, seconds_to_hhmmss

# Nombres de idioma para las pistas de audio del reproductor
LANG_NAMES = {
    'spa': 'Español', 'es': 'Español', 'esp': 'Español',
    'eng': 'Inglés / VO', 'en': 'Inglés / VO',
    'qaa': 'Versión Original', 'vo': 'Versión Original',
    'cat': 'Catalán', 'glg': 'Gallego', 'eus': 'Euskera',
    'jpn': 'Japonés', 'ja': 'Japonés',
    'fra': 'Francés', 'deu': 'Alemán', 'ita': 'Italiano', 'por': 'Portugués'
}


# ── Grabación ─────────────────────────────────
def segment_path(final_path: str, attempt: int) -> str:
    """Archivo .ts intermedio de cada intento (el 2º, 3º... aparecen tras reconexiones)."""
    return f"{final_path}.rec.ts" if attempt == 1 else f"{final_path}.part{attempt}.ts"


def build_record_cmd(url: str, duration_str: str, out_path: str, format_ext: str) -> list:
    cmd = ['ffmpeg', '-y', '-nostdin', '-hide_banner']
    if url.lower().startswith(('http://', 'https://')):
        cmd += ['-reconnect', '1', '-reconnect_streamed', '1',
                '-reconnect_delay_max', '10', '-rw_timeout', '20000000']
    cmd += ['-i', url, '-map', '0:v:0?', '-map', '0:a?']
    # Subtítulos solo si el formato final los admite (MP4 no acepta los de TV)
    if format_ext in ('mkv', 'ts'):
        cmd += ['-map', '0:s?']
    cmd += ['-dn', '-c', 'copy', '-t', duration_str, '-f', 'mpegts', out_path]
    return cmd


def signal_stop(proc) -> bool:
    """
    Pide a ffmpeg que termine cerrando bien el archivo (equivale a pulsar 'q').
    Devuelve False si no había proceso en marcha.
    """
    if proc is None or proc.poll() is not None:
        return False
    try:
        if os.name == 'nt':
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    return True


def run_ffmpeg(cmd: list, max_sec: int, cwd: str, on_start=None, on_line=None) -> int:
    """
    Ejecuta ffmpeg, pasa cada línea de su salida a on_line y devuelve el código de
    salida. Un vigilante lo cierra si se pasa mucho del tiempo previsto (stream colgado).
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=cwd, text=True, bufsize=1, errors='replace')
    if on_start:
        on_start(proc)

    limit = max(30, max_sec + 90)
    started = time.monotonic()

    def watchdog():
        while proc.poll() is None:
            time.sleep(2)
            if time.monotonic() - started > limit:
                log(f"Watchdog: ffmpeg superó el tiempo máximo ({limit}s). Cerrando proceso...")
                try:
                    proc.terminate()
                    time.sleep(5)
                    if proc.poll() is None:
                        proc.kill()
                except OSError:
                    pass
                return

    threading.Thread(target=watchdog, daemon=True).start()
    for line in proc.stdout:
        line = line.strip()
        if line and on_line:
            on_line(line)
    proc.wait()
    return proc.returncode


# ── Conversión final ──────────────────────────
def _remux(segments: list, out_path: str, format_ext: str, cwd: str):
    """
    Une uno o varios .ts y los convierte al formato pedido copiando los streams.
    Escribe en un temporal y solo lo renombra si todo salió bien. Devuelve (ok, error).
    """
    tmp_out = out_path + ".tmp"
    concat_list = out_path + ".concat.txt"
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
    try:
        if len(segments) == 1:
            cmd += ["-i", segments[0]]
        else:
            with open(concat_list, "w", encoding="utf-8") as f:
                for seg in segments:
                    escaped = os.path.abspath(seg).replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
            cmd += ["-f", "concat", "-safe", "0", "-i", concat_list]
        cmd += ["-map", "0:v?", "-map", "0:a?"]
        if format_ext in ('mkv', 'ts'):
            cmd += ["-map", "0:s?"]
        cmd += ["-dn", "-c", "copy"]
        if format_ext == 'mp4':
            # Índice al principio: el navegador puede reproducir sin descargarlo entero
            cmd += ["-movflags", "+faststart"]
        cmd += ["-f", FORMAT_MUXERS.get(format_ext, 'mp4'), tmp_out]

        # Margen generoso según tamaño (mínimo 10 min, ~10 MB/s de disco en el peor caso)
        timeout = 600 + sum(os.path.getsize(s) for s in segments) // (10 * 1024 * 1024)
        res = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                             cwd=cwd, timeout=timeout)
        if res.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
            os.replace(tmp_out, out_path)
            return True, ""
        return False, (res.stderr or "").strip()[-300:] or f"ffmpeg_return={res.returncode}"
    except subprocess.TimeoutExpired:
        return False, "tiempo de conversión agotado"
    except OSError as e:
        return False, str(e)
    finally:
        remove_quietly(tmp_out)
        remove_quietly(concat_list)


def finalize_recording(segments: list, final_path: str, format_ext: str, ts_fallback_path):
    """
    Convierte los .ts intermedios en el archivo final. Regla de oro: un .ts no se
    borra hasta que su contenido está a salvo en el archivo final.

    `ts_fallback_path` es una función que devuelve una ruta libre en .ts, por si la
    conversión falla. Devuelve (ruta_final, aviso); aviso vacío si todo fue bien.
    """
    cwd = os.path.dirname(final_path) or "."
    valid = [p for p in segments if os.path.exists(p) and os.path.getsize(p) > 0]
    for p in segments:
        if p not in valid:
            remove_quietly(p)
    if not valid:
        return final_path, ""

    ok, err = _remux(valid, final_path, format_ext, cwd)
    if ok:
        for seg in valid:
            remove_quietly(seg)
        return final_path, ""
    log(f"Aviso: no se pudo generar {os.path.basename(final_path)} ({len(valid)} fragmento/s): {err}")

    if len(valid) > 1:
        # Puede pasar si el canal cambió de resolución o códec al reconectar:
        # nos quedamos con el fragmento más largo y conservamos el resto en .ts
        largest = max(valid, key=os.path.getsize)
        ok, _ = _remux([largest], final_path, format_ext, cwd)
        if ok:
            remove_quietly(largest)
            rest = ", ".join(os.path.basename(p) for p in valid if p != largest)
            return final_path, ("No se pudieron unir los fragmentos; se guardó el más largo. "
                                f"Resto conservado en: {rest}")

    # Último recurso: dejarlo en .ts (MPEG-TS admite unir archivos byte a byte)
    ts_path = ts_fallback_path()
    try:
        if len(valid) == 1:
            os.replace(valid[0], ts_path)
        else:
            with open(ts_path, "wb") as out:
                for seg in valid:
                    with open(seg, "rb") as src:
                        shutil.copyfileobj(src, out, 4 * 1024 * 1024)
            for seg in valid:
                remove_quietly(seg)
    except OSError as e:
        log(f"Error guardando la grabación en .ts: {e}")
        return valid[0], f"No se pudo convertir a {format_ext}; grabación conservada en {os.path.basename(valid[0])}"
    return ts_path, f"No se pudo convertir a {format_ext}; grabación guardada en .ts"


# ── Análisis ──────────────────────────────────
def probe_duration(path: str) -> int:
    """Duración en segundos según ffprobe (0 si no se puede leer)."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=noprint_wrappers=1:nokey=1", path],
                             capture_output=True, text=True, timeout=15)
        val = res.stdout.strip()
        return max(0, int(float(val))) if val and val != "N/A" else 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def evaluate_recording(returncodes, path, requested_sec, was_stopped=False, by_shutdown=False):
    """Decide si la grabación salió bien. Devuelve (estado, motivos, bytes, segundos)."""
    size = os.path.getsize(path) if os.path.exists(path) else 0
    duration = probe_duration(path)

    if was_stopped and size > 0:
        reason = ("Detenida por apagado o reinicio del servidor" if by_shutdown
                  else "Detenida manualmente por el usuario")
        return "stopped", [reason], size, duration

    ok_duration = requested_sec > 0 and duration >= requested_sec * 0.85
    last_rc = returncodes[-1] if returncodes else -1
    reasons = []
    if last_rc != 0 and not ok_duration:
        reasons.append(f"ffmpeg_return={last_rc}")
    if not ok_duration:
        reasons.append(f"duración grabada ({seconds_to_hhmmss(duration)}) < 85% de {seconds_to_hhmmss(requested_sec)}")
    if size == 0:
        reasons.append("archivo vacío (0 B)")
    return ("ok" if ok_duration and size > 0 else "failed"), reasons, size, duration


def media_info(path: str) -> dict:
    """Duración, códec de vídeo y pistas de audio, para el reproductor de la web."""
    res = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration:stream=index,codec_name,codec_type,channels:stream_tags=language,title",
         "-of", "json", path],
        capture_output=True, text=True, timeout=10)
    info = json.loads(res.stdout or "{}")
    duration = float((info.get("format") or {}).get("duration") or 0)
    video_codec = ""
    tracks = []
    for st in info.get("streams", []):
        ctype = st.get("codec_type")
        if ctype == "video" and not video_codec:
            video_codec = st.get("codec_name", "")
        elif ctype == "audio":
            tags = st.get("tags") or {}
            lang = (tags.get("language") or "").strip().lower()
            title = (tags.get("title") or "").strip()
            codec = (st.get("codec_name") or "audio").upper()
            ch = st.get("channels")
            n = len(tracks)
            label = title or LANG_NAMES.get(lang) or (lang.upper() if lang and lang != "und" else f"Audio {n + 1}")
            meta = " · ".join(x for x in [codec, f"{ch}ch" if ch else ""] if x)
            tracks.append({
                "index": n,
                "stream_index": st.get("index"),
                "codec": codec,
                "channels": ch,
                "lang": lang,
                "label": f"Pista {n + 1} · {label}" + (f" ({meta})" if meta else ""),
            })
    if not tracks:
        tracks = [{"index": 0, "label": "Pista 1 · Principal"}]
    return {"duration": duration, "video_codec": video_codec, "audio_tracks": tracks}


def track_stream_cmd(path: str, track_idx: int, start_sec: float) -> list:
    """Vídeo + la pista de audio elegida, desde un segundo dado, en Matroska por stdout."""
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start_sec:.2f}", "-i", path,
            "-map", "0:v:0?", "-map", f"0:a:{track_idx}?",
            "-c:v", "copy", "-c:a", "copy", "-f", "matroska", "pipe:1"]
