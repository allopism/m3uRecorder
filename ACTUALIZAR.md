# Actualizar una instalación existente

Pasos para pasar de la versión original (un único `app.py` montado en el
contenedor) a la versión actual. Hazlos en la carpeta del proyecto en el servidor.

## 1. Gluetun: API de control con clave

1. **Borra `vpn/auth/config.toml`.** Si existe, tiene prioridad sobre la clave del
   compose y la API de control seguiría abierta.
2. Genera una clave y añádela a `.env` como `GLUETUN_API_KEY`:
   ```bash
   openssl rand -hex 24
   ```
   Sin ella, `docker compose` se niega a arrancar (a propósito).

## 2. `.env`

Parte de [.env.example](.env.example). Variables nuevas:

| Variable | Para qué |
|---|---|
| `GLUETUN_API_KEY` | Obligatoria. Clave de la API de control de Gluetun |
| `APP_TOKEN` | Opcional. Activa el login en la web y en la API |
| `PUID` / `PGID` | Usuario y grupo dueños de la carpeta de grabaciones |
| `VPN_ENABLED` | `false` para usar la app sin VPN |
| `RECORDINGS_DIR`, `M3U_LIST_PATH`, `WEB_PORT` | Rutas y puerto en el host |

## 3. `docker-compose.yml`

Lo más sencillo es partir del [docker-compose.yml](docker-compose.yml) del repo y
ajustar las rutas en `.env`. Si prefieres editar el tuyo:

- En `gluetun`, cambia `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE` por la versión con
  `"auth":"apikey"`.
- En el recorder, añade `APP_TOKEN`, `VPN_ENABLED`, `VPN_SERVICE_PROVIDER`,
  `GLUETUN_API_KEY`, `user: "${PUID}:${PGID}"`, `stop_grace_period: 60s` y el
  volumen `./vpn/servers:/gluetun/servers:ro`.
- **Quita el volumen que monta `app.py`.** El código ya no es un solo archivo: va
  dentro de la imagen y se actualiza reconstruyéndola.

## 4. Permisos de la carpeta de grabaciones

El contenedor ya no corre como root. Elige una opción:

- Pon en `PUID`/`PGID` el dueño actual de la carpeta (`ls -ln /ruta/de/grabaciones`).
- O dásela al UID 1000:
  ```bash
  sudo chown -R 1000:1000 /ruta/de/grabaciones
  ```

## 5. Reconstruir y arrancar

```bash
docker compose up -d --build
```

Comprueba que el recorder queda `healthy` con `docker compose ps`.

## 6. App Android

Si activas `APP_TOKEN`, la app Android necesita enviar el token
(`Authorization: Bearer <token>`). Hasta adaptarla, déjalo vacío.
