#!/usr/bin/env bash
# Refresh spec/openapi.json from Tripletex and print which paths came and went.
set -euo pipefail

URL="https://tripletex.no/v2/openapi.json"
DEST="$(cd "$(dirname "$0")/.." && pwd)/spec/openapi.json"
TMP=$(mktemp)
trap 'rm -f "$TMP" "$TMP.raw"' EXIT

curl -fsS --retry 3 "$URL?nocache=$(date +%s)" -o "$TMP.raw"
# Sorted keys and one value per line, so a weekly change reads as a small diff.
jq -S . "$TMP.raw" > "$TMP"

version=$(jq -r '.openapi // empty' "$TMP")
paths=$(jq '.paths | length' "$TMP")
if [[ "$version" != 3.* || "$paths" -lt 400 ]]; then
    echo "error: $URL does not look like the spec (openapi=$version, $paths paths)" >&2
    exit 1
fi

mkdir -p "$(dirname "$DEST")"
if [ -f "$DEST" ] && cmp -s "$TMP" "$DEST"; then
    echo "unchanged: $(jq -r .info.version "$DEST"), $paths paths"
    exit 0
fi

if [ -f "$DEST" ]; then
    changed=$(diff "$DEST" "$TMP" | grep -c '^[<>]' || true)
    echo "$(jq -r .info.version "$DEST") -> $(jq -r .info.version "$TMP"), $paths paths, $changed lines changed"
    diff <(jq -r '.paths | keys[]' "$DEST") <(jq -r '.paths | keys[]' "$TMP") \
        | sed -n 's/^</  removed/p; s/^>/  added  /p' || true
else
    echo "new: $(jq -r .info.version "$TMP"), $paths paths"
fi
install -m 644 "$TMP" "$DEST"
