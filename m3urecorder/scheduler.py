"""Programador: lanza las grabaciones programadas cuando llega su hora."""
import threading

from .notify import Notifier
from .recorder import Recorder, RecordingPlan
from .settings import Settings
from .storage import ScheduleStore
from .timeutils import now_local, now_utc, to_utc
from .utils import log, seconds_to_hhmmss

TICK_SEC = 2
VPN_PRESWITCH_SEC = 25  # segundos antes del inicio en que se conecta la VPN al país pedido


class Scheduler:
    def __init__(self, settings: Settings, recorder: Recorder, schedules: ScheduleStore, notifier: Notifier):
        self.settings = settings
        self.recorder = recorder
        self.schedules = schedules
        self.notifier = notifier
        # El grabador avisa al quedar libre: así la siguiente programada no espera al próximo ciclo
        self._wake = threading.Event()
        recorder.on_capture_released = self._wake.set

    def loop(self) -> None:
        log(f"Programador arrancado. Hora del servidor: {now_local().isoformat()}")
        while True:
            self._wake.wait(TICK_SEC)
            self._wake.clear()
            if self.recorder.shutdown_requested:
                continue  # no lanzar nada nuevo mientras el contenedor se apaga
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - un error puntual no debe parar el programador
                log(f"Error en el programador: {e}")

    def tick(self, now=None) -> None:
        now = now or now_utc()
        changed = False
        for job in self.schedules.snapshot():
            if job.get('status') != 'pending':
                continue
            try:
                start_utc, end_utc = to_utc(job['start_time']), to_utc(job['end_time'])
            except (KeyError, ValueError) as e:
                log(f"Error leyendo fechas de la programación {job.get('filename')}: {e}")
                continue
            until_start = (start_utc - now).total_seconds()

            # Conectar la VPN al país pedido unos segundos antes, si el grabador está libre
            if (job.get('vpn_country') and not self.recorder.is_busy()
                    and 0 < until_start <= VPN_PRESWITCH_SEC and not self.recorder.has_preswitch(job['id'])):
                self.recorder.preswitch_vpn_async(job)

            if now < start_utc:
                continue

            remaining = int(round((end_utc - now).total_seconds()))
            if remaining <= 0:
                changed |= self._fail(job, 'Tiempo expirado sin poder iniciar', "tiempo expirado")
                continue

            overdue = int(round((now - start_utc).total_seconds()))
            tolerance = self.settings.schedule_start_tolerance
            if self.recorder.is_busy() and overdue > tolerance:
                changed |= self._fail(job, f'No se pudo iniciar (grabador ocupado >{tolerance}s)', "grabador ocupado")
                continue

            if not self.recorder.try_reserve():
                continue

            log(f"¡Hora alcanzada! Lanzando tarea programada: {job['filename']} "
                f"(Duración real: {seconds_to_hhmmss(remaining)})")
            job['status'] = 'running'
            self.schedules.save()
            self.recorder.start(RecordingPlan(
                kind='scheduled', url=job['url'], filename=job['filename'], format=job['format'],
                vpn_country=(job.get('vpn_country') or '').strip(),
                restore_vpn=bool(job.get('restore_vpn', True)), job=job))
        if changed:
            self.schedules.save()

    def _fail(self, job: dict, reason: str, short: str) -> bool:
        job['status'] = 'failed'
        job['status_reason'] = reason
        self.notifier.telegram(f"❌ Grabación programada no iniciada ({short}):\n📁 {job.get('filename')}")
        return True
