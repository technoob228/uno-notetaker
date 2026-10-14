# Uno Notetaker bot: joins calls as a guest and streams their sound to the app.
# Chromium (headed, on a virtual screen) + PulseAudio (one silent sink per call) + ffmpeg.
FROM python:3.12-slim-bookworm
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils xvfb ffmpeg fonts-noto-core \
      fonts-noto-color-emoji tini \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /bot
COPY requirements.txt .
# Chromium with its system libraries (Playwright's own build: no proprietary
# codecs, but every call platform sends Opus audio).
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache
# PulseAudio refuses to run as root: the bot runs as its own user.
RUN useradd --create-home --uid 1000 bot
COPY notetaker_bot ./notetaker_bot
COPY entrypoint.sh /entrypoint.sh
USER bot
ENV NOTETAKER_URL=http://notetaker:8430 BOT_KEY_FILE=/bot-key/token MAX_CALLS=2 DISPLAY=:99
ENTRYPOINT ["/usr/bin/tini", "--", "/bin/sh", "/entrypoint.sh"]
