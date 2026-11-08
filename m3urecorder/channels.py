"""Lista de canales en formato M3U."""
import os
import re

from .logos import logo_id
from .utils import log

_GROUP_RE = re.compile(r'group-title="([^"]*)"', re.IGNORECASE)
_LOGO_RE = re.compile(r'tvg-logo="([^"]*)"', re.IGNORECASE)


def parse_m3u(text: str) -> list:
    """Convierte el contenido de un M3U en una lista de canales {name, url, group, logo}."""
    channels = []
    name = group = logo = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            g = _GROUP_RE.search(line)
            lg = _LOGO_RE.search(line)
            group = g.group(1).strip() if g else ""
            logo = lg.group(1).strip() if lg else ""
            parts = line.split(",")
            if len(parts) > 1:
                name = parts[-1].strip()
        elif line and not line.startswith("#"):
            if name:
                channels.append({"name": name, "url": line, "group": group, "logo": logo,
                                 "logo_id": logo_id(logo) if logo else ""})
            name = group = logo = ""
    return channels


class ChannelList:
    def __init__(self, path: str):
        self.path = path
        self.channels: list = []
        self.groups: list = []
        self._logo_urls: dict = {}
        self._by_url: dict = {}

    def load(self) -> int:
        if not os.path.exists(self.path):
            log(f"Aviso: No se encontró {self.path}. La pestaña M3U estará vacía.")
            self.channels, self.groups = [], []
            return 0
        try:
            with open(self.path, encoding="utf-8", errors="ignore") as f:
                channels = parse_m3u(f.read())
        except OSError as e:
            log(f"Error al cargar la lista M3U: {e}")
            return 0
        self.channels = channels
        self.groups = sorted({c["group"] for c in channels if c["group"]}, key=str.lower)
        self._logo_urls = {c["logo_id"]: c["logo"] for c in channels if c["logo_id"]}
        self._by_url = {c["url"]: c for c in channels}
        log(f"Lista cargada: {len(self.channels)} canales ({len(self.groups)} grupos)")
        return len(self.channels)

    def logo_url(self, lid: str):
        """URL original de un logo de la lista (solo se pueden descargar logos que estén en ella)."""
        return self._logo_urls.get(lid)

    def find_by_url(self, url: str):
        """Canal de la lista con esa URL de stream, o None si se grabó una URL suelta."""
        return self._by_url.get((url or "").strip())

    def search(self, query: str = "", group: str = "", limit: int = 120) -> list:
        query, group = query.strip().lower(), group.strip().lower()
        if not query and not group:
            return self.channels[:100]
        results = []
        for c in self.channels:
            if group and c.get("group", "").lower() != group:
                continue
            if query and query not in c["name"].lower() and query not in c.get("group", "").lower():
                continue
            results.append(c)
            if len(results) >= limit:
                break
        return results
