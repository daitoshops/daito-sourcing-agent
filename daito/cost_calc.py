"""
Cálculo de costo landed (puesto en Argentina) y selección de importador.

Importadores:
  - veronica: peso NAKED, tarifa 38 USD/kg + fee 4 USD por envío. Solo hasta 1.5 kg.
  - gonzalo:  peso PACKAGED, tarifa 40 USD/kg, sin fee fijo.

Decisión: si peso packaged <= 1.5 kg → puede traer Verónica; comparamos costos
y elegimos el más barato.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Tuple

from .models import CostBreakdown


def _load_toml(path: Path) -> dict:
    """Carga TOML usando tomllib (>=3.11) o tomli."""
    try:
        import tomllib  # type: ignore
        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        import tomli  # type: ignore
        with open(path, "rb") as f:
            return tomli.load(f)


_CONFIG_CACHE: Optional[dict] = None


def load_config(config_path: Optional[str] = None) -> dict:
    """Carga config.toml. Por default, el del directorio del proyecto."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and config_path is None:
        return _CONFIG_CACHE

    if config_path is None:
        # config.toml al lado del paquete daito/
        p = Path(__file__).resolve().parent.parent / "config.toml"
    else:
        p = Path(config_path)
    cfg = _load_toml(p)
    if config_path is None:
        _CONFIG_CACHE = cfg
    return cfg


def cost_landed_usd(
    supplier_usd: float,
    weight_naked_kg: Optional[float],
    weight_packaged_kg: Optional[float],
    importer: str,
    config: Optional[dict] = None,
) -> CostBreakdown:
    """
    Calcula el costo landed (USD) según el importador elegido.

    - veronica: usa peso NAKED (los bricks sin caja); cobra fee fijo por envío.
    - gonzalo: usa peso PACKAGED (con caja).
    """
    cfg = config or load_config()
    business = cfg["business"]
    importers = cfg["importers"]

    taxes_pct = business["taxes_us_pct"] / 100.0  # 7% expresado como 0.07
    taxes = supplier_usd * taxes_pct

    if importer == "veronica":
        weight_used = weight_naked_kg if weight_naked_kg is not None else weight_packaged_kg
        if weight_used is None:
            weight_used = 1.0
        tarifa = importers["veronica"]["tarifa_kg"]
        fee = importers["veronica"]["fee_per_envio"]
        import_cost = weight_used * tarifa
    else:  # gonzalo
        weight_used = weight_packaged_kg if weight_packaged_kg is not None else weight_naked_kg
        if weight_used is None:
            weight_used = 1.0
        tarifa = importers["gonzalo"]["tarifa_kg"]
        fee = importers["gonzalo"]["fee_per_envio"]
        import_cost = weight_used * tarifa

    total = supplier_usd + taxes + import_cost + fee
    return CostBreakdown(
        supplier_usd=round(supplier_usd, 2),
        taxes_us_usd=round(taxes, 2),
        import_cost_usd=round(import_cost, 2),
        fee_usd=round(fee, 2),
        total_usd=round(total, 2),
        importer=importer,
        weight_used_kg=round(weight_used, 3),
    )


def pick_importer(
    supplier_usd: float,
    weight_naked_kg: Optional[float],
    weight_packaged_kg: Optional[float],
    config: Optional[dict] = None,
) -> Tuple[CostBreakdown, CostBreakdown, str, bool]:
    """
    Devuelve (mejor_breakdown, breakdown_gonzalo, importer_elegido, puede_traer_veronica).

    Regla: Verónica solo puede traer si packaged <= 1.5 kg.
    Comparamos costos y elegimos el más barato.
    """
    cfg = config or load_config()
    max_w_vero = cfg["importers"]["veronica"]["max_weight_kg"]

    # ¿Verónica puede traerlo? El criterio es por peso packaged (lo que viaja)
    weight_for_decision = weight_packaged_kg if weight_packaged_kg is not None else weight_naked_kg
    puede_vero = bool(weight_for_decision and weight_for_decision <= max_w_vero)

    cost_gonzalo = cost_landed_usd(
        supplier_usd, weight_naked_kg, weight_packaged_kg, "gonzalo", cfg
    )

    if puede_vero:
        cost_vero = cost_landed_usd(
            supplier_usd, weight_naked_kg, weight_packaged_kg, "veronica", cfg
        )
        # Elegir el más barato
        if cost_vero.total_usd <= cost_gonzalo.total_usd:
            return cost_vero, cost_gonzalo, "veronica", True
        return cost_gonzalo, cost_gonzalo, "gonzalo", True

    return cost_gonzalo, cost_gonzalo, "gonzalo", False
