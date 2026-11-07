"""
Persistencia en archivos JSON: historial, programaciones y notificaciones.

Cada almacén guarda una lista en memoria protegida por un lock y la escribe de
forma atómica (archivo temporal + os.replace), para que un corte a mitad de
escritura nunca deje el JSON corrupto.
"""
import json
import os
import threading
from datetime import datetime

from .timeutils import now_local, now_utc, parse_local, to_utc
from .utils import log


class JsonListStore:
    def __init__(self, path: str, max_items: int = 0):
        self.path = path
        self.max_items = max_items
        self.lock = threading.RLock()
        self.items: list = []

    def load(self) -> None:
        with self.lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                self.items = data if isinstance(data, list) else []
            except FileNotFoundError:
                self.items = []
            except (OSError, ValueError) as e:
                log(f"Aviso: no se pudo leer {os.path.basename(self.path)} ({e}); se empieza vacío")
                self.items = []

    def save(self) -> None:
        with self.lock:
            if self.max_items and len(self.items) > self.max_items:
                self.items = self.items[-self.max_items:]
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.items, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except OSError as e:
                log(f"Error guardando {os.path.basename(self.path)}: {e}")

    def snapshot(self) -> list:
        with self.lock:
            return list(self.items)

    def append(self, item: dict) -> None:
        with self.lock:
            self.items.append(item)
            self.save()

    def remove_where(self, predicate) -> int:
        with self.lock:
            before = len(self.items)
            self.items = [it for it in self.items if not predicate(it)]
            removed = before - len(self.items)
            if removed:
                self.save()
            return removed


class HistoryStore(JsonListStore):
    def clear(self) -> None:
        with self.lock:
            self.items = []
            self.save()


class ScheduleStore(JsonListStore):
    ACTIVE = ('pending', 'running')

    def load(self) -> None:
        super().load()
        changed = False
        with self.lock:
            for job in self.items:
                # Si la app se reinició en mitad de una grabación, esa tarea ya no sigue
                if job.get('status') == 'running':
                    job['status'] = 'failed'
                    job['status_reason'] = 'La app se reinició durante la grabación'
                    changed = True
                # Programaciones antiguas guardadas sin offset: fijarles la hora local
                for key in ('start_time', 'end_time'):
                    val = job.get(key)
                    try:
                        if val and datetime.fromisoformat(val).tzinfo is None:
                            job[key] = parse_local(val).isoformat()
                            changed = True
                    except ValueError:
                        pass
                # Datos internos de una ejecución anterior
                for key in ('_pre_switched_from', '_pre_switched_done'):
                    if key in job:
                        job.pop(key)
                        changed = True
            if changed:
                self.save()

    def get(self, job_id: str):
        with self.lock:
            return next((j for j in self.items if j.get('id') == job_id), None)

    def active(self) -> list:
        with self.lock:
            return [j for j in self.items if j.get('status') in self.ACTIVE]

    def find_overlap(self, start_utc, end_utc):
        for job in self.active():
            try:
                if start_utc < to_utc(job['end_time']) and end_utc > to_utc(job['start_time']):
                    return job
            except (KeyError, ValueError):
                pass
        return None

    def next_due(self, exclude=None, horizon_sec: float = 3):
        """Otra programada pendiente que empieza ya o en los próximos segundos."""
        now = now_utc()
        for job in self.snapshot():
            if job is exclude or job.get('status') != 'pending':
                continue
            try:
                if (to_utc(job['start_time']) - now).total_seconds() <= horizon_sec:
                    return job
            except (KeyError, ValueError):
                pass
        return None

    def clear_finished(self) -> None:
        with self.lock:
            self.items = [j for j in self.items if j.get('status') in self.ACTIVE]
            self.save()


class NotificationStore(JsonListStore):
    def __init__(self, path: str):
        super().__init__(path, max_items=120)

    def emit(self, title: str, body: str, event_type: str = "info", filename: str = "") -> dict:
        with self.lock:
            next_id = max((int(n.get("id", 0)) for n in self.items), default=0) + 1
            event = {
                "id": next_id,
                "timestamp": now_local().isoformat(),
                "title": title,
                "body": body,
                "event_type": event_type,
                "filename": filename or "",
            }
            self.items.append(event)
            self.save()
            return event

    def latest_id(self) -> int:
        with self.lock:
            return self.items[-1]['id'] if self.items else 0

    def since(self, since_id: int) -> list:
        with self.lock:
            return [n for n in self.items if int(n.get('id', 0)) > since_id]
