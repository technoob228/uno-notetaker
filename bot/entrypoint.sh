#!/bin/sh
# A virtual screen (the browser runs headed: call pages treat headless
# Chromium as a bot and some refuse it) and a PulseAudio server for the
# calls' sound, then the bot.
set -eu
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
# -n: none of the desktop modules (udev, bluetooth…); the bot adds its sinks itself.
pulseaudio -n --daemonize=yes --exit-idle-time=-1 --disallow-exit --log-target=stderr \
  --load=module-native-protocol-unix >/dev/null 2>&1 || true
for i in 1 2 3 4 5 6 7 8 9 10; do pactl info >/dev/null 2>&1 && break; sleep 0.5; done
exec python -m notetaker_bot
