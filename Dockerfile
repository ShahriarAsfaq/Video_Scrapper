FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000

# ffmpeg: MP3 extraction + merging HD video/audio
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Optional: headless Chromium so JavaScript-injected players can be found.
#   docker compose build --build-arg WITH_BROWSER=1
ARG WITH_BROWSER=0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BROWSER_NO_SANDBOX=1
RUN if [ "$WITH_BROWSER" = "1" ]; then \
        pip install playwright && playwright install --with-deps chromium && chmod -R a+rX /ms-playwright; \
    fi

COPY app.py .

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'))"

# One worker + threads: the scan rate limiter lives in memory, so all requests must share a process.
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 600"]
