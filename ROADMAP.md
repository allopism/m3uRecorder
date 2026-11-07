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

## Fase 1 — Bugs (lo que hoy puede fallar sin que te des cuenta)

Ordenados por impacto. Ninguno cambia cómo se usa la app.

1. [x] **Apagado limpio del contenedor.** Capturar `SIGTERM` para cerrar ffmpeg
       bien al hacer `docker stop`. Hoy Python ignora la señal y Docker acaba
       matando ffmpeg a la fuerza → el MP4 queda corrupto.
2. [ ] **Grabar en formato resistente a cortes.** Grabar internamente en `.ts`
       y convertir al formato pedido al terminar (sin recodificar). Así, pase lo
       que pase, el archivo siempre se puede reproducir.
3. [ ] **Nombres con apóstrofo** ("Grey's Anatomy") rompen los botones Ver/Borrar.
       Sustituir los `onclick="...('nombre')"` por atributos `data-*` + listeners.
4. [ ] **Doble grabación simultánea.** Reservar el grabador dentro de un lock en
       `/record`, no dentro del hilo (doble clic o web + Android a la vez).
5. [ ] **Texto de ffmpeg sin escapar** en la página inicial.
6. [ ] **No permitir borrar** el archivo que se está grabando.
7. [ ] **Programador no bloqueante:** el pre-cambio de VPN (hasta 28 s) pasa a
       un hilo aparte.
8. [ ] **VPN opcional y rápida:** variable `VPN_ENABLED`; si Gluetun no responde,
       `/status` no debe tardar segundos.
9. [ ] **Unión de fragmentos:** sin timeout fijo de 120 s y sin dejar `.partN`
       huérfanos si falla.

## Fase 2 — Seguridad (imprescindible antes de publicar)

1. [ ] **Autenticación por token** (`APP_TOKEN` en `.env`): login sencillo en la
       web y cabecera `Authorization` para la app Android.
2. [ ] **Lista blanca de protocolos** para la URL (`http`, `https`, `rtmp`,
       `rtsp`, `udp`…) y de formatos de salida (`mp4`, `mkv`, `ts`…).
3. [ ] **Servidor de producción** (waitress) en lugar del servidor de desarrollo.
4. [ ] **Dockerfile:** usuario no-root, `HEALTHCHECK`, no copiar `vpn/` ni la
       lista; leer los países desde el volumen de Gluetun.
5. [ ] Proteger la API de control de Gluetun (hoy `auth = "none"`). Riesgo bajo: el
       puerto 8000 no se publica, solo es accesible desde la red compartida.

## Fase 3 — Estructura del código (lo que más luce en el portfolio)

1. [ ] Sacar el HTML/CSS/JS a `templates/` y `static/` (≈2.750 líneas fuera de `app.py`).
2. [ ] Dividir el backend en módulos: `recorder`, `scheduler`, `vpn`, `storage`, `api`.
3. [ ] Unificar grabación inmediata y programada (una inmediata = programada que
       empieza ya) → eliminar ~200 líneas duplicadas.
4. [ ] Eliminar el cálculo manual de cambio de hora (ya lo resuelve `zoneinfo` + `tzdata`).
5. [ ] Tests con pytest: tiempos y DST, solapes, evaluación de grabaciones, rutas.
6. [ ] CI con GitHub Actions (tests + lint + build de la imagen).

## Fase 4 — Presentación

- [ ] README con capturas/GIF, diagrama de arquitectura y guía de despliegue.
- [ ] Licencia (MIT) y aviso de uso: solo streams públicos o con derechos.
- [ ] Imagen publicada en GitHub Container Registry.

## Fase 5 — Funcionalidad (opcional, cuando todo lo anterior esté)

- [ ] Comprobación de espacio en disco + borrado automático de lo antiguo.
- [ ] Progreso real de la grabación (`ffmpeg -progress`).
- [ ] Importar listas M3U por URL (p. ej. directamente de iptv-org) y guía EPG (XMLTV).
- [ ] Varias grabaciones a la vez (si comparten país de VPN).
- [ ] SSE en lugar de consultar el estado cada 2 s.
- [ ] SQLite en lugar de JSON.

## App Android (companion)

Se revisa después de la Fase 2, ya que tendrá que adaptarse al token de
autenticación y a los cambios de la API.
