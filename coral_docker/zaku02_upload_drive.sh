#!/usr/bin/env bash
set -euo pipefail

# Args
WEBAPP_URL="${1:-}"
IMG="${2:-}"

# Normalize possible CR from Windows-edited .env files
WEBAPP_URL="${WEBAPP_URL%$'\r'}"

# Basic validation
if [[ -z "$WEBAPP_URL" ]]; then
  echo "ERR: GAS_UPLOAD_URL is empty" >&2
  exit 2
fi
if [[ -z "$IMG" || ! -f "$IMG" ]]; then
  echo "ERR: image path invalid: $IMG" >&2
  exit 3
fi

# Encode image (no newlines)
B64="$(base64 -w0 "$IMG")"

# POST
curl -sS -X POST -H "Content-Type: application/octet-stream" \
     --data-binary "$B64" \
     "${WEBAPP_URL}?filename=$(basename "$IMG")"
