# Hoja de ruta — m3uRecorder

DVR autoalojado para grabar streams públicos en directo (pensado para listas
abiertas como [iptv-org](https://github.com/iptv-org/iptv)), con VPN opcional
vía Gluetun para los canales geobloqueados.

**Cómo trabajamos:** cada punto es un commit pequeño. Después de cada uno se
prueba en Docker; si algo se rompe, se vuelve atrás con `git revert` o con la
etiqueta `v0-funcional` (estado original que funcionaba).

---

## Fase 0 — Base del repositorio

- [x] `.gitignore`, `.dockerignore`, `.gitattributes` (saltos de línea LF para Docker)
- [x] Commit inicial + etiqueta `v0-funcional`
- [x] `docker-compose.yml` con Gluetun + recorder (rutas relativas, versión de Gluetun fijada)
- [x] `.env.example` con todas las variables

## Fase 1 — Bugs (lo que hoy puede fallar sin que te des cuenta) ✅

Ordenados por impacto. Ninguno cambia cómo se usa la app.

1. [x] **Apagado limpio del contenedor.** Capturar `SIGTERM` para cerrar ffmpeg
       bien al hacer `docker stop`. Hoy Python ignora la señal y Docker acaba
       matando ffmpeg a la fuerza → el MP4 queda corrupto.
2. [x] **Grabar en formato resistente a cortes.** Grabar internamente en `.ts`
       y convertir al formato pedido al terminar (sin recodificar). Así, pase lo
       que pase, el archivo siempre se puede reproducir.
3. [x] **Nombres con apóstrofo** ("Grey's Anatomy") rompen los botones Ver/Borrar.
       Sustituir los `onclick="...('nombre')"` por atributos `data-*` + listeners.
4. [x] **Doble grabación simultánea.** Reservar el grabador dentro de un lock en
       `/record`, no dentro del hilo (doble clic o web + Android a la vez).
5. [x] **Texto de ffmpeg sin escapar** en la página inicial.
6. [x] **No permitir borrar** el archivo que se está grabando, ni archivos que no
       sean vídeos (antes una URL podía borrar `schedules.json` y perder todas las
       programaciones).
7. [x] **Programador no bloqueante:** el pre-cambio de VPN (hasta 28 s) pasa a
       un hilo aparte.
8. [x] **VPN opcional y rápida:** variable `VPN_ENABLED`; si Gluetun no responde,
       `/status` no debe tardar segundos.
9. [x] **Unión de fragmentos:** sin timeout fijo de 120 s y sin dejar `.partN`
       huérfanos si falla (resuelto junto con el punto 2).
10. [x] **Programaciones en el pasado:** hoy se aceptan sin error y luego fallan
        con "Tiempo expirado". Rechazarlas al crearlas si la hora de fin ya pasó.
11. [x] **Colchón de 35 s en programadas:** dos programaciones seguidas se pisaban
        y la segunda podía perderse entera. Ahora se mantiene el colchón, pero si
        otra programada empieza justo después, se corta a la hora exacta y se le
        cede el grabador.

## Fase 2 — Seguridad (imprescindible antes de publicar) ✅

1. [x] **Autenticación por token** (`APP_TOKEN`): login en la web con cookie de sesión
       (HttpOnly, SameSite) y cabecera `Authorization: Bearer` para la app Android.
       Enlaces firmados por archivo (`?sig=`) para abrir grabaciones en VLC sin
       compartir el token. Sin `APP_TOKEN` la app funciona como antes.
2. [x] **Lista blanca de protocolos** para la URL (`http`, `https`, `rtmp`, `rtsp`,
       `udp`, `srt`…): bloquea `file:`, `concat:`, etc. Formatos de salida limitados.
3. [x] **Servidor de producción** (waitress) en lugar del servidor de desarrollo.
4. [x] **Dockerfile:** usuario sin privilegios (UID configurable con `PUID`/`PGID`),
       `HEALTHCHECK` en `/healthz`, solo se copia `app.py`; países VPN leídos del
       volumen de Gluetun según `VPN_SERVICE_PROVIDER`.
5. [x] **API de control de Gluetun con clave** (`GLUETUN_API_KEY`). Se elimina
       `vpn/auth/config.toml`, que tenía prioridad y la dejaba abierta.

## Fase 3 — Estructura del código ✅

1. [x] HTML, CSS y JS separados en `templates/` y `static/` (plantillas Jinja con
       escapado automático). El HTML generado es idéntico al original.
2. [x] Backend dividido en el paquete `m3urecorder/` (`settings`, `timeutils`, `storage`,
       `notify`, `vpn`, `channels`, `media`, `recorder`, `scheduler`, `auth`, `routes`)
       con `create_app()`: sin estado global ni efectos al importar.
3. [x] Grabación inmediata y programada unificadas en un solo flujo. El hueco de
       captura se libera antes de la conversión final, así que la siguiente
       programada arranca en el mismo segundo.
4. [x] Cambio de hora resuelto con `zoneinfo` (sirve para cualquier zona horaria).
5. [x] 61 tests con pytest, incluidas grabaciones reales con ffmpeg.
6. [x] CI con GitHub Actions: ruff, pytest y construcción de la imagen con healthcheck.

## Fase 4 — Presentación

- [ ] README con capturas/GIF, diagrama de arquitectura y guía de despliegue.
- [ ] Quitar "ProtonVPN" fijo del subtítulo de la interfaz (ahora el proveedor es configurable).
- [ ] Licencia (MIT) y aviso de uso: solo streams públicos o con derechos.
- [ ] Imagen publicada en GitHub Container Registry.

## Fase 5 — Funcionalidad (opcional, cuando todo lo anterior esté)

- [ ] Comprobación de espacio en disco + borrado automático de lo antiguo.
- [ ] Recuperar al arrancar los `.rec.ts` que deje un corte brusco (convertirlos y
      añadirlos al historial). Hoy se conservan y se pueden reproducir, pero a mano.
- [ ] Progreso real de la grabación (`ffmpeg -progress`).
- [ ] Importar listas M3U por URL (p. ej. directamente de iptv-org) y guía EPG (XMLTV).
- [ ] Varias grabaciones a la vez (si comparten país de VPN).
- [ ] SSE en lugar de consultar el estado cada 2 s.
- [ ] SQLite en lugar de JSON.

## App Android (companion)

Pendiente de revisar. Si se activa `APP_TOKEN`, tendrá que enviar la cabecera
`Authorization: Bearer <token>` en cada petición y usar el campo `sig` de
`/api/media-info` para reproducir o descargar grabaciones.
