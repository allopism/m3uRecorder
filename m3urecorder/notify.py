"""Avisos: notificaciones para la web/app Android y mensajes opcionales de Telegram."""
import threading
import urllib.parse
import urllib.request

from .storage import NotificationStore
from .utils import log


class Notifier:
    def __init__(self, store: NotificationStore, telegram_token: str = "", telegram_chat_id: str = ""):
        self.store = store
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id

    def app(self, title: str, body: str, event_type: str = "info", filename: str = "") -> dict:
        return self.store.emit(title, body, event_type, filename)

    def telegram(self, text: str) -> None:
        """Envía en segundo plano: un Telegram lento nunca debe retrasar una grabación."""
        if not self.telegram_token or not self.telegram_chat_id:
            return
        threading.Thread(target=self._send_telegram, args=(text,), daemon=True).start()

    def _send_telegram(self, text: str) -> None:
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = urllib.parse.urlencode({'chat_id': self.telegram_chat_id, 'text': text}).encode('utf-8')
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8):
                pass
        except Exception as e:  # noqa: BLE001 - cualquier fallo de red se registra y se ignora
            log(f"Error enviando notificación de Telegram: {e}")
