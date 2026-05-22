"""
Buscador de peso para un set LEGO. Consulta 5 fuentes y elige la mediana.

Fuentes (en orden de prioridad):
  1. Brickfact     - peso CON caja (package)
  2. Bricklink     - peso CON caja
  3. Amazon US     - "Item Weight" en libras (a veces solo bricks)
  4. Amazon UK     - "Item Weight" en kg (a veces solo bricks)
  5. Google search - texto "approximately X.XX kg" del AI Overview

Brickfact y Bricklink son los más confiables para peso packaged.
Amazon es sanity check (a veces es naked).
"""
from __future__ import annotations

import re
import statistics
import sys
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from .models import WeightSource, WeightResult


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}

TIMEOUT = 20


def _slugify(text: str) -> str:
    """Slug simple para construir URLs de Brickfact."""
    if not text:
        return ""
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def _safe_get(url: str) -> Optional[str]:
    """GET con manejo de errores. Devuelve HTML o None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.text
    except requests.RequestException:
        return None


# ---------------------------------------------------------------------------
# Brickfact
# ---------------------------------------------------------------------------
def fetch_brickfact(set_number: str, theme: Optional[str] = None, name: Optional[str] = None) -> WeightSource:
    """
    Brickfact suele reportar peso CON caja.
    URL canónica: https://brickfact.com/sets/lego-{theme-slug}-{name-slug}-{set_number}
    Fallback: búsqueda por set_number.
    """
    src = WeightSource(source="Brickfact")

    # 1) Intento URL directa si tengo theme + name
    candidates: List[str] = []
    if theme and name:
        candidates.append(
            f"https://brickfact.com/sets/lego-{_slugify(theme)}-{_slugify(name)}-{set_number}"
        )
    # 2) Fallback: search
    candidates.append(f"https://brickfact.com/search?q={set_number}")

    html: Optional[str] = None
    used_url: Optional[str] = None
    for url in candidates:
        html = _safe_get(url)
        if html and "Weight" in html:
            used_url = url
            break

    if not html:
        src.error = "no response"
        return src

    src.url = used_url

    # Si fue la search, intento encontrar el primer link y seguirlo
    if "search?q=" in (used_url or ""):
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("a", href=re.compile(r"/sets/lego-"))
        if link and link.get("href"):
            detail_url = link["href"]
            if detail_url.startswith("/"):
                detail_url = "https://brickfact.com" + detail_url
            html2 = _safe_get(detail_url)
            if html2:
                html = html2
                src.url = detail_url

    # Buscar patrón "Weight ... X.XXX kg"
    m = re.search(r"Weight[^\d]{0,40}(\d+(?:[.,]\d+)?)\s*kg", html, re.IGNORECASE)
    if m:
        weight_kg = float(m.group(1).replace(",", "."))
        src.weight_packaged_kg = weight_kg
        return src

    src.error = "no weight parsed"
    return src


# ---------------------------------------------------------------------------
# Bricklink
# ---------------------------------------------------------------------------
def fetch_bricklink(set_number: str) -> WeightSource:
    """
    Bricklink: https://www.bricklink.com/v2/catalog/catalogitem.page?S={set_number}-1
    El peso aparece como "Weight: XXXXg" o "Weight: X.XXg".
    """
    src = WeightSource(source="Bricklink")
    url = f"https://www.bricklink.com/v2/catalog/catalogitem.page?S={set_number}-1"
    src.url = url
    html = _safe_get(url)
    if not html:
        src.error = "no response"
        return src

    # Patrón típico: "Weight: 1,143.50g" o "Weight: 1143g"
    m = re.search(r"Weight:\s*([\d.,]+)\s*g\b", html, re.IGNORECASE)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            grams = float(raw)
            src.weight_packaged_kg = round(grams / 1000.0, 3)
            return src
        except ValueError:
            pass

    src.error = "no weight parsed"
    return src


# ---------------------------------------------------------------------------
# Amazon US
# ---------------------------------------------------------------------------
def _amazon_search_to_dp(html_search: str, base: str) -> Optional[str]:
    """Devuelve el primer URL /dp/ que aparece en la página de búsqueda."""
    soup = BeautifulSoup(html_search, "html.parser")
    a = soup.find("a", href=re.compile(r"/dp/[A-Z0-9]{10}"))
    if a and a.get("href"):
        href = a["href"]
        if href.startswith("/"):
            return base + href
        return href
    return None


def fetch_amazon_us(set_number: str) -> WeightSource:
    """
    Amazon US: peso suele venir como "Item Weight: X.XX Pounds".
    Convertimos a kg (1 lb = 0.4536 kg).
    """
    src = WeightSource(source="Amazon US")
    search_url = f"https://www.amazon.com/s?k=LEGO+{set_number}"
    html = _safe_get(search_url)
    if not html:
        src.error = "search blocked"
        src.url = search_url
        return src

    dp_url = _amazon_search_to_dp(html, "https://www.amazon.com")
    if not dp_url:
        src.error = "no dp link"
        src.url = search_url
        return src

    src.url = dp_url
    detail = _safe_get(dp_url)
    if not detail:
        src.error = "dp blocked"
        return src

    # Patrones de peso. Amazon mezcla "Item Weight" e "Item weight"
    # Puede venir en pounds o en ounces.
    m = re.search(r"Item[\s\-]?[Ww]eight[^\d]{0,30}(\d+(?:\.\d+)?)\s*Pounds", detail)
    if m:
        lbs = float(m.group(1))
        kg = round(lbs * 0.4536, 3)
        # Amazon a veces reporta SOLO bricks → naked
        src.weight_naked_kg = kg
        return src
    m = re.search(r"Item[\s\-]?[Ww]eight[^\d]{0,30}(\d+(?:\.\d+)?)\s*Kilograms", detail)
    if m:
        src.weight_naked_kg = round(float(m.group(1)), 3)
        return src
    m = re.search(r"Item[\s\-]?[Ww]eight[^\d]{0,30}(\d+(?:\.\d+)?)\s*Ounces", detail)
    if m:
        oz = float(m.group(1))
        src.weight_naked_kg = round(oz * 0.02835, 3)
        return src

    src.error = "no weight parsed"
    return src


# ---------------------------------------------------------------------------
# Amazon UK
# ---------------------------------------------------------------------------
def fetch_amazon_uk(set_number: str) -> WeightSource:
    """
    Amazon UK: "Item Weight: X.XX Kilograms" o "Item weight: X.XX Kilograms".
    """
    src = WeightSource(source="Amazon UK")
    search_url = f"https://www.amazon.co.uk/s?k=LEGO+{set_number}"
    html = _safe_get(search_url)
    if not html:
        src.error = "search blocked"
        src.url = search_url
        return src

    dp_url = _amazon_search_to_dp(html, "https://www.amazon.co.uk")
    if not dp_url:
        src.error = "no dp link"
        src.url = search_url
        return src

    src.url = dp_url
    detail = _safe_get(dp_url)
    if not detail:
        src.error = "dp blocked"
        return src

    m = re.search(r"Item[\s\-]?[Ww]eight[^\d]{0,30}(\d+(?:\.\d+)?)\s*Kilograms", detail)
    if m:
        src.weight_naked_kg = round(float(m.group(1)), 3)
        return src
    m = re.search(r"Item[\s\-]?[Ww]eight[^\d]{0,30}(\d+(?:\.\d+)?)\s*g\b", detail)
    if m:
        src.weight_naked_kg = round(float(m.group(1)) / 1000.0, 3)
        return src

    src.error = "no weight parsed"
    return src


# ---------------------------------------------------------------------------
# Google AI Overview
# ---------------------------------------------------------------------------
def fetch_google(set_number: str) -> WeightSource:
    """
    Google search: buscamos texto del tipo "approximately X.XX kg" o
    "weight of X.XX kg" en la página de resultados (AI Overview).

    Esta fuente es la menos confiable; Google bloquea bastante.
    """
    src = WeightSource(source="Google")
    query = quote_plus(f'"LEGO {set_number}" weight kg')
    url = f"https://www.google.com/search?q={query}"
    src.url = url
    html = _safe_get(url)
    if not html:
        src.error = "no response (probable block)"
        return src

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    patterns = [
        r"approximately\s+(\d+(?:\.\d+)?)\s*kg",
        r"weight of\s+(\d+(?:\.\d+)?)\s*kg",
        r"weighs\s+(?:approximately\s+)?(\d+(?:\.\d+)?)\s*kg",
        r"(\d+(?:\.\d+)?)\s*kg",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                kg = float(m.group(1))
                if 0.05 < kg < 30:  # sanity range
                    src.weight_packaged_kg = round(kg, 3)
                    return src
            except ValueError:
                continue

    src.error = "no weight parsed"
    return src


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------
def _median_discarding_outliers(values: List[float], tol: float = 0.15) -> Tuple[float, List[float]]:
    """Devuelve (mediana_final, valores_usados) descartando outliers >tol del mediano."""
    if not values:
        return 0.0, []
    med = statistics.median(values)
    keep = [v for v in values if abs(v - med) / med <= tol]
    if not keep:
        keep = values
    return statistics.median(keep), keep


def find_weight(
    set_number: str,
    theme: Optional[str] = None,
    name: Optional[str] = None,
    parallel: bool = True,
) -> WeightResult:
    """
    Llama las 5 fuentes (en paralelo) y agrega resultados.
    Elige el peso packaged como mediana de las fuentes confiables,
    descartando outliers >15%.
    """
    fetchers = [
        ("Brickfact", lambda: fetch_brickfact(set_number, theme, name)),
        ("Bricklink", lambda: fetch_bricklink(set_number)),
        ("Amazon US", lambda: fetch_amazon_us(set_number)),
        ("Amazon UK", lambda: fetch_amazon_uk(set_number)),
        ("Google", lambda: fetch_google(set_number)),
    ]

    sources: List[WeightSource] = []
    if parallel:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(fn): name for name, fn in fetchers}
            for fut in as_completed(futures):
                try:
                    sources.append(fut.result())
                except Exception as e:
                    sources.append(
                        WeightSource(source=futures[fut], error=f"crash: {e}")
                    )
    else:
        for _, fn in fetchers:
            try:
                sources.append(fn())
            except Exception as e:
                sources.append(WeightSource(source="?", error=f"crash: {e}"))

    # Reordeno para que el orden de prioridad sea estable
    order = {"Brickfact": 0, "Bricklink": 1, "Amazon US": 2, "Amazon UK": 3, "Google": 4}
    sources.sort(key=lambda s: order.get(s.source, 99))

    # Pesos packaged confiables (Brickfact, Bricklink, Google)
    packaged_candidates = [
        s.weight_packaged_kg for s in sources if s.weight_packaged_kg is not None
    ]
    # Pesos naked (Amazon)
    naked_candidates = [s.weight_naked_kg for s in sources if s.weight_naked_kg is not None]

    packaged_kg: Optional[float] = None
    naked_kg: Optional[float] = None

    if packaged_candidates:
        packaged_kg, _ = _median_discarding_outliers(packaged_candidates)
    if naked_candidates:
        naked_kg, _ = _median_discarding_outliers(naked_candidates)

    # Elegir el peso "chosen" para cotizar:
    # - Si hay packaged: usar packaged (es el real para envío)
    # - Si solo hay naked: estimar packaged = naked * 1.15 (caja típica ~15%)
    if packaged_kg:
        chosen = packaged_kg
        method = (
            f"Mediana de {len(packaged_candidates)} fuentes packaged "
            "(Brickfact/Bricklink son los más confiables)"
        )
    elif naked_kg:
        chosen = round(naked_kg * 1.15, 3)
        method = (
            f"No hubo peso packaged disponible; estimado como naked ({naked_kg} kg) × 1.15"
        )
    else:
        chosen = 1.0
        method = "Sin datos: usando default 1.0 kg (REVISAR MANUALMENTE)"

    return WeightResult(
        sources=sources,
        weight_naked_kg=naked_kg,
        weight_packaged_kg=packaged_kg,
        weight_chosen_kg=chosen,
        weight_chosen_method=method,
    )
