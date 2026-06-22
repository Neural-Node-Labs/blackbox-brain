#!/usr/bin/env bash
# Deploy the Blackbox Brain stack (postgres + gateway + console).
#
# Usage:
#   ./deploy.sh up        Build and start everything (default)
#   ./deploy.sh down       Stop and remove containers (keeps volumes)
#   ./deploy.sh down -v    Stop and remove containers AND volumes (destroys data)
#   ./deploy.sh logs       Tail logs from all services
#   ./deploy.sh status     Show container health/status

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker &>/dev/null; then
  echo "ERROR: docker is not installed or not on PATH." >&2
  exit 1
fi

if docker compose version &>/dev/null; then
  COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE="docker-compose"
else
  echo "ERROR: neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "No .env found. Creating one from .env.example — edit it before re-running:"
  cp .env.example .env
  echo "  -> $(pwd)/.env"
  exit 1
fi

ACTION="${1:-up}"
shift || true

case "$ACTION" in
  up)
    $COMPOSE up -d --build
    echo
    echo "Stack starting. Check status with: ./deploy.sh status"
    PORT=$(grep -E '^CONSOLE_PORT=' .env | cut -d= -f2)
    PORT="${PORT:-8088}"
    echo "Console will be available at: http://localhost:${PORT}"
    ;;
  down)
    $COMPOSE down "$@"
    ;;
  logs)
    $COMPOSE logs -f "$@"
    ;;
  status)
    $COMPOSE ps
    ;;
  *)
    echo "Unknown action: $ACTION" >&2
    echo "Usage: $0 {up|down|logs|status}" >&2
    exit 1
    ;;
esac
