# syntax=docker/dockerfile:1
# Single image: FastAPI serves the API, the WebSocket and the built frontend from
# one origin, so no CORS or WebSocket proxy configuration is needed in production.

FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN useradd --create-home --uid 10001 twin
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/api ./api
COPY backend/sim ./sim
COPY backend/experiments ./experiments
COPY backend/scenarios ./scenarios
COPY --from=web /web/dist ./static

ENV STATIC_DIR=/app/static \
    PORT=8000 \
    CORS_ORIGINS="" \
    LOG_LEVEL=INFO
USER twin
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
# --proxy-headers: honour X-Forwarded-* from the hosting platform's proxy. Set
# FORWARDED_ALLOW_IPS to that proxy's address (uvicorn reads it from the env).
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
