#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
backup_root=${1:-"$project_root/backups"}
compose_file=${COMPOSE_FILE:-"$project_root/compose.prod.yaml"}
env_file=${ENV_FILE:-"$project_root/.env.prod"}
mkdir -p "$backup_root"
target="$backup_root/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "$target" "$target/media"

compose=(docker compose --env-file "$env_file" -f "$compose_file")
"${compose[@]}" exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$target/database.dump"
"${compose[@]}" cp web:/app/media/. "$target/media"
printf 'Backup created: %s\n' "$target"
