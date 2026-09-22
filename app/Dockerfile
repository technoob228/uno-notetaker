FROM python:3.12-slim
# ffmpeg: decode anything people upload, cut chunks; util-linux: setpriv (drop root)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg util-linux \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY notetaker ./notetaker
COPY static ./static
COPY entrypoint.sh /entrypoint.sh
ENV PYTHONUNBUFFERED=1 PORT=8430 HF_HUB_DISABLE_TELEMETRY=1
EXPOSE 8430
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8430/healthz')" || exit 1
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
