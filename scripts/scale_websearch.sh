#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <replica-count>" >&2
  exit 2
fi

replicas="$1"
case "$replicas" in
  ''|*[!0-9]*|0)
    echo "Replica count must be a positive integer." >&2
    exit 2
    ;;
esac

docker compose -f docker-compose.multi.yml up -d --scale "websearch=${replicas}" --no-recreate websearch
docker compose -f docker-compose.multi.yml up -d --no-deps --force-recreate nginx
