# Daito Sourcing Agent

CLI en Python que analiza un producto LEGO desde una imagen (o URL de Amazon) e imprime un JSON listo para pegar en la wishlist (maqueta) de Daito Shops. Identifica el set con Claude vision, busca peso en 5 fuentes (Brickfact, Bricklink, Amazon US/UK, Google), consulta la competencia en Mercado Libre AR via API, calcula el costo landed según importador (Verónica o Gonzalo) y genera tres estrategias de precio con su veredicto.

## Instalación

```bash
cd ~/Documents/Daito/scripts/sourcing-agent
pip install -r requirements.txt

# Configurar la API key de Anthropic
cp .env.example .env
# Editar .env y poner tu clave en ANTHROPIC_API_KEY=...
```

Requiere Python 3.10+ (idealmente 3.11+).

## Uso

### Un producto (imagen local)
```bash
python daito_analyze.py imagen.png > out.json
```

### Un producto (URL de Amazon u otra)
```bash
python daito_analyze.py --url "https://www.amazon.com/dp/B0FMYTPFH3" > out.json
```

### Lote (carpeta con varias imágenes)
```bash
python daito_analyze.py --batch ./carpeta_imagenes/ --out-dir ./resultados/
```

### Modo verbose (logs en stderr; stdout sigue siendo JSON limpio)
```bash
python daito_analyze.py imagen.png -v > out.json
```

## Cómo pegar el resultado en la maqueta

1. Abrir la wishlist en la maqueta de Daito.
2. Click en **+ Nuevo análisis**.
3. Ir al tab **Pegar JSON**.
4. Copiar el contenido de `out.json` y pegarlo.
5. Click en **Importar**. La maqueta valida el schema y crea la ficha.

El JSON respeta exactamente el schema esperado (claves `product_name`, `weight_chosen_kg`, `strategies`, etc.). No modifiques los nombres de claves o la maqueta no va a aceptarlo.

## Customizar `config.toml`

Toda la lógica de negocio (dólar, tarifas de importadores, comisiones de ML) vive en `config.toml`. Si cambia algún parámetro, editás solo ese archivo sin tocar código.

Parámetros típicos a actualizar:
- `dolar_ars`: cotización del dólar para convertir USD → ARS.
- `ml_max_price_ars`: tope arriba del cual ML te baja el listado.
- `ml_commission_juguetes_pct`, `ml_installments_cost_pct`, `ml_shipping_ars`: estructura de costos de ML.
- `importers.veronica.tarifa_kg` / `importers.gonzalo.tarifa_kg`: tarifa por kilo de cada importador.
- `importers.veronica.max_weight_kg`: tope de peso que Vero puede traer (1.5 kg).

## Troubleshooting

- **Amazon devuelve 503 o página vacía**: te bloqueó por rate-limit. Esperar 1 minuto y reintentar. El scraper de Amazon es el más frágil; si falla, los otros 4 cubren.
- **Brickfact / Bricklink no encuentran el set**: para sets muy nuevos puede no estar todavía. El agente sigue funcionando con menos fuentes; revisar el `weight_chosen_method` en el JSON.
- **`ANTHROPIC_API_KEY missing`**: copiar `.env.example` a `.env` y completar.
- **JSON vacío de Mercado Libre**: probablemente el `set_number` no fue identificado correctamente. Reintentar con `-v` para ver qué identificó Claude.
- **Veredicto "bad" inesperado**: revisar `projected_cost_usd` y compararlo con `prices_competition_ars.ml_min`. Si el ML cap está limitando el precio (680000), el margen puede salir negativo aunque el set sea bueno.

## Roadmap

- **v2**: agregar otros importadores (Braun) y rubros (tattoo).
- **v2**: auto-escribir directo a la wishlist via API en vez de copiar/pegar JSON.
- **v2**: caché local de pesos y de búsquedas para no reconsultar.
