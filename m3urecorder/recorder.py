"""
El grabador. Una sola captura a la vez (la VPN es global: dos grabaciones podrían
necesitar países distintos), pero la conversión final de una grabación puede
solaparse con la captura de la siguiente.

Grabación inmediata y programada comparten el mismo flujo (_run). Diferencias:
- Inmediata: graba N segundos de vídeo. Si hay cortes, intenta recuperar el vídeo
  perdido grabando algo más de tiempo.
- Programada: cubre una franja horaria fija, más un colchón final que se cede si
  otra programada empieza justo después.
"""
import os
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from . import media
from .notify import Notifier
from .settings import Settings
from .storage import HistoryStore, ScheduleStore
from .timeutils import now_local, now_utc, seconds_between, to_utc
from .utils import human_size, log, remove_quietly, seconds_to_hhmmss
from .vpn import VpnController

IDLE_STATUS = {
    'running': False,
    'pid': None,
    'progress': 'Listo para grabar',
    'start_time': None,
    'duration_requested': None,
    'filename': None,
    'mode': None,
    'vpn_country': None,
    'last_result': None,
    'channel': None,
    'logo_id': None,
}


@dataclass
class RecordingPlan:
    kind: str                     # 'immediate' | 'scheduled'
    url: str
    filename: str
    format: str
    vpn_country: str = ""
    restore_vpn: bool = True
    requested_sec: int = 0        # solo inmediatas
    job: dict | None = None    # solo programadas

    @property
    def scheduled(self) -> bool:
        return self.kind == 'scheduled'


