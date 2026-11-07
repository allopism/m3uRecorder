"""
Logos de canal, descargados por el servidor y guardados en caché.

El navegador nunca pide los logos a sitios de terceros: pide /logo/<id> y el
servidor los descarga una vez (por la VPN, si la hay) y los sirve desde disco.

Como el servidor descarga URLs que vienen de un archivo M3U, se protege contra
que una lista maliciosa lo use para llegar a servicios internos (SSRF):
- solo se descargan URLs que están en la lista cargada (la ruta recibe un id, no una URL),
- solo http/https, y nunca a direcciones privadas, locales o de loopback
  (comprobado también en cada redirección),
- solo imágenes, de tamaño limitado y con un timeout corto.
"""
import hashlib
import ipaddress
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .utils import log

MAX_BYTES = 512 * 1024
TIMEOUT_SEC = 6
RETRY_AFTER_SEC = 24 * 3600
ALLOWED_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico", "image/avif": ".avif",
}
EXT_TYPES = {ext: mime for mime, ext in ALLOWED_TYPES.items()}


def logo_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


class BlockedURL(Exception):
    pass


def check_public_url(url: str) -> None:
    """Lanza BlockedURL si la URL no es http(s) o su host resuelve a una dirección no pública."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL("solo se permiten URLs http/https")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise BlockedURL(f"no se pudo resolver {parts.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise BlockedURL(f"{parts.hostname} apunta a una dirección interna ({ip})")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


class LogoCache:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self._locks: dict = {}
        self._locks_guard = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    def _cached(self, lid: str):
        for ext, mime in EXT_TYPES.items():
            path = os.path.join(self.cache_dir, lid + ext)
            if os.path.exists(path):
                return path, mime
        return None

    def _recently_failed(self, lid: str) -> bool:
        marker = os.path.join(self.cache_dir, lid + ".fail")
        try:
            return time.time() - os.path.getmtime(marker) < RETRY_AFTER_SEC
        except OSError:
            return False

    def get(self, lid: str, url: str = None):
        """Devuelve (ruta, tipo) del logo en caché, descargándolo si hace falta; None si no hay."""
        hit = self._cached(lid)
        if hit or not url or self._recently_failed(lid):
            return hit
        with self._locks_guard:
            lock = self._locks.setdefault(lid, threading.Lock())
        with lock:  # si varias peticiones piden el mismo logo a la vez, se descarga una vez
            return self._cached(lid) or self._download(lid, url)

    def _download(self, lid: str, url: str):
        try:
            check_public_url(url)
            req = urllib.request.Request(url, headers={"User-Agent": "m3uRecorder"})
            with _opener.open(req, timeout=TIMEOUT_SEC) as resp:
                mime = (resp.headers.get_content_type() or "").lower()
                if mime not in ALLOWED_TYPES:
                    raise BlockedURL(f"no es una imagen ({mime})")
                data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise BlockedURL("imagen demasiado grande")
            path = os.path.join(self.cache_dir, lid + ALLOWED_TYPES[mime])
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return path, mime
        except (BlockedURL, urllib.error.URLError, OSError, ValueError) as e:
            log(f"Logo no disponible ({urllib.parse.urlsplit(url).hostname}): {e}")
            try:
                with open(os.path.join(self.cache_dir, lid + ".fail"), "w") as f:
                    f.write(str(e))
            except OSError:
                pass
            return None
