"""m3uRecorder: DVR autoalojado para grabar streams en directo con ffmpeg."""
import os
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import timedelta

from flask import Flask

from . import auth, routes
from .channels import ChannelList
from .notify import Notifier
from .recorder import Recorder
from .scheduler import Scheduler
from .settings import APP_VERSION, Settings
from .storage import HistoryStore, NotificationStore, ScheduleStore
from .timeutils import set_timezone
from .utils import log
from .vpn import VpnController

__version__ = APP_VERSION


@dataclass
class Services:
    """Todo lo que comparten las rutas, accesible como current_app.extensions['m3u']."""
    settings: Settings
    history: HistoryStore
    schedules: ScheduleStore
    notifications: NotificationStore
    notifier: Notifier
    vpn: VpnController
    channels: ChannelList
    recorder: Recorder
    scheduler: Scheduler


def build_services(settings: Settings) -> Services:
    set_timezone(settings.tz_name)
    os.makedirs(settings.output_dir, exist_ok=True)

    history = HistoryStore(settings.history_file)
    schedules = ScheduleStore(settings.schedules_file)
    notifications = NotificationStore(settings.notifications_file)
    for store in (history, schedules, notifications):
        store.load()

    notifier = Notifier(notifications, settings.telegram_token, settings.telegram_chat_id)
    vpn = VpnController(settings)
    vpn.load_countries()
    channels = ChannelList(settings.m3u_list_file)
    channels.load()
    recorder = Recorder(settings, vpn, notifier, history, schedules)
    scheduler = Scheduler(settings, recorder, schedules, notifier)
    return Services(settings, history, schedules, notifications, notifier, vpn, channels, recorder, scheduler)


def create_app(settings: Settings = None, start_background: bool = True) -> Flask:
    """
    Crea la aplicación. Con start_background=False no arranca el programador ni el
    monitor de la VPN (útil en los tests).
    """
    settings = settings or Settings.from_env()
    services = build_services(settings)

    app = Flask(__name__)
    app.secret_key = auth.session_secret(settings.app_token)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,   # el JavaScript de la página no puede leer la cookie
        SESSION_COOKIE_SAMESITE='Lax',  # otras webs no pueden enviar formularios con tu sesión
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    app.extensions['m3u'] = services
    app.register_blueprint(auth.bp)
    app.register_blueprint(routes.bp)

    if start_background:
        threading.Thread(target=services.scheduler.loop, daemon=True).start()
        if settings.vpn_enabled:
            threading.Thread(target=services.vpn.monitor_loop, daemon=True).start()
    return app


def run_server() -> None:
    """Arranque en producción: waitress + apagado limpio al recibir SIGTERM de Docker."""
    from waitress import serve

    app = create_app()
    services = app.extensions['m3u']
    settings = services.settings

    def handle_shutdown(signum, _frame):
        # Python, como PID 1 del contenedor, ignora SIGTERM si no tiene manejador, y Docker
        # acabaría matando ffmpeg a la fuerza. Aquí se cierra la grabación con cuidado.
        if services.recorder.shutdown_requested:
            return
        log(f"Señal {signal.Signals(signum).name} recibida: apagando de forma segura...")
        services.recorder.shutdown(settings.shutdown_grace_sec)
        log("Aplicación detenida.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    if not settings.auth_enabled:
        log("Aviso: APP_TOKEN no está definido; la web y la API no piden autenticación.")
    log(f"m3uRecorder {APP_VERSION} · servidor web (waitress, {settings.web_threads} hilos) en el puerto 5000")
    serve(app, host='0.0.0.0', port=5000, threads=settings.web_threads, ident=None)
