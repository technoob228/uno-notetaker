#!/bin/sh
# Runs as root only long enough to hand the folders to the computer's user,
# then drops to that user — files in ~/Meetings belong to the person, so
# Files in Uno Work can open, edit and delete them.
set -eu
if [ -z "${ADMIN_PASSWORD:-}" ] && [ "${ALLOW_NO_PASSWORD:-}" != "1" ]; then
  echo "ADMIN_PASSWORD is not set — refusing to start an open notetaker on the internet" >&2
  exit 1
fi
PUID="${PUID:-1000}"; PGID="${PGID:-$PUID}"
# The Uno Work settings file belongs to the computer's user: take its uid.
if [ -f /uno-work/settings.json ]; then
  PUID=$(stat -c %u /uno-work/settings.json); PGID=$(stat -c %g /uno-work/settings.json)
fi
for d in /meetings /state; do
  mkdir -p "$d"
  [ "$(stat -c %u "$d")" = "$PUID" ] || chown "$PUID:$PGID" "$d"
done
# ~/.uno may have been created by docker (as root) for the bind mount.
if [ -d /uno-dot ]; then
  [ "$(stat -c %u /uno-dot)" = "0" ] && chown "$PUID:$PGID" /uno-dot
  mkdir -p /uno-dot/apps && chown "$PUID:$PGID" /uno-dot/apps
  export UNO_APPS_DIR=/uno-dot/apps
fi
chown -R "$PUID:$PGID" /state 2>/dev/null || true
export HOME=/state
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
  uvicorn notetaker.main:app --host 0.0.0.0 --port "${PORT:-8430}" --proxy-headers --forwarded-allow-ips='*' --no-server-header
