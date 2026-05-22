# Imagen base liviana de Python 3.11
FROM python:3.11-slim

# Variables de entorno para que Python se comporte bien dentro del contenedor
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Instalamos primero las dependencias (mejor cacheo de capas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto del código del proyecto
COPY . .

# Render asigna el puerto vía $PORT; en local usamos 10000 por default
EXPOSE 10000

# Arranque del servidor. Usamos shell form para que ${PORT:-10000} se expanda.
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}
