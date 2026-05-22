"""
Generación de estrategias de precio + veredicto final.

Tres estrategias:
  1. Match cuotas: alinear con ml_min que absorbe cuotas (incluye costo 12.3%).
  2. Cash-only undercut: 6% bajo ml_min, sin cuotas. Vende rápido.
  3. Premium: cerca del ml_max (95%). Cuando el stock es escaso.

Veredicto:
  - great   : mejor margen >= 50%
  - good    : 30-50%
  - marginal: 10-30%
  - bad     : < 10%
"""
from __future__ import annotations

from typing import List, Optional

from .models import CostBreakdown, MLCompetition, Strategy


def _compute_margin(
    price_ars: float,
    cost_landed_ars: float,
    with_installments: bool,
    cfg: dict,
) -> tuple[float, float, dict]:
    """
    Calcula (margen_ars, margen_pct, detalle_costos) para un precio dado.

    Costos a deducir del precio bruto:
      - comisión ML (15%)
      - IIBB (4.5%)
      - costo cuotas (12.3% si aplica)
      - envío ML (8170 ARS)
    """
    biz = cfg["business"]
    commission_pct = biz["ml_commission_juguetes_pct"] / 100.0
    iibb_pct = biz["iibb_pct"] / 100.0
    installments_pct = biz["ml_installments_cost_pct"] / 100.0
    shipping = biz["ml_shipping_ars"]

    commission = price_ars * commission_pct
    iibb = price_ars * iibb_pct
    installments_cost = price_ars * installments_pct if with_installments else 0.0
    total_costs = commission + iibb + installments_cost + shipping

    net_revenue = price_ars - total_costs
    margin_ars = net_revenue - cost_landed_ars
    margin_pct = (margin_ars / cost_landed_ars * 100.0) if cost_landed_ars > 0 else 0.0

    detail = {
        "gross_ars": round(price_ars, 2),
        "ml_commission_ars": round(commission, 2),
        "iibb_ars": round(iibb, 2),
        "installments_cost_ars": round(installments_cost, 2),
        "shipping_ars": round(shipping, 2),
        "total_costs_ars": round(total_costs, 2),
        "net_revenue_ars": round(net_revenue, 2),
        "cost_landed_ars": round(cost_landed_ars, 2),
        "net_margin_ars": round(margin_ars, 2),
        "net_margin_pct": round(margin_pct, 2),
    }
    return margin_ars, margin_pct, detail


def _sales_likelihood(price_ars: float, ml_min: float, matches_cuotas: bool) -> str:
    """Heurística simple para estimar likelihood de venta."""
    if ml_min <= 0:
        return "low"
    ratio = price_ars / ml_min
    if ratio <= 1.02 and matches_cuotas:
        return "high"
    if ratio <= 1.15:
        return "medium"
    return "low"


