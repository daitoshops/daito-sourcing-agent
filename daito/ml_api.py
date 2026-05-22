"""
Cliente de la API pública de Mercado Libre AR.
No requiere autenticación para búsquedas.

Endpoint: https://api.mercadolibre.com/sites/MLA/search?q=...
"""
from __future__ import annotations

import sys
from typing import List, Optional

import requests

from .models import MLCompetition, MLCompetitor


ML_SEARCH_URL = "https://api.mercadolibre.com/sites/MLA/search"

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
    "minifigura",  # cuando es solo la minifig suelta
    "minifig sola",
    "estuche",
    "funda",
    "calco",
    "sticker",
)

# Palabras que indican que el listado es "internacional" (no nos sirve)
INTERNATIONAL_TERMS = ("internacional", "international")


def _is_relevant(title: str, set_number: str, permalink: str = "") -> bool:
    """Filtra: el título debe contener el set_number y no ser internacional ni accesorio."""
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


def search_set(set_number: str, name: Optional[str] = None, limit: int = 20) -> MLCompetition:
    """
    Busca un set LEGO en Mercado Libre AR y devuelve datos de competencia.

    Construye una query con "LEGO {set_number}". Filtra por condición nueva.
    Parsea cada resultado, identifica si el vendedor absorbe cuotas
    (cuando installments.rate == 0 → seller absorbe ~12.3%).
    """
    query = f"LEGO {set_number}"
    if name:
        # Agrego el nombre pero sin saturar la query
        query = f"LEGO {set_number} {name.split()[0]}"

    params = {
        "q": query,
        "condition": "new",
        "limit": limit,
    }
    headers = {"User-Agent": "DaitoSourcingAgent/0.1"}

    try:
        resp = requests.get(ML_SEARCH_URL, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ml_api] Error consultando ML: {e}", file=sys.stderr)
        return MLCompetition()

    data = resp.json()
    results = data.get("results", [])

    competitors: List[MLCompetitor] = []
    for r in results:
        title = r.get("title", "")
        permalink = r.get("permalink", "")
        if not _is_relevant(title, set_number, permalink):
            continue

        installments = r.get("installments") or {}
        # rate=0 → el vendedor absorbe el costo financiero (sin interés)
        # rate>0 → el comprador paga interés
        rate = installments.get("rate")
        seller_absorbs = rate == 0 if rate is not None else False

        seller_info = r.get("seller") or {}
        seller_id = str(seller_info.get("id", "")) or r.get("seller_id", "")
        # Algunos resultados traen 'seller.nickname', si no hay usamos el id
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

    # Ordeno por precio asc y me quedo con top 10
    competitors.sort(key=lambda c: c.price_ars)
    competitors = competitors[:10]

    if not competitors:
        return MLCompetition()

    ml_min = competitors[0].price_ars
    ml_max = max(c.price_ars for c in competitors)

    # Describe la modalidad de cuotas del listado más barato
    cheapest = competitors[0]
    if cheapest.seller_absorbs_installments:
        ml_min_terms = "Vendedor absorbe cuotas (sin interés para el comprador)"
    elif cheapest.installments_rate_pct and cheapest.installments_rate_pct > 0:
        ml_min_terms = (
            f"Cuotas con interés del {cheapest.installments_rate_pct}% (no absorbe el vendedor)"
        )
    else:
        ml_min_terms = "Sin cuotas / desconocido"

    # Proxy de demanda: suma de sold_quantity de los top 5 más baratos.
    # OJO: este es el TOTAL HISTÓRICO del listado, no un volumen mensual.
    # Lo usamos como proxy comparativo entre sets, no como métrica absoluta.
    monthly_demand_proxy = sum(c.sold_qty for c in competitors[:5])

    return MLCompetition(
        ml_min=ml_min,
        ml_max=ml_max,
        ml_min_terms=ml_min_terms,
        competitors=competitors,
        monthly_demand_proxy=monthly_demand_proxy,
    )
