#!/usr/bin/env python3
"""
server.py
==========
API HTTP (FastAPI) que expone el pipeline de sourcing-agent de Daito
para que la maqueta HTML pueda llamarlo vía fetch().

Endpoints:
    GET  /health   -> ping sin autenticación
    POST /analyze  -> corre el pipeline completo (requiere X-API-Key)

El pipeline reutiliza los módulos del paquete `daito/` (los mismos que usa
`daito_analyze.py` en CLI). Devuelve el mismo JSON wishlist que el CLI.

Para correr en local (modo dev):
    uvicorn server:app --reload

Para producción (Docker / Render):
    uvicorn server:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Cargar .env si está disponible (solo dev / local)
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from daito import identify, ml_api, weight_finder, cost_calc, strategy
from daito.models import AnalysisResult


# --------------------------------------------------------------------------
# Configuración general
# --------------------------------------------------------------------------

# Tamaño máximo de imagen aceptado (10 MB)
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Timeout cuando descargamos image_url (segundos)
IMAGE_DOWNLOAD_TIMEOUT = 10

app = FastAPI(
    title="Daito Sourcing Agent API",
    version="1.0",
    description="API que identifica sets LEGO y devuelve análisis de precio/margen.",
)

# CORS abierto por ahora (la maqueta puede estar en cualquier dominio).
# El usuario lo va a cerrar más adelante.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Modelos de request
# --------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """Body del POST /analyze. Hay que mandar al menos UNO de los tres campos."""

    image_url: Optional[str] = Field(default=None, description="URL pública de la imagen del producto.")
    supplier_url: Optional[str] = Field(default=None, description="URL del producto (Amazon, LEGO.com, etc).")
    image_b64: Optional[str] = Field(default=None, description="Imagen en base64 (sin prefijo data:).")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _check_auth(x_api_key: Optional[str]) -> None:
    """Valida el header X-API-Key contra DAITO_API_TOKEN del entorno."""
    expected = os.environ.get("DAITO_API_TOKEN")
    if not expected:
        # Si no hay token configurado, fallamos cerrado (mejor seguro que abierto).
        raise HTTPException(
            status_code=500,
            detail="DAITO_API_TOKEN no está configurado en el servidor.",
        )
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=401, detail="API key inválida o ausente.")


def _guess_media_type_from_url(url: str) -> str:
    """Adivina el media_type a partir de la extensión de la URL."""
    path = urlparse(url).path.lower()
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".gif"):
        return "image/gif"
    if path.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"  # default seguro para Amazon/etc.


def _guess_media_type_from_b64(data: bytes) -> str:
    """Detecta el formato a partir de los magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) > 12 and data[8:12] == b"WEBP":
        return "image/webp"
    # JPEG arranca con FF D8
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "image/jpeg"


