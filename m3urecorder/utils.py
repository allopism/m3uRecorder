"""Utilidades sin estado: log, formatos de tamaño y duración, nombres de archivo."""
import os
import re
import urllib.parse

from .settings import ALLOWED_URL_SCHEMES
from .timeutils import now_local


def log(msg: str) -> None:
    print(f"[{now_local().strftime('%Y-%m-%d %H:%M:%S %z')}] {msg}", flush=True)


def human_size(num) -> str:
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def hhmmss_to_seconds(hhmmss: str) -> int:
    """'01:30:00' -> 5400. También acepta 'MM:SS' y segundos sueltos. Inválido -> 0."""
    try:
        parts = [int(p) for p in str(hhmmss).strip().split(':')]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return 0


def seconds_to_hhmmss(secs) -> str:
    secs = max(0, int(secs))
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


def sanitize_basename(name: str, format_ext: str) -> str:
    """Nombre de archivo seguro, sin carpetas ni caracteres prohibidos y sin la extensión."""
    name = os.path.basename((name or "").strip())
    if name.lower().endswith("." + format_ext.lower()):
        name = name[:-(len(format_ext) + 1)]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' ._')
    if not name:
        name = f"grabacion_{now_local().strftime('%Y%m%d_%H%M%S')}"
    return name


def is_allowed_stream_url(url: str) -> bool:
    try:
        return urllib.parse.urlsplit((url or "").strip()).scheme.lower() in ALLOWED_URL_SCHEMES
    except ValueError:
        return False


def remove_quietly(path) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
