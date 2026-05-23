"""
Cliente de competencia en Mercado Libre AR.

Estrategia:
1. Intento primero la API pública (api.mercadolibre.com) — más limpia, devuelve JSON estructurado.
2. Si la API bloquea (403 desde IPs de servidores como Render), caigo a SCRAPEAR el frontend
   público (listado.mercadolibre.com.ar/lego-{set}) que sí responde a User-Agents de navegador.

La función search_set() es la API pública del módulo y devuelve siempre un MLCompetition.
"""
from __future__ import annotations

import re
import sys
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from .models import MLCompetition, MLCompetitor
from .ml_auth import get_app_token, invalidate_token


ML_SEARCH_URL = "https://api.mercadolibre.com/sites/MLA/search"
ML_FRONTEND_BASE = "https://listado.mercadolibre.com.ar"

# Headers de navegador realista (Chrome desktop) — necesarios para que ML no nos rechace.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Palabras que descartan resultados (accesorios, no el set en sí)
ACCESSORY_TERMS = (
    "kit luz",
    "kit de luz",
    "soporte",
    "stand",
    "vitrina",
    "display case",
    "iluminacion",
    "iluminación",
    "minifigura",
    "minifig sola",
    "estuche",
    "funda",
    "calco",
    "sticker",
)

INTERNATIONAL_TERMS = ("internacional", "international")


def _is_relevant(title: str, set_number: str, permalink: str = "") -> bool:
    """Filtra: el título debe contener el set_number (si lo hay) y no ser internacional ni accesorio."""
    t = title.lower()
    if set_number and set_number not in t:
        return False
    for term in INTERNATIONAL_TERMS:
        if term in t or term in permalink.lower():
            return False
    for term in ACCESSORY_TERMS:
        if term in t:
            return False
    return True


def _build_query(set_number: str, name: Optional[str]) -> Optional[str]:
    """Arma el query string para ML. None si no hay info."""
    if set_number:
        if name:
            return f"LEGO {set_number} {name.split()[0]}"
        return f"LEGO {set_number}"
    if name:
        return f"LEGO {' '.join(name.split()[:3])}"
    return None


# ------------------------------------------------------------------ #
# Estrategia 1: API pública                                          #
# ------------------------------------------------------------------ #
def _search_via_api(query: str, set_number: str, limit: int) -> List[MLCompetitor]:
    """
    Búsqueda via api.mercadolibre.com/sites/MLA/search.

    Si tenemos credenciales OAuth (ML_CLIENT_ID/SECRET), las usamos via Bearer token.
    Esto bypassa el bloqueo IP que ML aplica a requests anónimos desde IPs de servidores.
    Sin credenciales, intentamos sin auth (tiende a fallar con 403 desde Render).
    """
    params = {"q": query, "condition": "new", "limit": limit}
    api_headers = {
        **BROWSER_HEADERS,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.mercadolibre.com.ar",
        "Referer": "https://www.mercadolibre.com.ar/",
    }

    token = get_app_token()
    if token:
        api_headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(ML_SEARCH_URL, params=params, headers=api_headers, timeout=20)

    # Si el token está vencido / inválido, invalido el cache y reintento una vez
    if resp.status_code == 401 and token:
        invalidate_token()
        new_token = get_app_token()
        if new_token:
            api_headers["Authorization"] = f"Bearer {new_token}"
            resp = requests.get(ML_SEARCH_URL, params=params, headers=api_headers, timeout=20)

    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])

    competitors: List[MLCompetitor] = []
    for r in results:
        title = r.get("title", "")
        permalink = r.get("permalink", "")
        if not _is_relevant(title, set_number, permalink):
            continue

        installments = r.get("installments") or {}
        rate = installments.get("rate")
        seller_absorbs = rate == 0 if rate is not None else False

        seller_info = r.get("seller") or {}
        seller_id = str(seller_info.get("id", "")) or r.get("seller_id", "")
        seller_name = seller_info.get("nickname") or seller_id or "desconocido"

        competitors.append(
            MLCompetitor(
                seller=str(seller_name),
                price_ars=float(r.get("price", 0) or 0),
                sold_qty=int(r.get("sold_quantity", 0) or 0),
                installments_rate_pct=float(rate) if rate is not None else None,
                seller_absorbs_installments=seller_absorbs,
                title=title,
                permalink=permalink,
                listing_type_id=r.get("listing_type_id", ""),
            )
        )
    return competitors


# ------------------------------------------------------------------ #
# Estrategia 2: scraping del frontend                                #
# ------------------------------------------------------------------ #
def _parse_price(text: str) -> Optional[float]:
    """Convierte '1.234.567' o '1234567' a float."""
    if not text:
        return None
    clean = re.sub(r"[^\d]", "", text)
    if not clean:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def _parse_sold_qty(text: str) -> int:
    """Extrae cantidad vendida de un texto tipo '+50 vendidos' o '5mil vendidos'."""
    if not text:
        return 0
    t = text.lower()
    m = re.search(r"(\d+)\s*mil", t)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r"(\d+)", t)
    if m:
        return int(m.group(1))
    return 0


