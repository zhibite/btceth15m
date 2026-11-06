FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8036 \
    PAPER_TRADING=true \
    LIVE_ENABLED=false

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config.toml ./
COPY .env.example ./

RUN mkdir -p /app/data

EXPOSE 8036

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WEB_PORT','8036')+'/api/health', timeout=3)"

CMD ["python", "-m", "src.main"]
