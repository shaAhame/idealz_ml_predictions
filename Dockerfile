# Sales Forecasting API — multi-stage, slim, non-root.
ARG PYTHON_VERSION=3.13

# ---------- builder: resolve wheels into an isolated venv ----------
FROM python:${PYTHON_VERSION}-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime
# libgomp1: OpenMP runtime required by LightGBM
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    WEB_CONCURRENCY=1 \
    MODEL_SOURCE=supabase \
    MODEL_CACHE_DIR=/tmp/model_cache

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app ml/ ml/
COPY --chown=app:app api/ api/

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)" || exit 1

# PORT is injected by Render / Cloud Run / Railway. One worker per container is the
# norm behind an autoscaler; raise WEB_CONCURRENCY on a single larger VM instead.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 30 --no-server-header"]
