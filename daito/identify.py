"""
Identificación de un set LEGO a partir de una imagen o URL usando Claude.
Usa el modelo Haiku (rápido y barato) por default; se puede cambiar a Sonnet.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from .models import LegoIdentification

# Modelo por defecto: Haiku para velocidad y costo.
# Si querés más precisión, cambiar a "claude-sonnet-4-5".
DEFAULT_MODEL = "claude-haiku-4-5"

# Prompt para identificar el set. Devuelve JSON estricto.
IDENTIFY_PROMPT = (
    "Identify this LEGO set. Return JSON: "
    '{set_number: "NNNNN", theme: "...", name: "...", pieces: N, '
    "age_min: N, retail_usd: N or null, confidence: 0-100}. "
    'If not a LEGO, return {is_lego: false}. '
    "Respond ONLY with valid JSON, no markdown, no commentary."
)


def _read_image_bytes(image_path: str) -> tuple[bytes, str]:
    """Lee la imagen y devuelve los bytes + el media type."""
    p = Path(image_path)
    if not p.exists():
        raise FileNotFoundError(f"No existe la imagen: {image_path}")

    suffix = p.suffix.lower().lstrip(".")
    media_type_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }
    media_type = media_type_map.get(suffix, "image/jpeg")
    return p.read_bytes(), media_type


def _extract_json(text: str) -> dict:
    """Extrae JSON aunque venga rodeado de markdown o texto."""
    # Intento 1: parsear directo
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Intento 2: buscar bloque ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # Intento 3: primer balanceo de llaves
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"No pude parsear JSON de la respuesta: {text[:200]}")


def identify_from_image_bytes(
    img_bytes: bytes,
    media_type: str = "image/jpeg",
    model: str = DEFAULT_MODEL,
) -> LegoIdentification:
    """
    Identifica un set LEGO directamente desde bytes de imagen en memoria.
    Útil para el endpoint HTTP donde recibimos el archivo en base64 o por URL,
    sin necesidad de escribir a disco.
    """
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError(
            "Falta instalar 'anthropic'. Corré: pip install -r requirements.txt"
        ) from e

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Falta ANTHROPIC_API_KEY en el entorno. Copiá .env.example a .env y completalo."
        )

    b64 = base64.standard_b64encode(img_bytes).decode("ascii")

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": IDENTIFY_PROMPT},
                ],
            }
        ],
    )

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _extract_json(text)

    if data.get("is_lego") is False:
        return LegoIdentification(is_lego=False)

    # Normalizo el set_number a string sin espacios
    set_number = data.get("set_number")
    if set_number is not None:
        set_number = str(set_number).strip()

    return LegoIdentification(
        is_lego=True,
        set_number=set_number,
        theme=data.get("theme"),
        name=data.get("name"),
        pieces=data.get("pieces"),
        age_min=data.get("age_min"),
        retail_usd=data.get("retail_usd"),
        confidence=data.get("confidence"),
    )


def identify_from_image(image_path: str, model: str = DEFAULT_MODEL) -> LegoIdentification:
    """
    Identifica un set LEGO desde una imagen local (path en disco).
    Internamente delega en identify_from_image_bytes.
    """
    img_bytes, media_type = _read_image_bytes(image_path)
    return identify_from_image_bytes(img_bytes, media_type=media_type, model=model)


def identify_from_url(url: str, model: str = DEFAULT_MODEL) -> LegoIdentification:
    """
    Identifica un set LEGO desde una URL (Amazon, LEGO.com, etc).
    Estrategia: descargar la página, extraer la imagen principal,
    y pasársela a Claude. Si no podemos, le pasamos el texto del título.
    """
    import requests
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Intento extraer la imagen principal de Amazon
    img_url: Optional[str] = None
    img_tag = soup.find("img", id="landingImage")
    if img_tag and img_tag.get("src"):
        img_url = img_tag["src"]
    if not img_url:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            img_url = og["content"]

    # Intento extraer el título también (fallback)
    title = ""
    title_tag = soup.find("span", id="productTitle")
    if title_tag:
        title = title_tag.get_text(strip=True)
    if not title:
        ogt = soup.find("meta", property="og:title")
        if ogt and ogt.get("content"):
            title = ogt["content"]

    # Si encontré una imagen, la descargo y la mando a Claude
    if img_url:
        try:
            img_resp = requests.get(img_url, headers=headers, timeout=20)
            img_resp.raise_for_status()
            # Guardo a temp para reusar identify_from_image
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(img_resp.content)
                tmp_path = tmp.name
            ident = identify_from_image(tmp_path, model=model)
            ident.image_url = img_url
            return ident
        except Exception as e:
            print(f"[identify] No pude descargar imagen, uso título: {e}", file=sys.stderr)

    # Fallback: usar el título como prompt de texto
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError("Falta 'anthropic' instalado") from e

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY")
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Product title: {title}\nURL: {url}\n\n" + IDENTIFY_PROMPT
                ),
            }
        ],
    )
    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _extract_json(text)
    if data.get("is_lego") is False:
        return LegoIdentification(is_lego=False)
    set_number = data.get("set_number")
    if set_number is not None:
        set_number = str(set_number).strip()
    return LegoIdentification(
        is_lego=True,
        set_number=set_number,
        theme=data.get("theme"),
        name=data.get("name"),
        pieces=data.get("pieces"),
        age_min=data.get("age_min"),
        retail_usd=data.get("retail_usd"),
        confidence=data.get("confidence"),
        image_url=img_url,
    )
