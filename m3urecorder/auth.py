"""
Autenticación opcional por token (APP_TOKEN).

- Web: login con cookie de sesión (HttpOnly, SameSite=Lax).
- App Android / scripts: cabecera "Authorization: Bearer <token>".
- Reproductores externos (VLC): enlace firmado por archivo (?sig=), de solo lectura.
"""
import hashlib
import hmac
import os
import time

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from .utils import log

bp = Blueprint('auth', __name__)

# Rutas accesibles sin sesión
PUBLIC_ENDPOINTS = {'auth.login', 'auth.healthz', 'static'}
# Rutas de archivos que aceptan un enlace firmado
SIGNED_FILE_ENDPOINTS = {'routes.stream_video', 'routes.download'}


def _settings():
    return current_app.extensions['m3u'].settings


def token_matches(candidate: str) -> bool:
    token = _settings().app_token
    # compare_digest tarda lo mismo acierte o falle: no da pistas sobre el token por tiempos
    return bool(candidate) and bool(token) and hmac.compare_digest(candidate.encode(), token.encode())


def file_signature(filename: str) -> str:
    """Firma de solo lectura para un archivo concreto."""
    return hmac.new(current_app.secret_key, f"file:{filename}".encode(), hashlib.sha256).hexdigest()[:32]


def session_secret(app_token: str) -> bytes:
    """Clave de sesión derivada del token: si cambias APP_TOKEN, todas las sesiones caducan."""
    seed = app_token or os.urandom(32).hex()
    return hmac.new(seed.encode(), b"m3urecorder-session", hashlib.sha256).digest()


@bp.before_app_request
def require_auth():
    if not _settings().auth_enabled or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get('auth') is True:
        return None
    header = request.headers.get('Authorization', '')
    if header.startswith('Bearer ') and token_matches(header[7:].strip()):
        return None
    if request.endpoint in SIGNED_FILE_ENDPOINTS:
        fname = os.path.basename((request.view_args or {}).get('filename', ''))
        sig = request.args.get('sig', '')
        if sig and hmac.compare_digest(sig, file_signature(fname)):
            return None
    if request.endpoint == 'routes.index':
        return redirect(url_for('auth.login'))
    return jsonify({'error': 'No autorizado'}), 401


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if not _settings().auth_enabled or session.get('auth') is True:
        return redirect(url_for('routes.index'))
    error = ""
    if request.method == 'POST':
        if token_matches(request.form.get('token', '').strip()):
            session.clear()
            session.permanent = True
            session['auth'] = True
            return redirect(url_for('routes.index'))
        time.sleep(1)  # frena los intentos de adivinar el token a fuerza bruta
        log(f"Intento de acceso fallido desde {request.remote_addr}")
        error = "Token incorrecto"
    return render_template('login.html', error=error), (401 if error else 200)


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login') if _settings().auth_enabled else url_for('routes.index'))


@bp.route('/healthz')
def healthz():
    """Para el HEALTHCHECK de Docker."""
    return jsonify({'ok': True})