def generate_strategies(
    cost: CostBreakdown,
    competition: MLCompetition,
    cfg: dict,
) -> List[Strategy]:
    """
    Genera las 3 estrategias dado el costo landed y la competencia.
    Aplica el cap de ml_max_price_ars (680000 default).
    """
    dolar = cfg["business"]["dolar_ars"]
    ml_cap = cfg["business"]["ml_max_price_ars"]

    cost_landed_ars = cost.total_usd * dolar

    ml_min = competition.ml_min or 0.0
    ml_max = competition.ml_max or ml_min

    strategies: List[Strategy] = []

    # -----------------------------------------------------------------------
    # 1) Match cuotas
    # -----------------------------------------------------------------------
    # Si hay un competidor con seller_absorbs_installments=True, usamos su precio.
    # Si no, usamos ml_min y asumimos que matcheamos cuotas.
    matching_comp = next(
        (c for c in competition.competitors if c.seller_absorbs_installments),
        None,
    )
    price_match = (matching_comp.price_ars if matching_comp else ml_min) or 0.0
    price_match_capped = min(price_match, ml_cap) if price_match else 0.0

    margin_ars, margin_pct, _ = _compute_margin(
        price_match_capped, cost_landed_ars, with_installments=True, cfg=cfg
    )
    likelihood = _sales_likelihood(price_match_capped, ml_min, matches_cuotas=True) if ml_min else "low"
    reasoning = (
        "Alinea con el precio más barato que absorbe cuotas. "
        "Incluimos el 12.3% de costo financiero para que el cliente vea cuotas sin interés."
    )
    if price_match_capped < price_match:
        reasoning += f" Capeado al máximo permitido en ML ({ml_cap} ARS)."
    if not ml_min:
        reasoning = "Sin competencia detectada en ML; precio sugerido = 0 (REVISAR)."

    strategies.append(
        Strategy(
            name="Match cuotas",
            price_ars=round(price_match_capped, 2),
            with_installments=True,
            margin_pct=round(margin_pct, 2),
            margin_ars=round(margin_ars, 2),
            sales_likelihood=likelihood,
            reasoning=reasoning,
        )
    )

    # -----------------------------------------------------------------------
    # 2) Cash-only undercut
    # -----------------------------------------------------------------------
    price_cash = ml_min * 0.94 if ml_min else 0.0
    price_cash_capped = min(price_cash, ml_cap) if price_cash else 0.0
    margin_ars2, margin_pct2, _ = _compute_margin(
        price_cash_capped, cost_landed_ars, with_installments=False, cfg=cfg
    )
    likelihood2 = _sales_likelihood(price_cash_capped, ml_min, matches_cuotas=False) if ml_min else "low"
    # Cash-only generalmente vende rápido (6% más barato), así que subimos likelihood
    if likelihood2 == "medium":
        likelihood2 = "high"
    reasoning2 = (
        "6% por debajo del más barato de ML. Sin cuotas, así que ahorramos 12.3% en costo financiero. "
        "Ideal para rotar stock rápido."
    )
    strategies.append(
        Strategy(
            name="Cash-only undercut",
            price_ars=round(price_cash_capped, 2),
            with_installments=False,
            margin_pct=round(margin_pct2, 2),
            margin_ars=round(margin_ars2, 2),
            sales_likelihood=likelihood2,
            reasoning=reasoning2,
        )
    )

    # -----------------------------------------------------------------------
    # 3) Premium
    # -----------------------------------------------------------------------
    price_premium = ml_max * 0.95 if ml_max else 0.0
    price_premium_capped = min(price_premium, ml_cap) if price_premium else 0.0
    margin_ars3, margin_pct3, _ = _compute_margin(
        price_premium_capped, cost_landed_ars, with_installments=False, cfg=cfg
    )
    likelihood3 = _sales_likelihood(price_premium_capped, ml_min, matches_cuotas=False) if ml_min else "low"
    reasoning3 = (
        "5% por debajo del máximo del mercado. "
        "Conviene cuando el stock es escaso o tu reputación es premium."
    )
    strategies.append(
        Strategy(
            name="Premium",
            price_ars=round(price_premium_capped, 2),
            with_installments=False,
            margin_pct=round(margin_pct3, 2),
            margin_ars=round(margin_ars3, 2),
            sales_likelihood=likelihood3,
            reasoning=reasoning3,
        )
    )

    return strategies


def verdict_from_best_margin(best_pct: float) -> tuple[str, str]:
    """Mapea el mejor margen porcentual a veredicto + razón corta."""
    if best_pct >= 50:
        return "great", f"Margen excelente ({best_pct:.1f}%): vale la pena traerlo."
    if best_pct >= 30:
        return "good", f"Margen sólido ({best_pct:.1f}%): producto rentable."
    if best_pct >= 10:
        return "marginal", f"Margen marginal ({best_pct:.1f}%): solo si vende rápido."
    return "bad", f"Margen bajo ({best_pct:.1f}%): no conviene traerlo."


def daitoshops_price(ml_min: Optional[float], cfg: dict) -> float:
    """
    Precio para daitoshops.com.ar: 2% por debajo del ml_min como default
    (configurable en daitoshops_discount_vs_competition_pct).
    """
    if not ml_min:
        return 0.0
    discount = cfg["business"]["daitoshops_discount_vs_competition_pct"] / 100.0
    return round(ml_min * (1 - discount), 2)


def daitoshops_margin(price_ars: float, cost_landed_ars: float, cfg: dict) -> float:
    """
    Margen en daitoshops: descontamos solo IIBB + envío propio (sin comisión ML).
    """
    biz = cfg["business"]
    iibb_pct = biz["daitoshops_iibb_pct"] / 100.0
    shipping = biz["daitoshops_shipping_ars"]
    iibb = price_ars * iibb_pct
    net = price_ars - iibb - shipping - cost_landed_ars
    if cost_landed_ars <= 0:
        return 0.0
    return round(net / cost_landed_ars * 100.0, 2)
