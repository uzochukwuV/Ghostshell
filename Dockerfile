FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system ghostshell \
    && useradd --system --gid ghostshell --create-home --home-dir /app ghostshell

COPY --chown=ghostshell:ghostshell fastapi_app ./fastapi_app

# Zeabur injects PORT; 12000 is the local default from the README.
ENV PORT=12000
EXPOSE 12000

# Health checks do not require any provider credentials and must remain local to
# the container; the public API is protected at the deployment ingress.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT', '12000') + '/api/healthz', timeout=3)"

USER ghostshell
CMD ["sh", "-c", "uvicorn fastapi_app.main:app --host 0.0.0.0 --port ${PORT:-12000}"]
