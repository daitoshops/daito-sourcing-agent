"""
Dataclasses para tipar los datos que circulan por el pipeline.
Mantener estos modelos sincronizados con el schema de salida JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


@dataclass
class LegoIdentification:
    """Resultado de la identificación del set por Claude."""
    is_lego: bool = True
    set_number: Optional[str] = None
    theme: Optional[str] = None
    name: Optional[str] = None
    pieces: Optional[int] = None
    age_min: Optional[int] = None
    retail_usd: Optional[float] = None
    confidence: Optional[int] = None
    image_url: Optional[str] = None  # url original si vino por --url


@dataclass
class WeightSource:
    """Una fuente individual de peso (Brickfact, Bricklink, Amazon, etc)."""
    source: str
    url: Optional[str] = None
    weight_naked_kg: Optional[float] = None
    weight_packaged_kg: Optional[float] = None
    error: Optional[str] = None  # si la fuente falló, registramos por qué


@dataclass
class WeightResult:
    """Resultado final del weight_finder: fuentes + valor elegido."""
    sources: List[WeightSource] = field(default_factory=list)
    weight_naked_kg: Optional[float] = None
    weight_packaged_kg: Optional[float] = None
    weight_chosen_kg: float = 1.0
    weight_chosen_method: str = ""


@dataclass
class MLCompetitor:
    """Un competidor en Mercado Libre AR."""
    seller: str
    price_ars: float
    sold_qty: int
    installments_rate_pct: Optional[float] = None
    seller_absorbs_installments: bool = False
    title: str = ""
    permalink: str = ""
    listing_type_id: str = ""


@dataclass
class MLCompetition:
    """Datos agregados de la competencia en ML AR."""
    ml_min: Optional[float] = None
    ml_max: Optional[float] = None
    ml_min_terms: str = ""  # describe cuotas del listado más barato
    competitors: List[MLCompetitor] = field(default_factory=list)
    monthly_demand_proxy: int = 0  # suma de sold_quantity top 5 (es histórico, no mensual)


@dataclass
class CostBreakdown:
    """Desglose del costo landed en USD."""
    supplier_usd: float
    taxes_us_usd: float
    import_cost_usd: float
    fee_usd: float
    total_usd: float
    importer: str  # "veronica" o "gonzalo"
    weight_used_kg: float


@dataclass
class Strategy:
    """Una estrategia de precio sugerida."""
    name: str
    price_ars: float
    with_installments: bool
    margin_pct: float
    margin_ars: float
    sales_likelihood: str  # high/medium/low
    reasoning: str


@dataclass
class AnalysisResult:
    """Resultado completo del análisis listo para serializar a JSON."""
    product_name: str
    brand: str = "LEGO"
    category: str = "DAITOYS"
    categoria_ml: str = "juguetes"
    image_url: Optional[str] = None
    weight_naked_kg: Optional[float] = None
    weight_packaged_kg: Optional[float] = None
    weight_chosen_kg: float = 1.0
    weight_chosen_method: str = ""
    weight_sources: List[Dict[str, Any]] = field(default_factory=list)
    prices_supplier: Dict[str, Optional[float]] = field(default_factory=dict)
    prices_competition_ars: Dict[str, Any] = field(default_factory=dict)
    puede_traer_veronica: bool = False
    importer_suggested: str = "gonzalo"
    projected_cost_usd: float = 0.0
    projected_price_ml_ars: float = 0.0
    projected_margin_ml_pct: float = 0.0
    projected_margin_ml_note: str = ""
    projected_price_daitoshops_ars: float = 0.0
    projected_margin_daitoshops_pct: float = 0.0
    verdict: str = "marginal"
    verdict_reason: str = ""
    strategies: List[Dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
