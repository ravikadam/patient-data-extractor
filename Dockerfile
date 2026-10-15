# Patient Data Extractor - portal + REST API (default) or MCP over HTTP (override the command).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HDX_DATA_DIR=/data HDX_CONFIG_DIR=/config

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY samples ./samples

RUN useradd --create-home --uid 10001 hdx && mkdir -p /data /config && chown hdx:hdx /data /config
USER hdx
VOLUME ["/data", "/config"]
EXPOSE 8000 8765

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/settings')" || exit 1

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
