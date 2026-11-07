"""
Cliente de la API de control de Gluetun: estado de la VPN y cambio de país.

El estado se refresca en segundo plano (monitor_loop) y las peticiones web leen
la caché, así que la interfaz nunca se queda esperando a Gluetun.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

from .settings import FEATURED_VPN_COUNTRIES, Settings
from .utils import log


class VpnController:
    def __init__(self, settings: Settings):
        self.enabled = settings.vpn_enabled
        self.base_url = settings.gluetun_url.rstrip("/")
        self.api_key = settings.gluetun_api_key
        self.provider = settings.vpn_provider
        self.refresh_sec = settings.vpn_refresh_sec
        self.servers_dirs = settings.gluetun_servers_dirs
        self.switch_lock = threading.Lock()
        self.countries = {"featured": FEATURED_VPN_COUNTRIES, "others": []}
        self.cache = {
            "last_fetch": 0.0,
            "available": False,
            "status": "unknown",
            "public_ip": "",
            "ip_country": "",
            "ip_city": "",
            "configured_country": "",
            "default_country": "",
            "provider": self.provider,
            "switching": False,
            "enabled": self.enabled,
        }

    # ── Países disponibles ────────────────────
    def load_countries(self) -> None:
        """Lee los países del proveedor desde los datos de servidores de Gluetun."""
        found = set()
        for folder in self.servers_dirs:
            path = os.path.join(folder, f"{self.provider}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                found = {s["country"].strip() for s in data.get("servers", []) if s.get("country")}
                break
            except (OSError, ValueError) as e:
                log(f"Aviso leyendo países VPN ({path}): {e}")
        featured = {c["value"] for c in FEATURED_VPN_COUNTRIES}
        self.countries = {
            "featured": FEATURED_VPN_COUNTRIES,
            "others": sorted(c for c in found if c not in featured),
        }

    # ── Estado ────────────────────────────────
    def _request(self, method: str, path: str, payload=None, timeout: float = 4.0) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw}

    def info(self, force: bool = False) -> dict:
        """Sin force: caché instantánea. Con force: consulta a Gluetun en el momento."""
        c = self.cache
        if not self.enabled or (not force and c["last_fetch"] > 0):
            return dict(c)

        now_ts = time.monotonic()
        try:
            ip_info = self._request("GET", "/v1/publicip/ip", timeout=2.5)
            c["available"] = True
            c["public_ip"] = ip_info.get("public_ip", "") or ""
            c["ip_country"] = str(ip_info.get("country", "") or "").strip().title()
            c["ip_city"] = ip_info.get("city", "") or ""
        except urllib.error.HTTPError:
            # Gluetun responde pero aún no tiene IP (p. ej. reconectando): seguimos con el resto
            c["public_ip"] = ""
        except Exception:  # noqa: BLE001 - Gluetun no responde: no esperar a las otras consultas
            c["available"] = False
            c["public_ip"] = ""
            c["last_fetch"] = now_ts
            return dict(c)

        try:
            st = self._request("GET", "/v1/vpn/status", timeout=2.0)
            c["status"] = st.get("status", "running" if c["public_ip"] else "unknown")
            c["available"] = True
        except Exception:  # noqa: BLE001
            pass

        try:
            prov = (self._request("GET", "/v1/vpn/settings", timeout=2.5).get("provider") or {})
            if prov.get("name"):
                c["provider"] = prov["name"]
            countries = (prov.get("server_selection") or {}).get("countries") or []
            if countries:
                c["configured_country"] = str(countries[0]).strip().title()
                if not c["default_country"]:
                    c["default_country"] = c["configured_country"]
            c["available"] = True
        except Exception:  # noqa: BLE001
            pass

        c["last_fetch"] = now_ts
        return dict(c)

    def monitor_loop(self) -> None:
        while True:
            if not self.cache["switching"]:
                try:
                    self.info(force=True)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(self.refresh_sec)

    # ── Cambio de país ────────────────────────
    def switch_country(self, target: str, wait_ready: bool = True, timeout_sec: int = 28):
        """Cambia el país en Gluetun y espera a que el túnel esté levantado. Devuelve (ok, mensaje)."""
        target = (target or "").strip()
        if not target:
            return True, "Sin cambio de país requerido"
        if not self.enabled:
            return False, "La VPN está desactivada (VPN_ENABLED=false)"

        c = self.cache
        with self.switch_lock:
            info = self.info(force=True)
            if info.get("configured_country", "").lower() == target.lower() and info.get("public_ip"):
                log(f"VPN ya se encuentra conectada en {target} (IP: {info.get('public_ip')})")
                return True, f"VPN activa en {target} ({info.get('public_ip')})"

            old_ip = info.get("public_ip", "")
            c["switching"] = True
            log(f"Solicitando a Gluetun cambio de país VPN -> '{target}'...")
            try:
                payload = {"provider": {"server_selection": {"countries": [target]}}}
                self._request("PUT", "/v1/vpn/settings", payload=payload, timeout=8.0)
                c["configured_country"] = target
            except urllib.error.HTTPError as he:
                c["switching"] = False
                try:
                    body = he.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    body = ""
                msg = f"Gluetun rechazó el cambio (HTTP {he.code}): {body or he.reason}"
                log(msg)
                return False, msg
            except Exception as e:  # noqa: BLE001
                c["switching"] = False
                msg = f"No se pudo conectar con la API de Gluetun ({self.base_url}): {e}"
                log(msg)
                return False, msg

            if not wait_ready:
                c["switching"] = False
                self.info(force=True)
                return True, f"Cambiando VPN a {target}..."

            # Esperar a que Gluetun reinicie el túnel y obtenga la nueva IP pública
            start = time.monotonic()
            time.sleep(2.5)
            while time.monotonic() - start < timeout_sec:
                updated = self.info(force=True)
                new_ip = updated.get("public_ip", "")
                if (updated.get("status") == "running" and new_ip
                        and (new_ip != old_ip or time.monotonic() - start > 6.0)):
                    c["switching"] = False
                    log(f"VPN conectada con éxito a {target} | Nueva IP: {new_ip} ({updated.get('ip_country')})")
                    return True, f"Conectado a {target} · IP: {new_ip}"
                time.sleep(1.5)

            c["switching"] = False
            final = self.info(force=True)
            if final.get("public_ip"):
                return True, f"VPN en {target} · IP: {final.get('public_ip')}"
            return False, f"Tiempo de espera agotado reconectando VPN a {target}"

    def prepare_for_recording(self, target: str):
        """
        Si una grabación pide un país, cambia a él. Devuelve (país_anterior, se_cambió)
        para poder restaurarlo al terminar.
        """
        target = (target or "").strip()
        if not target:
            return "", False
        if not self.enabled:
            log(f"VPN desactivada: se ignora el país '{target}' pedido para la grabación")
            return "", False
        info = self.info(force=True)
        previous = info.get("configured_country") or info.get("default_country") or ""
        if previous.lower() == target.lower() and info.get("public_ip"):
            return previous, False
        ok, msg = self.switch_country(target, wait_ready=True, timeout_sec=28)
        if not ok:
            log(f"Aviso cambio VPN a {target}: {msg}")
        return previous, True
