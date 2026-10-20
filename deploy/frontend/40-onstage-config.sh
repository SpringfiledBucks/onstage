#!/bin/sh
set -eu
for value in "${ONSTAGE_API_ORIGIN:-}" "${ONSTAGE_WS_ORIGIN:-}"; do
  if [ -n "$value" ] && ! printf '%s' "$value" | grep -Eq '^(https?|wss?)://[A-Za-z0-9.-]+(:[0-9]+)?$'; then
    echo 'Onstage API/WS Origin must be a plain trusted Origin without path or credentials.' >&2
    exit 1
  fi
done
printf 'window.ONSTAGE_CONFIG = {"apiBaseUrl":"%s","wsBaseUrl":"%s"};\n' \
  "${ONSTAGE_API_ORIGIN:-}" "${ONSTAGE_WS_ORIGIN:-}" > /usr/share/nginx/html/config.js
