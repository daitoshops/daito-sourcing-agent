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

# Modelo por defecto: Sonnet 4.6 — mucho mejor leyendo números de set en cajas LEGO.
# ~3-4x más caro que Haiku (~$0.01/análisis) pero vale la pena para sourcing real.
# Si necesitás bajar costo y aceptás peor precisión, cambiar a "claude-haiku-4-5".
DEFAULT_MODEL = "claude-sonnet-4-6"

# Prompt para identificar el set LEGO.
# Estrategia: forzar a Claude a LEER el set number de la caja, no adivinar por similitud visual.
# Muchos sets LEGO se parecen visualmente; solo el número los identifica de forma única.
IDENTIFY_PROMPT = (
    "Identify this LEGO set. Your task is to read the set number printed on the box.\n\n"
    "CRITICAL RULES:\n"
    "1. Look for a 4-5 digit number on the box (usually top-right corner, sometimes bottom). "
    "Examples: '42211', '10295', '75423'.\n"
    "2. If you can SEE the number clearly, return it exactly. Set confidence >= 80.\n"
    "3. If you CANNOT read the number, DO NOT guess based on similar-looking sets. "
    "Return set_number: null and confidence < 50.\n"
    "4. Many LEGO sets look very similar (Technic rovers, Icons cars, Star Wars ships). "
    "Visual similarity is NOT enough — only the number is definitive.\n"
    "5. Look also at: pieces count printed on box, age (10+, 18+), product name on box.\n\n"
    "Return JSON ONLY (no markdown, no comments):\n"
    "{\n"
    '  "set_number": "NNNNN" or null,\n'
    '  "name": "exact name from box",\n'
    '  "theme": "Technic | Icons | Star Wars | City | Creator | Friends | etc",\n'
    '  "pieces": N or null,\n'
    '  "age_min": N or null,\n'
    '  "retail_usd": N or null,\n'
    '  "confidence": 0-100 (be honest about how clearly you read the set number)\n'
    "}\n\n"
    "If the image is not a LEGO product, return: {\"is_lego\": false}.\n"
    "Respond ONLY with valid JSON, no other text."
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
    extra_context: Optional[str] = None,
) -> LegoIdentification:
    """
    Identifica un set LEGO directamente desde bytes de imagen en memoria.
    Útil para el endpoint HTTP donde recibimos el archivo en base64 o por URL,
    sin necesidad de escribir a disco.

    extra_context: texto adicional para darle más contexto a Claude.
    Por ejemplo, el título del listing de Amazon (que suele incluir el set number).
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

    # Si tenemos contexto extra (e.g. título del listing), lo agregamos AL PRINCIPIO del prompt.
    # Esto le da a Claude info adicional que muchas veces incluye el set number directo en texto.
    prompt_with_context = IDENTIFY_PROMPT
    if extra_context:
        prompt_with_context = (
            "ADDITIONAL CONTEXT (use this to confirm your identification):\n"
            f"{extra_context}\n\n"
            "If the context above contains the LEGO set number (like '42211', '10295', '75423') "
            "and matches what you see in the image, use it confidently with high confidence (>= 90).\n\n"
            + IDENTIFY_PROMPT
        )

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
                    {"type": "text", "text": prompt_with_context},
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

    # Si encontré una imagen, la descargo y la mando a Claude JUNTO CON EL TÍTULO del listing.
    # Esto es clave: el título del listing suele tener el set number ("LEGO 75423 X-Wing...")
    # y Claude lo puede combinar con la imagen visual para identificar con alta confianza.
    if img_url:
        try:
            img_resp = requests.get(img_url, headers=headers, timeout=20)
            img_resp.raise_for_status()
            img_bytes = img_resp.content
            # Inferir media_type del Content-Type o de la extensión de la URL
            ctype = (img_resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype.startswith("image/"):
                media_type = ctype
            else:
                media_type = "image/jpeg"
            # Armar contexto extra con title + URL para que Claude tenga toda la info
            extra_context = None
            if title:
                extra_context = f"Product listing title: '{title}'\nProduct page URL: {url}"
            ident = identify_from_image_bytes(
                img_bytes,
                media_type=media_type,
                model=model,
                extra_context=extra_context,
            )
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
