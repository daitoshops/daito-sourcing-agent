"""
Autenticación con Mercado Libre API via client_credentials grant.

Estrategia:
- Pedimos un access_token usando client_id + client_secret (sin OAuth de usuario).
- Lo cacheamos en memoria hasta 5 min antes de que expire (lifetime real ~6h).
- En ml_api.py adjuntamos el token como Bearer en cada request a la API pública.
- Esto bypassa el bloqueo IP que ML aplica a requests anónimos desde IPs de servidores.

Variables de entorno requeridas:
- ML_CLIENT_ID
- ML_CLIENT_SECRET
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

import requests

ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

# Cache simple en memoria. El proceso se reinicia cada cierto tiempo en Render free tier,
# así que perder el cache no es problema: pedimos uno nuevo y listo.
_token_cache: dict = {"access_token": None, "expires_at": 0.0}


def get_app_token() -> Optional[str]:
    """
    Devuelve un access_token de ML usando el flow client_credentials.
    Devuelve None si faltan credenciales o si ML rechaza la request.
    El caller debería tolerar None y caer al scraping como fallback.
    """
    now = time.time()
    cached = _token_cache.get("access_token")
    expires_at = _token_cache.get("expires_at", 0.0)
    if cached and now < expires_at:
        return cached

    client_id = os.environ.get("ML_CLIENT_ID")
    client_secret = os.environ.get("ML_CLIENT_SECRET")
    if not client_id or not client_secret:
        # Sin credenciales no podemos hacer nada. Devolvemos None y el caller decide.
        return None

    try:
        resp = requests.post(
            ML_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[ml_auth] Error pidiendo token: {e}", file=sys.stderr)
        return None
    except ValueError as e:
        print(f"[ml_auth] Respuesta no-JSON del token endpoint: {e}", file=sys.stderr)
        return None

    access_token = data.get("access_token")
    expires_in = int(data.get("expires_in", 21600))  # default 6h
    if not access_token:
        print(f"[ml_auth] Respuesta sin access_token: {data}", file=sys.stderr)
        return None

    # Restamos 5 min de margen para refrescar antes de que expire
    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + expires_in - 300

    return access_token


def invalidate_token() -> None:
    """Invalida el cache. Útil si una request con el token devuelve 401."""
    _token_cache["access_token"] = None
    _token_cache["expires_at"] = 0.0
