#!/usr/bin/env bash
set -euo pipefail

SEARXNG_SECRET_KEY="${SEARXNG_SECRET_KEY:-}"

export WEBSEARCH_CONFIG_PATH="${WEBSEARCH_CONFIG_PATH:-/app/config.yaml}"

readarray -t _runtime_values < <(python - <<'PY'
from urllib.parse import urlparse

from utils.config import get_config_value

settings_path = str(get_config_value("runtime.searxng_settings_path", "/etc/searxng/settings.yml"))
api_host = str(get_config_value("server.api_host", "0.0.0.0"))
api_port = int(get_config_value("server.api_port", 9000))
searxng_base_url = str(get_config_value("service.searxng_base_url", "http://127.0.0.1:8080"))
searxng_bind_host = str(get_config_value("service.searxng_bind_host", "0.0.0.0"))
parsed = urlparse(searxng_base_url)
searxng_port = parsed.port or 8080

print(settings_path)
print(api_host)
print(api_port)
print(searxng_bind_host)
print(searxng_port)
PY
)

SEARXNG_SETTINGS_PATH="${_runtime_values[0]}"
WEBSEARCH_API_HOST="${_runtime_values[1]}"
WEBSEARCH_API_PORT="${_runtime_values[2]}"
SEARXNG_BIND_HOST="${_runtime_values[3]}"
SEARXNG_BIND_PORT="${_runtime_values[4]}"
export SEARXNG_SETTINGS_PATH

cleanup() {
  kill ${SEARX_PID:-0} >/dev/null 2>&1 || true
  kill ${API_PID:-0} >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Render a runtime SearXNG settings file with a non-committed secret key.
if [ -z "$SEARXNG_SECRET_KEY" ]; then
  SEARXNG_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
  echo "SEARXNG_SECRET_KEY not set; generated an ephemeral runtime key." >&2
fi

secret_escaped="$(printf '%s' "$SEARXNG_SECRET_KEY" | sed 's/[&|]/\\&/g')"
RUNTIME_SEARXNG_SETTINGS_PATH="/tmp/searxng-settings.runtime.yml"
sed "s|__SEARXNG_SECRET_KEY__|$secret_escaped|g" "$SEARXNG_SETTINGS_PATH" > "$RUNTIME_SEARXNG_SETTINGS_PATH"
export SEARXNG_SETTINGS_PATH="$RUNTIME_SEARXNG_SETTINGS_PATH"

# Start SearXNG locally in this same container.
/opt/searxng-venv/bin/granian searx.webapp:app \
  --interface wsgi \
  --host "$SEARXNG_BIND_HOST" \
  --port "$SEARXNG_BIND_PORT" &
SEARX_PID=$!

# Wait for SearXNG readiness.
ready=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${SEARXNG_BIND_PORT}/search?q=health&format=json" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  echo "SearXNG failed readiness check on ${SEARXNG_BIND_PORT}" >&2
  exit 1
fi

python /app/main.py serve --host "$WEBSEARCH_API_HOST" --port "$WEBSEARCH_API_PORT" &
API_PID=$!

wait -n "$SEARX_PID" "$API_PID"