def _search_via_scrape(query: str, set_number: str, limit: int) -> List[MLCompetitor]:
    """Scraping de listado.mercadolibre.com.ar. Devuelve hasta `limit` competidores."""
    # ML reemplaza espacios por guiones en la URL "amigable"
    slug = query.lower().replace(" ", "-")
    url = f"{ML_FRONTEND_BASE}/{slug}_ITEM*CONDITION_2230284"  # 2230284 = "nuevo"

    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    if resp.status_code != 200:
        # Si la URL "amigable" falla, probar la búsqueda libre por query
        fallback_url = f"{ML_FRONTEND_BASE}/{slug}"
        resp = requests.get(fallback_url, headers=BROWSER_HEADERS, timeout=20)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    competitors: List[MLCompetitor] = []

    # ML usa varios layouts; busco contenedores de items
    items = soup.select("li.ui-search-layout__item, div.ui-search-result__wrapper, div.poly-card")
    if not items:
        # fallback: cualquier <a> que apunte a /MLA-... o producto.mercadolibre.com.ar
        items = soup.find_all("a", href=re.compile(r"MLA-?\d+|articulo\.mercadolibre"))

    for item in items[: limit * 3]:  # tomo más por si filtro descarta varios
        # Título
        title_tag = (
            item.select_one("h3.poly-component__title-wrapper a")
            or item.select_one("a.poly-component__title")
            or item.select_one("h2.ui-search-item__title")
            or item.select_one("a.ui-search-item__group__element")
            or (item if getattr(item, "name", None) == "a" else None)
        )
        if not title_tag:
            continue
        title = title_tag.get_text(strip=True)
        permalink = title_tag.get("href", "") if hasattr(title_tag, "get") else ""

        if not _is_relevant(title, set_number, permalink):
            continue

        # Precio: ML pone el precio en .andes-money-amount__fraction
        price_tag = item.select_one(
            "div.poly-price__current span.andes-money-amount__fraction, "
            "span.andes-money-amount__fraction"
        )
        price_ars = _parse_price(price_tag.get_text() if price_tag else "")
        if not price_ars or price_ars <= 0:
            continue

        # Cuotas: si dice "en X cuotas sin interés" → seller absorbs
        seller_absorbs = False
        installments_rate_pct: Optional[float] = None
        installments_tag = item.select_one(
            "span.poly-price__installments, div.ui-search-installments, "
            "span.ui-search-installments"
        )
        installments_text = (installments_tag.get_text(" ", strip=True).lower()
                             if installments_tag else "")
        if installments_text:
            if "sin interés" in installments_text or "sin interes" in installments_text:
                seller_absorbs = True
                installments_rate_pct = 0.0
            else:
                # Intento extraer un % si aparece
                m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", installments_text)
                if m:
                    installments_rate_pct = float(m.group(1).replace(",", "."))

        # Vendidos: ML los muestra a veces como "+10 vendidos"
        sold_tag = item.select_one(
            "span.poly-component__sold-quantity, "
            "li.poly-attributes-list__item, "
            "span.ui-search-item__details__sold"
        )
        sold_qty = _parse_sold_qty(sold_tag.get_text(strip=True) if sold_tag else "")

        # Seller: no siempre lo muestra en el listado, lo dejamos vacío
        seller_tag = item.select_one("span.poly-component__seller")
        seller_name = seller_tag.get_text(strip=True) if seller_tag else "ml-listado"

        competitors.append(
            MLCompetitor(
                seller=seller_name,
                price_ars=price_ars,
                sold_qty=sold_qty,
                installments_rate_pct=installments_rate_pct,
                seller_absorbs_installments=seller_absorbs,
                title=title,
                permalink=permalink,
                listing_type_id="",  # no lo expone el HTML
            )
        )

        if len(competitors) >= limit:
            break

    return competitors


# ------------------------------------------------------------------ #
# Función pública                                                    #
# ------------------------------------------------------------------ #
def search_set(set_number: str, name: Optional[str] = None, limit: int = 20) -> MLCompetition:
    """
    Busca un set LEGO en Mercado Libre AR y devuelve datos de competencia.

    Intenta primero la API JSON; si falla (403 / rate-limit / red), cae a scrapear
    el frontend HTML. El resultado se normaliza al mismo MLCompetition en ambos casos.
    """
    query = _build_query(set_number, name)
    if not query:
        return MLCompetition()

    competitors: List[MLCompetitor] = []

    # Intento 1: API JSON
    try:
        competitors = _search_via_api(query, set_number, limit)
    except requests.RequestException as e:
        print(f"[ml_api] API falló ({e}), caigo a scraping del frontend", file=sys.stderr)
        competitors = []

    # Intento 2: scraping (si API no devolvió nada)
    if not competitors:
        try:
            competitors = _search_via_scrape(query, set_number, limit)
        except requests.RequestException as e:
            print(f"[ml_api] Scraping también falló: {e}", file=sys.stderr)
            return MLCompetition()
        except Exception as e:
            print(f"[ml_api] Error parseando HTML de ML: {e}", file=sys.stderr)
            return MLCompetition()

    if not competitors:
        return MLCompetition()

    # Ordeno por precio asc y me quedo con top 10
    competitors.sort(key=lambda c: c.price_ars)
    competitors = competitors[:10]

    ml_min = competitors[0].price_ars
    ml_max = max(c.price_ars for c in competitors)

    cheapest = competitors[0]
    if cheapest.seller_absorbs_installments:
        ml_min_terms = "Vendedor absorbe cuotas (sin interés para el comprador)"
    elif cheapest.installments_rate_pct and cheapest.installments_rate_pct > 0:
        ml_min_terms = (
            f"Cuotas con interés del {cheapest.installments_rate_pct}% (no absorbe el vendedor)"
        )
    else:
        ml_min_terms = "Sin cuotas / desconocido"

    # Proxy de demanda histórica (suma sold_qty de top 5 más baratos)
    monthly_demand_proxy = sum(c.sold_qty for c in competitors[:5])

    return MLCompetition(
        ml_min=ml_min,
        ml_max=ml_max,
        ml_min_terms=ml_min_terms,
        competitors=competitors,
        monthly_demand_proxy=monthly_demand_proxy,
    )
