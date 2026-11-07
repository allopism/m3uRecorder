FROM python:3.12-slim

ENV TZ=Europe/Madrid \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

# Usuario sin privilegios: si alguien encontrara un fallo en la app o en ffmpeg,
# no tendría permisos de root dentro del contenedor.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /output && chown app:app /output

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Solo el código: la lista de canales y los datos de la VPN se montan como volúmenes
COPY app.py .
COPY m3urecorder/ m3urecorder/

USER app
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)" || exit 1

CMD ["python", "app.py"]
