#!/usr/bin/env sh
set -eu
BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUT_DIR="$BASE_DIR/certs"
mkdir -p "$OUT_DIR"
if [ -f "$OUT_DIR/fullchain.pem" ] && [ -f "$OUT_DIR/privkey.pem" ]; then
  echo "certificate already exists: $OUT_DIR"
  exit 0
fi
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout "$OUT_DIR/privkey.pem" \
  -out "$OUT_DIR/fullchain.pem" \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 "$OUT_DIR/privkey.pem"
echo "generated development certificate in $OUT_DIR (not for public production use)"