class Recorder:
    def __init__(self, settings: Settings, vpn: VpnController, notifier: Notifier,
                 history: HistoryStore, schedules: ScheduleStore):
        self.settings = settings
        self.vpn = vpn
        self.notifier = notifier
        self.history = history
        self.schedules = schedules

        self.status = dict(IDLE_STATUS)
        self.current_proc = None
        self.stop_requested = False
        self.shutdown_requested = False

        self._lock = threading.Lock()
        self._capturing = False        # hueco de captura ocupado
        self._status_owner = None      # la grabación que controla lo que muestra la interfaz
        self._workers = 0              # hilos de grabación vivos (captura o conversión)
        self._workers_cond = threading.Condition()
        self._reserved_names = set()   # nombres en uso por grabaciones aún no terminadas
        self._names_lock = threading.Lock()
        self._preswitch = {}           # job_id -> preconexión VPN en curso o terminada
        self.on_capture_released = None  # aviso al programador de que el hueco quedó libre
        self.channel_lookup = None       # url -> canal de la lista (para nombre y logo)
        self.thumbnails = None           # ThumbnailCache

    # ── Reserva del grabador ──────────────────
    def is_busy(self) -> bool:
        return self._capturing

    def try_reserve(self) -> bool:
        """
        Ocupa el hueco de captura de forma atómica. Comprobar y marcar dentro del
        mismo lock evita que dos peticiones casi simultáneas lancen dos ffmpeg.
        """
        with self._lock:
            if self._capturing or self.shutdown_requested:
                return False
            self._capturing = True
            self.status['running'] = True
            return True

    def cancel_reservation(self) -> None:
        with self._lock:
            self._capturing = False
            if self._status_owner is None:
                self.status['running'] = False

    # ── Nombres de archivo ────────────────────
    def unique_filename(self, basename: str, ext: str, reserve: bool = False) -> str:
        out = self.settings.output_dir
        scheduled = {j.get('filename') for j in self.schedules.active()}
        with self._names_lock:
            taken = self._reserved_names | scheduled
            candidate, n = f"{basename}.{ext}", 2
            while (candidate in taken or os.path.exists(os.path.join(out, candidate))
                   or os.path.exists(os.path.join(out, candidate + ".rec.ts"))):
                candidate, n = f"{basename}_{n}.{ext}", n + 1
            if reserve:
                self._reserved_names.add(candidate)
            return candidate

    def release_filename(self, filename: str) -> None:
        with self._names_lock:
            self._reserved_names.discard(filename)

    def is_in_progress(self, filename: str) -> bool:
        with self._names_lock:
            return filename in self._reserved_names

    # ── Control ───────────────────────────────
    def start(self, plan: RecordingPlan) -> None:
        """Lanza la grabación. El hueco de captura debe estar ya reservado (try_reserve)."""
        with self._names_lock:
            self._reserved_names.add(plan.filename)
        with self._workers_cond:
            self._workers += 1
        threading.Thread(target=self._worker, args=(plan,), daemon=True).start()

    def request_stop(self) -> None:
        self.stop_requested = True
        if media.signal_stop(self.current_proc):
            self.status['progress'] += ' (finalizando archivo...)'

    def shutdown(self, grace_sec: int) -> None:
        """Para la captura en curso y espera a que se guarde todo (máx. grace_sec)."""
        self.shutdown_requested = True
        self.stop_requested = True
        media.signal_stop(self.current_proc)
        with self._workers_cond:
            if self._workers:
                log(f"Esperando a que la grabación se cierre (máx. {grace_sec}s)...")
                if self._workers_cond.wait_for(lambda: self._workers == 0, timeout=grace_sec):
                    log("Grabación cerrada y guardada correctamente.")
                else:
                    log("Aviso: la grabación no terminó de cerrarse a tiempo.")

    # ── VPN de las programadas ────────────────
    def has_preswitch(self, job_id: str) -> bool:
        return job_id in self._preswitch

    def preswitch_vpn_async(self, job: dict) -> None:
        """Conecta la VPN al país de una programada unos segundos antes de que empiece."""
        if job['id'] in self._preswitch:
            return
        entry = {'event': threading.Event(), 'previous': '', 'did_switch': False}
        self._preswitch[job['id']] = entry

        def work():
            try:
                country = job.get('vpn_country', '')
                log(f"Pre-conectando VPN a '{country}' antes de iniciar tarea programada {job['filename']}...")
                entry['previous'], entry['did_switch'] = self.vpn.prepare_for_recording(country)
                if not self.status['running']:
                    self.status['progress'] = f"🌍 VPN lista en {country} (esperando inicio de {job['filename']})"
            except Exception as e:  # noqa: BLE001
                log(f"Error en la preconexión VPN de {job.get('filename')}: {e}")
            finally:
                entry['event'].set()

        threading.Thread(target=work, daemon=True).start()

    def _take_preswitch(self, job: dict):
        """
        Si la preconexión aún está en marcha, la espera: si no, no sabríamos a qué
        país volver al terminar y la VPN se quedaría en el de esta grabación.
        """
        entry = self._preswitch.pop(job['id'], None)
        if entry is None:
            return "", False
        if not entry['event'].wait(timeout=45):
            log(f"Aviso: la preconexión VPN de {job['filename']} no terminó a tiempo")
        return entry['previous'], entry['did_switch']

    def _restore_vpn(self, previous: str, did_switch: bool, plan: RecordingPlan) -> None:
        if not did_switch or not plan.restore_vpn or not previous or self.shutdown_requested:
            return
        # Si otra programada empieza en menos de un minuto, ella ya pondrá su país
        if self.schedules.next_due(exclude=plan.job, horizon_sec=60):
            return
        log(f"Restaurando país VPN anterior: {previous}...")
        self.status['progress'] = f"🌍 Restaurando VPN a {previous}..."
        self.vpn.switch_country(previous, wait_ready=True, timeout_sec=25)

    # ── Flujo de una grabación ────────────────
    def _worker(self, plan: RecordingPlan) -> None:
        try:
            self._run(plan)
        finally:
            self.release_filename(plan.filename)
            with self._workers_cond:
                self._workers -= 1
                self._workers_cond.notify_all()

    def _set_status(self, owner, **fields) -> None:
        """Solo la grabación dueña del estado puede cambiar lo que muestra la interfaz."""
        with self._lock:
            if self._status_owner is owner:
                self.status.update(fields)

    def _run(self, plan: RecordingPlan) -> None:
        s = self.settings
        job = plan.job
        label = "programada" if plan.scheduled else "en directo"
        prefix = "[Programada] " if plan.scheduled else ""
        filename = plan.filename
        filepath = os.path.join(s.output_dir, filename)

        channel = (self.channel_lookup(plan.url) if self.channel_lookup else None) or {}
        channel_name, logo = channel.get("name", ""), channel.get("logo_id", "")

        owner = object()
        with self._lock:
            self._status_owner = owner
            self.stop_requested = self.shutdown_requested  # limpia un "parar" antiguo
            self.status.update(running=True, mode=plan.kind, filename=filename,
                               vpn_country=plan.vpn_country or None, pid=None,
                               channel=channel_name or None, logo_id=logo or None,
                               progress=f"{prefix}Preparando: {filename}...")

        previous_country, did_switch = "", False
        capture_released = False
        try:
            # 1) VPN
            if plan.scheduled:
                previous_country, did_switch = self._take_preswitch(job)
            if plan.vpn_country and not did_switch:
                previous_country, did_switch = self.vpn.prepare_for_recording(plan.vpn_country)
            vinfo = self.vpn.info()
            country = plan.vpn_country or vinfo.get("configured_country") or vinfo.get("ip_country") or ""
            country_txt = f" · 🌍 {country}" if country else ""

            # 2) Objetivo de tiempo
            start_dt = now_local()
            start_utc = to_utc(start_dt)
            if plan.scheduled:
                end_utc = to_utc(job['end_time'])
                requested = max(1, int(round((end_utc - start_utc).total_seconds())))
                padding, max_attempts, retry_sleep, media_target = s.schedule_padding_sec, 10, 5, False
            else:
                requested = plan.requested_sec
                end_utc = start_utc + timedelta(seconds=requested)
                padding, max_attempts, retry_sleep, media_target = 0, 8, 4, True

            self._set_status(owner, start_time=start_dt.isoformat(), duration_requested=requested,
                             progress=f"{prefix}Iniciando: {filename}...")
            log(f"Iniciando grabación {label.upper()} ({filename}) | Duración: {seconds_to_hhmmss(requested)}"
                + (f" (+{padding}s colchón)" if padding else "") + f" | VPN: {country or 'Actual'}")
            self.notifier.app(f"🔴 Grabación {label} iniciada",
                              f"{filename} · Duración: {seconds_to_hhmmss(requested)}{country_txt}",
                              "recording_started", filename)

            # 3) Captura (con reintentos si el stream se corta)
            segments, returncodes = [], []
            attempt, accumulated = 0, 0
            handover = threading.Event()
            while not self.stop_requested:
                remaining_wall = int(round((end_utc - now_utc()).total_seconds()))
                remaining_media = requested - accumulated
                if attempt >= max_attempts:
                    break
                # "Quedan ≤3 s, no merece la pena reconectar" solo vale para los reintentos
                if attempt > 0 and (remaining_wall <= 3 or (media_target and remaining_media <= 3)):
                    break
                attempt += 1
                target = remaining_media if media_target else remaining_wall + padding
                seg = media.segment_path(filepath, attempt)
                cmd = media.build_record_cmd(plan.url, seconds_to_hhmmss(target), seg, plan.format)
                if attempt == 1:
                    log(f"Comando FFmpeg: {' '.join(cmd)}")
                else:
                    log(f"Reintento #{attempt} para {filename} (faltan {seconds_to_hhmmss(target)})...")
                    self._set_status(owner, progress=f"{prefix}Reconectando stream (intento #{attempt})...")

                t0 = time.monotonic()
                proc_done = threading.Event()
                if padding:
                    threading.Thread(target=self._handover_watcher,
                                     args=(job, end_utc, handover, proc_done), daemon=True).start()
                try:
                    rc = media.run_ffmpeg(
                        cmd, target, cwd=s.output_dir,
                        on_start=lambda p: self._on_ffmpeg_start(p, owner),
                        on_line=lambda line: self._set_status(owner, progress=f"{prefix}{line[-85:]}"))
                finally:
                    proc_done.set()
                    self.current_proc = None
                returncodes.append(rc)

                seg_sec = 0
                if os.path.exists(seg) and os.path.getsize(seg) > 0:
                    segments.append(seg)
                    seg_sec = media.probe_duration(seg) or max(1, int(round(time.monotonic() - t0)))
                    accumulated += seg_sec
                else:
                    remove_quietly(seg)

                if self.stop_requested or handover.is_set():
                    break
                if rc == 0 and (seg_sec >= target - 3 or (media_target and accumulated >= requested - 3)):
                    break
                remaining_after = int(round((end_utc - now_utc()).total_seconds()))
                if remaining_after <= 15 or (media_target and requested - accumulated <= 5):
                    break
                time.sleep(retry_sleep)

            # 4) Fin de la captura: liberar el hueco para que pueda empezar otra grabación
            was_stopped, by_shutdown = self.stop_requested, self.shutdown_requested
            self._restore_vpn(previous_country, did_switch, plan)
            self._set_status(owner, pid=None, progress=f"{prefix}Guardando grabación en {plan.format.upper()}...")
            with self._lock:
                self._capturing = False
            capture_released = True
            if self.on_capture_released:
                self.on_capture_released()

            # 5) Conversión al formato final y evaluación
            base = os.path.splitext(filename)[0]
            filepath, note = media.finalize_recording(
                segments, filepath, plan.format,
                lambda: os.path.join(s.output_dir, self.unique_filename(base, "ts")))
            filename = os.path.basename(filepath)
            end_dt = now_local()
            elapsed = seconds_between(start_dt, end_dt)
            result, reasons, size, media_sec = media.evaluate_recording(
                returncodes, filepath, requested, was_stopped, by_shutdown)
            if media_sec == 0 and size > 0:
                media_sec = elapsed
            if size > 0 and self.thumbnails:
                self.thumbnails.get(filename)
            reason = "; ".join(reasons + ([note] if note else []))
            log(f"Finalizada {'Programada' if plan.scheduled else 'Inmediata'}: {filename} | Estado: {result} "
                f"| Tamaño: {human_size(size)} | Detalles: {reason}")

            # 6) Guardar resultado y avisar
            if job is not None:
                job.update(filename=filename, status=result, status_reason=reason,
                           actual_start=start_dt.isoformat(), actual_end=end_dt.isoformat(),
                           filesize_human=human_size(size), filesize_bytes=size)
                self.schedules.save()
            entry = {
                "filename": filename, "url": plan.url,
                "start": start_dt.isoformat(), "end": end_dt.isoformat(),
                "duration_requested": seconds_to_hhmmss(requested),
                "duration_seconds": elapsed, "media_duration_seconds": media_sec,
                "format": plan.format, "filesize_bytes": size, "filesize_human": human_size(size),
                "status": result, "status_reason": reason, "vpn_country": country, "type": plan.kind,
                "channel": channel_name, "logo_id": logo,
            }
            if job is not None:
                entry.update(scheduled_start=job['start_time'], scheduled_end=job['end_time'])
            self.history.append(entry)
            final_msg = self._notify_result(plan, label, prefix, result, filename, media_sec, size, reason, country)
            self._finish_status(owner, filename=filename, progress=final_msg,
                                last_result={"status": result, "reason": reason})
        except Exception as e:  # noqa: BLE001 - un fallo inesperado no debe dejar el grabador bloqueado
            reason = f"Excepción: {e}"
            log(f"Excepción en grabación {label} ({filename}): {e}")
            if job is not None:
                job.update(status='failed', status_reason=reason)
                self.schedules.save()
            self.notifier.telegram(f"❌ Excepción en grabación {label}:\n📁 {filename}\n⚠️ {e}")
            if not capture_released:
                self._restore_vpn(previous_country, did_switch, plan)
                with self._lock:
                    self._capturing = False
                capture_released = True
            self._finish_status(owner, progress=f"{prefix}{reason}",
                                last_result={"status": "failed", "reason": reason})
        finally:
            with self._lock:
                if not capture_released:
                    self._capturing = False

    def _on_ffmpeg_start(self, proc, owner) -> None:
        self.current_proc = proc
        self._set_status(owner, pid=proc.pid)
        if self.stop_requested:  # se pidió parar justo mientras arrancaba ffmpeg
            media.signal_stop(proc)

    def _finish_status(self, owner, **fields) -> None:
        """Publica el resultado y deja la interfaz en reposo, salvo que otra grabación haya tomado el relevo."""
        with self._lock:
            if self._status_owner is not owner:
                return
            self.status.update(fields)
            if not self._capturing:
                self.status.update(running=False, pid=None, start_time=None, duration_requested=None,
                                   mode=None, vpn_country=None, channel=None, logo_id=None)
                self._status_owner = None

    def _handover_watcher(self, job, end_utc, handover: threading.Event, proc_done: threading.Event) -> None:
        """
        Durante el colchón final de una programada, si otra programada empieza ya,
        corta a la hora exacta y le cede el grabador (si no, le robaría su comienzo).
        """
        signaled = None
        while not proc_done.wait(1):
            if not handover.is_set():
                if now_utc() < end_utc:
                    continue
                nxt = self.schedules.next_due(exclude=job)
                if nxt is None:
                    continue
                log(f"Colchón recortado: {job['filename']} cede el grabador a {nxt.get('filename')}")
                handover.set()
            # Avisar una sola vez a cada ffmpeg (varias señales seguidas lo matan sin cerrar el archivo)
            proc = self.current_proc
            if proc is not None and proc is not signaled:
                signaled = proc
                media.signal_stop(proc)

    def _notify_result(self, plan, label, prefix, result, filename, media_sec, size, reason, country) -> str:
        kind_title = " programada" if plan.scheduled else ""
        dur, sz = seconds_to_hhmmss(media_sec), human_size(size)
        country_txt = f" · 🌍 {country}" if country else ""
        if result == "ok":
            self.notifier.app(f"✅ Grabación{kind_title} completada", f"{filename} · {dur} · {sz}{country_txt}",
                              "recording_ok", filename)
            self.notifier.telegram(f"✅ Grabación {label} finalizada con éxito:\n📁 {filename}\n"
                                   f"⏱️ Duración: {dur}\n💾 Tamaño: {sz}" + (f"\n🌍 VPN: {country}" if country else ""))
            return f"{prefix}✅ Grabación completada ({sz})"
        if result == "stopped":
            self.notifier.app(f"⏹️ Grabación{kind_title} detenida", f"{filename} · {dur} · {sz}",
                              "recording_stopped", filename)
            self.notifier.telegram(f"⏹️ Grabación {label} detenida:\n📁 {filename}\n⏱️ Duración: {dur}\n"
                                   f"💾 Tamaño: {sz}\nℹ️ {reason}")
            return f"{prefix}⏹️ Grabación detenida ({sz})"
        self.notifier.app(f"❌ Error en grabación{kind_title}", f"{filename} · {reason}", "recording_failed", filename)
        self.notifier.telegram(f"❌ Grabación {label} fallida:\n📁 {filename}\n⚠️ Motivo: {reason}")
        return f"{prefix}❌ Error: {reason}"
