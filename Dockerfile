FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_TRANSPORT=streamable-http \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/input /data/output \
    && chown -R app:app /data

COPY --chown=app:app server.py oauth.py ./

USER app
VOLUME ["/data"]
EXPOSE 8000

# No-op in stdio mode (no HTTP listener there)
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0) if os.environ.get('MCP_TRANSPORT')=='stdio' else urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["python", "server.py"]
