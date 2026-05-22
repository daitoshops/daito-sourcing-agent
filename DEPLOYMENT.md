# Despliegue de Daito Sourcing Agent (API)

Esta guía te lleva paso a paso desde el código local hasta tener una API
pública en `https://daito-sourcing-agent.onrender.com` que la maqueta HTML
puede llamar vía `fetch()`.

Costo total estimado: **USD 0/mes** (Render free + GitHub Actions free + ~USD 0.01 por análisis en Anthropic).

---

## 1. Pre-requisitos

Antes de empezar, asegurate de tener:

1. **Cuenta de GitHub** (gratis): https://github.com/signup
2. **Cuenta de Render** (gratis): https://render.com — recomendado registrarse con GitHub para que después conecte solo.
3. **API key de Anthropic**: https://console.anthropic.com/ (la misma que ya usás en `.env`).
4. **Token random para `DAITO_API_TOKEN`**: este token va a ser la "contraseña" que la maqueta manda en cada request. Generalo desde la terminal:

   ```bash
   openssl rand -hex 32
   ```

   Vas a obtener algo como `a3f2b1c8...64 caracteres`. **Guardalo en un lugar seguro** (1Password, Notas seguras, etc.). Lo vas a necesitar dos veces: una en Render, y otra en la maqueta.

---

## 2. Subir el código a GitHub

### Opción A: con Git desde la terminal (recomendado)

Desde la carpeta del proyecto (`/Users/juliantero/Documents/Daito/scripts/sourcing-agent/`):

```bash
cd /Users/juliantero/Documents/Daito/scripts/sourcing-agent/

# Inicializar el repo (solo la primera vez)
git init
git add .
git commit -m "initial: sourcing agent + API server"
```

Después, en https://github.com → click en **New repository**:
- Nombre: `daito-sourcing-agent`
- Visibilidad: **Private** (tu API key no se ve, pero igual mejor)
- **NO** marques "Add README" ni nada — el repo tiene que arrancar vacío.

GitHub te muestra un comando tipo:

```bash
git remote add origin https://github.com/TU_USUARIO/daito-sourcing-agent.git
git branch -M main
git push -u origin main
```

Copialo y corrélo en la terminal.

### Opción B: con GitHub Desktop (si no usás git)

1. Bajá GitHub Desktop: https://desktop.github.com/
2. **File → Add Local Repository** → seleccioná la carpeta `sourcing-agent`.
3. Como no es un repo todavía, te ofrece "create a repository". Aceptá.
4. Hacé un commit inicial con el botón abajo a la izquierda.
5. **Publish repository** → marcá **Keep this code private** → Publish.

### IMPORTANTE: verificar que `.env` NO se subió

Tu `.env` tiene la API key de Anthropic. **Nunca** debe terminar en GitHub.

Verificá que existe un `.gitignore` en la carpeta y que contiene `.env`. Si no existe, creá uno con este contenido antes del `git add .`:

```
.env
__pycache__/
*.pyc
.venv/
venv/
resultados/
.DS_Store
```

---

## 3. Crear el servicio en Render

1. Entrá a https://dashboard.render.com/
2. Click en **New +** (arriba a la derecha) → **Web Service**.
3. **Connect a repository** → autorizá GitHub si es la primera vez → elegí `daito-sourcing-agent`.
4. Render detecta automáticamente el `render.yaml` y propone:
   - **Name**: `daito-sourcing-agent`
   - **Runtime**: Docker
   - **Plan**: **Free**
   - **Health Check Path**: `/health`
5. Bajá hasta **Environment Variables** y agregá las dos variables (los valores los pegás vos, no se versionan):
   - `ANTHROPIC_API_KEY` → tu key de Anthropic (`sk-ant-...`)
   - `DAITO_API_TOKEN` → el token random que generaste en el paso 1
6. Click en **Create Web Service**.

Render arranca el build (toma 3–6 minutos la primera vez). En los logs vas a ver `pip install`, luego `Uvicorn running on http://0.0.0.0:10000`. Cuando aparece el cartel **Live** en verde, está listo.

La URL pública va a ser algo como:

```
https://daito-sourcing-agent.onrender.com
```

(El subdominio depende de cómo se llamó el servicio; lo ves arriba en el dashboard).

---

## 4. Probar el endpoint

Abrí una terminal nueva y probá los dos endpoints.

### Health check (sin autenticación)

