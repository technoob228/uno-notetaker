FROM python:3.12-slim
# ffmpeg: decode anything people upload, cut chunks; util-linux: setpriv (drop root)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg util-linux \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Speaker fingerprints (NeMo TitaNet-small, 40 MB) for telling voices apart.
ADD --checksum=sha256:ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx /opt/models/speaker.onnx
RUN chmod 644 /opt/models/speaker.onnx
COPY notetaker ./notetaker
COPY static ./static
COPY entrypoint.sh /entrypoint.sh
ENV PYTHONUNBUFFERED=1 PORT=8430 HF_HUB_DISABLE_TELEMETRY=1
EXPOSE 8430
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8430/healthz')" || exit 1
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