def _download_image(url: str) -> tuple[bytes, str]:
    """Descarga una imagen desde una URL pública. Devuelve (bytes, media_type)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(
            url,
            headers=headers,
            timeout=IMAGE_DOWNLOAD_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=400, detail=f"No pude descargar image_url: {e}")

    # Leemos en chunks para poder cortar si supera el límite.
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"La imagen supera el límite de {MAX_IMAGE_BYTES // (1024 * 1024)} MB.",
            )
        chunks.append(chunk)

    data = b"".join(chunks)
    # Primero confiamos en el Content-Type, después en magic bytes.
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        media_type = ctype
    else:
        media_type = _guess_media_type_from_url(url) or _guess_media_type_from_b64(data)
    return data, media_type


def _decode_b64(b64_str: str) -> tuple[bytes, str]:
    """Decodifica una imagen en base64. Acepta o no el prefijo data:."""
    if not b64_str or not isinstance(b64_str, str):
        raise HTTPException(status_code=400, detail="image_b64 vacío o inválido.")

    # Si viene como data URL (data:image/png;base64,xxxx) sacamos el prefijo
    if b64_str.startswith("data:"):
        try:
            header, b64_str = b64_str.split(",", 1)
        except ValueError:
            raise HTTPException(status_code=400, detail="image_b64 con prefijo data: malformado.")

    # Limpiamos espacios y saltos de línea
    b64_str = "".join(b64_str.split())

    try:
        data = base64.b64decode(b64_str, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="image_b64 no es base64 válido.")

    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"La imagen supera el límite de {MAX_IMAGE_BYTES // (1024 * 1024)} MB.",
        )
    if len(data) < 100:
        raise HTTPException(status_code=400, detail="image_b64 demasiado chica para ser una imagen.")

    return data, _guess_media_type_from_b64(data)


# --------------------------------------------------------------------------
# Pipeline principal (sincrónico, lo corremos en threadpool desde async)
# --------------------------------------------------------------------------


def _run_pipeline(
    image_url: Optional[str],
    supplier_url: Optional[str],
    image_b64: Optional[str],
) -> dict:
    """
    Ejecuta el pipeline completo y devuelve el dict wishlist.
    Réplica de la orquestación de daito_analyze.analyze_one() pero adaptada
    a entradas vía HTTP (bytes en memoria, sin path en disco).
    """
    cfg = cost_calc.load_config()

    # 1) Identificar el set ----------------------------------------------------
    if supplier_url:
        ident = identify.identify_from_url(supplier_url)
    elif image_url:
        img_bytes, media_type = _download_image(image_url)
        ident = identify.identify_from_image_bytes(img_bytes, media_type=media_type)
        # Conservamos la URL original para la wishlist
        if not ident.image_url:
            ident.image_url = image_url
    elif image_b64:
        img_bytes, media_type = _decode_b64(image_b64)
        ident = identify.identify_from_image_bytes(img_bytes, media_type=media_type)
    else:
        # Esto no debería pasar (validamos antes), pero por las dudas.
        raise HTTPException(
            status_code=400,
            detail="Hay que enviar image_url, supplier_url o image_b64.",
        )

    # 2) Si no es LEGO, devolvemos resultado mínimo ----------------------------
    if not ident.is_lego:
        result = AnalysisResult(
            product_name="(no identificado como LEGO)",
            verdict="bad",
            verdict_reason="La imagen no corresponde a un producto LEGO.",
            notes="Identificación rechazada por Claude.",
        )
        return result.to_dict()

    # 3) Disparar scrapers en paralelo (peso + competencia ML) -----------------
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_weight = ex.submit(
            weight_finder.find_weight,
            ident.set_number or "",
            ident.theme,
            ident.name,
        )
        f_ml = ex.submit(ml_api.search_set, ident.set_number or "", ident.name)
        weight_res = f_weight.result()
        ml_res = f_ml.result()

    # 4) Calcular costos y elegir importador -----------------------------------
    supplier_usd = ident.retail_usd or 0.0
    best_cost, _gonzalo_cost, importer, puede_vero = cost_calc.pick_importer(
        supplier_usd,
        weight_res.weight_naked_kg,
        weight_res.weight_packaged_kg,
        cfg,
    )

    # 5) Generar estrategias y veredicto ---------------------------------------
    strategies = strategy.generate_strategies(best_cost, ml_res, cfg)
    best_margin = max((s.margin_pct for s in strategies), default=0.0)
    verdict, verdict_reason = strategy.verdict_from_best_margin(best_margin)

    proj_strategy = strategies[0] if strategies else None
    proj_price_ml = proj_strategy.price_ars if proj_strategy else 0.0
    proj_margin_ml = proj_strategy.margin_pct if proj_strategy else 0.0
    proj_margin_note = proj_strategy.reasoning if proj_strategy else ""

    # 6) Precio en daitoshops.com.ar -------------------------------------------
    daito_price = strategy.daitoshops_price(ml_res.ml_min, cfg)
    cost_landed_ars = best_cost.total_usd * cfg["business"]["dolar_ars"]
    daito_margin = strategy.daitoshops_margin(daito_price, cost_landed_ars, cfg)

    # 7) Ensamblar resultado (mismo schema que el CLI) -------------------------
    product_name = ident.name or f"LEGO {ident.set_number}"
    if ident.set_number and ident.name and ident.set_number not in product_name:
        product_name = f"LEGO {ident.set_number} {ident.name}"

    result = AnalysisResult(
        product_name=product_name,
        image_url=ident.image_url,
        weight_naked_kg=weight_res.weight_naked_kg,
        weight_packaged_kg=weight_res.weight_packaged_kg,
        weight_chosen_kg=weight_res.weight_chosen_kg,
        weight_chosen_method=weight_res.weight_chosen_method,
        weight_sources=[
            {
                "source": s.source,
                "url": s.url,
                "weight_naked_kg": s.weight_naked_kg,
                "weight_packaged_kg": s.weight_packaged_kg,
                **({"error": s.error} if s.error else {}),
            }
            for s in weight_res.sources
        ],
        prices_supplier={
            "amazon": supplier_usd if supplier_usd else None,
            "ebay": None,
            "alibaba": None,
        },
        prices_competition_ars={
            "ml_min": ml_res.ml_min,
            "ml_max": ml_res.ml_max,
            "off_ml_min": None,
            "ml_min_terms": ml_res.ml_min_terms,
            "ml_competitor_volume": [
                {
                    "seller": c.seller,
                    "price_ars": c.price_ars,
                    "sold_qty": c.sold_qty,
                    "installments_rate_pct": c.installments_rate_pct,
                    "seller_absorbs_installments": c.seller_absorbs_installments,
                    "title": c.title,
                    "permalink": c.permalink,
                }
                for c in ml_res.competitors
            ],
            "sources_consulted": ["MercadoLibre AR (via API)"],
        },
        puede_traer_veronica=puede_vero,
        importer_suggested=importer,
        projected_cost_usd=best_cost.total_usd,
        projected_price_ml_ars=proj_price_ml,
        projected_margin_ml_pct=proj_margin_ml,
        projected_margin_ml_note=proj_margin_note,
        projected_price_daitoshops_ars=daito_price,
        projected_margin_daitoshops_pct=daito_margin,
        verdict=verdict,
        verdict_reason=verdict_reason,
        strategies=[
            {
                "name": s.name,
                "price_ars": s.price_ars,
                "with_installments": s.with_installments,
                "margin_pct": s.margin_pct,
                "margin_ars": s.margin_ars,
                "sales_likelihood": s.sales_likelihood,
                "reasoning": s.reasoning,
            }
            for s in strategies
        ],
        notes=(
            f"Set #{ident.set_number} - {ident.pieces or '?'} piezas - "
            f"edad {ident.age_min or '?'}+ - confianza identificación {ident.confidence or '?'}%. "
            f"Costo landed: USD {best_cost.total_usd} (supplier {best_cost.supplier_usd} + "
            f"taxes {best_cost.taxes_us_usd} + import {best_cost.import_cost_usd} + fee {best_cost.fee_usd})."
        ),
    )
    return result.to_dict()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    """Ping liviano para healthcheck de Render / keep-alive de GitHub Actions."""
    return {"status": "ok", "version": "1.0"}


@app.post("/analyze")
async def analyze(
    body: AnalyzeRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> JSONResponse:
    """
    Endpoint principal. Recibe image_url, supplier_url o image_b64
    y devuelve el JSON wishlist (mismo schema que el CLI).
    """
    # Auth primero
    _check_auth(x_api_key)

    # Validar que al menos un input venga seteado
    if not body.image_url and not body.supplier_url and not body.image_b64:
        raise HTTPException(
            status_code=400,
            detail="Hay que enviar al menos uno de: image_url, supplier_url, image_b64.",
        )

    # El pipeline es sync y pesado (red, scraping, Claude). Lo tiramos a un thread
    # para no bloquear el event loop de FastAPI.
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            _run_pipeline,
            body.image_url,
            body.supplier_url,
            body.image_b64,
        )
    except HTTPException:
        # Errores controlados (400/401/etc.) los re-emitimos tal cual
        raise
    except Exception as e:
        # Cualquier error inesperado del pipeline -> 500 con mensaje
        traceback.print_exc(file=sys.stderr)
        raise HTTPException(status_code=500, detail=f"Pipeline falló: {e}")

    return JSONResponse(content=result)


# --------------------------------------------------------------------------
# Modo CLI: permite correr `python server.py` directamente para dev
# --------------------------------------------------------------------------

if __name__ == "__main__":
    # Atajo para testear rápido sin acordarse del comando uvicorn.
    try:
        import uvicorn
    except ImportError:
        print(
            "Falta uvicorn. Corré: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)