```bash
curl https://daito-sourcing-agent.onrender.com/health
```

Respuesta esperada:

```json
{"status":"ok","version":"1.0"}
```

### Análisis (con autenticación)

Reemplazá `TU_TOKEN_ACA` por el `DAITO_API_TOKEN` que generaste:

```bash
curl -X POST https://daito-sourcing-agent.onrender.com/analyze \
  -H "X-API-Key: TU_TOKEN_ACA" \
  -H "Content-Type: application/json" \
  -d '{"supplier_url": "https://www.amazon.com/dp/B0FMYTPFH3"}'
```

La primera vez puede tardar 30–60 segundos (cold start + pipeline completo). Las siguientes corren en 5–15 segundos.

Si la respuesta es:

- `{"detail":"API key inválida o ausente."}` → revisá el header `X-API-Key`.
- `{"detail":"Hay que enviar..."}` → falta `image_url`, `supplier_url` o `image_b64` en el body.
- Una respuesta gigante con `product_name`, `verdict`, `strategies`, etc. → 🎉 funciona.

---

## 5. Mantener el servicio "caliente" (evitar cold starts)

Render free duerme el servicio después de 15 min sin tráfico. La primera request post-sueño tarda ~30 seg. Para evitarlo, hacemos que GitHub Actions le pegue al `/health` cada 10 minutos.

Creá el archivo `.github/workflows/keep-alive.yml` en el repo:

```yaml
name: Keep Render alive

on:
  schedule:
    - cron: '*/10 * * * *'  # cada 10 minutos
  workflow_dispatch:        # permite dispararlo a mano desde la UI

jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - name: Ping health endpoint
        run: curl -fsSL https://daito-sourcing-agent.onrender.com/health
```

Hacé commit + push de ese archivo. GitHub Actions arranca el cron solo.

### Sobre el costo de GitHub Actions

- **Repos públicos**: GitHub Actions es 100% gratis, sin límites razonables.
- **Repos privados**: 2000 minutos/mes gratis. Como cada ping dura ~5 segundos y corren 144 pings/día = unas 12 horas/mes de uso → alcanza de sobra.

---

## 6. Integración con la maqueta HTML

La maqueta ya trae el botón **"Analizar con IA"** que llama al endpoint (esto lo hace Claude en otra tarea en paralelo). Sólo necesitás dos cosas:

1. **La URL del servicio** (la que te dio Render, ej. `https://daito-sourcing-agent.onrender.com`).
2. **El `DAITO_API_TOKEN`** (el mismo random hex que cargaste en Render).

Esos dos valores se configuran adentro de la maqueta. La maqueta hace un `fetch` así:

```js
fetch("https://daito-sourcing-agent.onrender.com/analyze", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": "TU_TOKEN_ACA"
  },
  body: JSON.stringify({ supplier_url: "https://www.amazon.com/dp/..." })
})
```

El JSON que devuelve es **exactamente el mismo** schema que produce el CLI (`daito_analyze.py`), así que la maqueta lo pega directo en su wishlist.

---

## 7. Costos mensuales esperados

| Servicio | Plan | Costo |
|---|---|---|
| Render (hosting) | Free | USD 0 |
| GitHub Actions (keep-alive) | Free | USD 0 |
| Anthropic API (Claude Haiku) | Pay-as-you-go | ~USD 0.01 por análisis |

Si hacés 1000 análisis al mes, vas a gastar **~USD 10/mes** en Anthropic y nada en el resto.

---

## 8. Troubleshooting

### "Build failed" en Render
Mirá los logs. Si dice algo de `requirements.txt`, abrí el archivo y verificá que esté todo bien escrito. Si dice "Dockerfile not found", chequeá que el archivo se llame **exactamente** `Dockerfile` (con D mayúscula, sin extensión).

### "DAITO_API_TOKEN no está configurado en el servidor"
Te olvidaste de cargar la variable en Render. Dashboard → tu servicio → **Environment** → **Add Environment Variable**.

### El endpoint responde 502 o tarda muchísimo
Es cold start del plan free. Esperá 30–60 seg y reintentá. Después configurá el keep-alive del paso 5.

### Cambié código local, ¿cómo lo subo?
```bash
git add .
git commit -m "describe el cambio"
git push
```
Render detecta el push y redespliega automáticamente (porque `autoDeploy: true` en `render.yaml`).
