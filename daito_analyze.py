#!/usr/bin/env python3
"""
daito_analyze.py
=================
CLI principal del sourcing-agent de Daito Shops.

Toma una imagen (o URL) de un producto LEGO, identifica el set,
busca precio y peso, calcula costos y devuelve un JSON listo para
pegar en la wishlist (maqueta) de Daito.

Uso:
    python daito_analyze.py imagen.png > out.json
    python daito_analyze.py --url "https://www.amazon.com/dp/B0FMYTPFH3" > out.json
    python daito_analyze.py --batch ./carpeta/ --out-dir ./resultados/
    python daito_analyze.py imagen.png -v       # logs en stderr

Salida (stdout): JSON limpio según el schema de la wishlist.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Cargar .env si está disponible
try:
    from dotenv import load_dotenv  # type: ignore

    # .env al lado del script
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from daito import identify, ml_api, weight_finder, cost_calc, strategy
from daito.models import AnalysisResult


VERBOSE = False


def log(msg: str) -> None:
    """Imprime en stderr solo si verbose está activo."""
    if VERBOSE:
        print(f"[daito] {msg}", file=sys.stderr)


def analyze_one(
    image_path: Optional[str] = None,
    url: Optional[str] = None,
    cfg: Optional[dict] = None,
) -> dict:
    """
    Pipeline completo para un único producto.
    Devuelve un dict listo para JSON.
    """
    cfg = cfg or cost_calc.load_config()

    # 1) Identificar el set ----------------------------------------------------
    log("Identificando set LEGO con Claude...")
    if url:
        ident = identify.identify_from_url(url)
    elif image_path:
        ident = identify.identify_from_image(image_path)
    else:
        raise ValueError("Hay que pasar image_path o url")

    if not ident.is_lego:
        # No es LEGO: devolvemos un resultado mínimo con verdict=bad
        log("La imagen no es un producto LEGO.")
        result = AnalysisResult(
            product_name="(no identificado como LEGO)",
            verdict="bad",
            verdict_reason="La imagen no corresponde a un producto LEGO.",
            notes="Identificación rechazada por Claude.",
        )
        return result.to_dict()

    log(
        f"Set: {ident.set_number} - {ident.name} "
        f"(tema {ident.theme}, {ident.pieces} piezas, confianza {ident.confidence}%)"
    )

    if not ident.set_number:
        log("ADVERTENCIA: no se obtuvo set_number, los scrapers pueden fallar.")

    # 2) Disparar scrapers en paralelo -----------------------------------------
    log("Buscando peso (5 fuentes) y competencia ML en paralelo...")

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

    log(
        f"Peso elegido: {weight_res.weight_chosen_kg} kg "
        f"(packaged={weight_res.weight_packaged_kg}, naked={weight_res.weight_naked_kg})"
    )
    log(
        f"Competencia ML: min={ml_res.ml_min} max={ml_res.ml_max} "
        f"({len(ml_res.competitors)} competidores)"
    )

    # 3) Calcular costos y elegir importador -----------------------------------
    supplier_usd = ident.retail_usd or 0.0
    log(f"Precio supplier (retail USD): {supplier_usd}")

    best_cost, _gonzalo_cost, importer, puede_vero = cost_calc.pick_importer(
        supplier_usd,
        weight_res.weight_naked_kg,
        weight_res.weight_packaged_kg,
        cfg,
    )
    log(
        f"Importador elegido: {importer} "
        f"(landed USD = {best_cost.total_usd}, puede_traer_veronica={puede_vero})"
    )

    # 4) Generar estrategias ---------------------------------------------------
    strategies = strategy.generate_strategies(best_cost, ml_res, cfg)
    log(f"Estrategias generadas: {len(strategies)}")

    best_margin = max((s.margin_pct for s in strategies), default=0.0)
    verdict, verdict_reason = strategy.verdict_from_best_margin(best_margin)

    # Estrategia "Match cuotas" como precio proyectado por defecto
    proj_strategy = strategies[0] if strategies else None
    proj_price_ml = proj_strategy.price_ars if proj_strategy else 0.0
    proj_margin_ml = proj_strategy.margin_pct if proj_strategy else 0.0
    proj_margin_note = proj_strategy.reasoning if proj_strategy else ""

    # 5) Precio en daitoshops.com.ar -------------------------------------------
    daito_price = strategy.daitoshops_price(ml_res.ml_min, cfg)
    cost_landed_ars = best_cost.total_usd * cfg["business"]["dolar_ars"]
    daito_margin = strategy.daitoshops_margin(daito_price, cost_landed_ars, cfg)

    # 6) Ensamblar resultado ---------------------------------------------------
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


def run_single(image_path: Optional[str], url: Optional[str]) -> int:
    """Ejecuta el análisis y lo imprime a stdout."""
    try:
        result = analyze_one(image_path=image_path, url=url)
    except Exception as e:
        print(f"[daito] ERROR: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def run_batch(folder: str, out_dir: str) -> int:
    """Procesa todas las imágenes de una carpeta y escribe un JSON por cada una."""
    src = Path(folder)
    dst = Path(out_dir)
    dst.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        print(f"[daito] ERROR: no es directorio: {folder}", file=sys.stderr)
        return 2

    exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    images = sorted(p for p in src.iterdir() if p.suffix.lower() in exts)
    if not images:
        print(f"[daito] No se encontraron imágenes en {folder}", file=sys.stderr)
        return 1

    errors = 0
    for img in images:
        log(f"Procesando {img.name}...")
        try:
            result = analyze_one(image_path=str(img))
            out_path = dst / f"{img.stem}.json"
            out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            log(f"  -> {out_path}")
        except Exception as e:
            print(f"[daito] ERROR en {img.name}: {e}", file=sys.stderr)
            errors += 1
    return 0 if errors == 0 else 1


def main() -> int:
    global VERBOSE

    parser = argparse.ArgumentParser(
        description="Analiza un producto LEGO y genera el JSON para la wishlist de Daito.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("image", nargs="?", help="Path a una imagen del producto.")
    parser.add_argument("--url", help="URL del producto (Amazon, etc.) en vez de imagen.")
    parser.add_argument("--batch", help="Carpeta con varias imágenes para procesar en lote.")
    parser.add_argument(
        "--out-dir",
        default="./resultados",
        help="Carpeta de salida en modo --batch (default: ./resultados).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Logs en stderr.")
    args = parser.parse_args()

    VERBOSE = args.verbose

    if args.batch:
        return run_batch(args.batch, args.out_dir)

    if not args.image and not args.url:
        parser.print_help()
        return 1

    return run_single(args.image, args.url)


if __name__ == "__main__":
    sys.exit(main())
