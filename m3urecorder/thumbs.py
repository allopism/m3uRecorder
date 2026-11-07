"""Miniaturas de las grabaciones (un fotograma en JPG), generadas al terminar o la primera vez que se piden."""
import os
import threading

from . import media
from .utils import remove_quietly


class ThumbnailCache:
    def __init__(self, cache_dir: str, output_dir: str):
        self.cache_dir = cache_dir
        self.output_dir = output_dir
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    def _paths(self, filename: str):
        base = os.path.join(self.cache_dir, os.path.basename(filename))
        return base + ".jpg", base + ".fail"

    def get(self, filename: str):
        """Ruta de la miniatura, generándola si no existe. None si el vídeo no tiene imagen."""
        thumb, fail = self._paths(filename)
        if os.path.exists(thumb):
            return thumb
        video = os.path.join(self.output_dir, os.path.basename(filename))
        if os.path.exists(fail) or not os.path.exists(video):
            return None
        with self._lock:  # una generación a la vez: ffmpeg es pesado
            if os.path.exists(thumb):
                return thumb
            if media.make_thumbnail(video, thumb):
                return thumb
            # Sin imagen (p. ej. solo audio): no reintentarlo en cada visita
            open(fail, "w").close()
            return None

    def remove(self, filename: str) -> None:
        for path in self._paths(filename):
            remove_quietly(path)
