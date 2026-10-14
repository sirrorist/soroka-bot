#!/bin/bash

# Deployment script for remote server
# Порядок: собрать образ, затем миграции новым образом, и только потом новый бот -
# бот на старой схеме не стартует (database.check_schema). Ошибка любого шага -
# стоп: старый бот при этом продолжает работать.
set -euo pipefail

echo "🚀 Starting deployment..."

# Pull latest changes
git pull origin main

docker compose build --no-cache

# Run database migrations (база создана до миграций - сначала один раз
# `alembic stamp 0001`, commands.md в Docs). traefik.enable=false: одноразовый
# контейнер несёт метки сервиса, и traefik не должен слать ему webhook
docker compose run --rm -l traefik.enable=false bot alembic upgrade head

docker compose up -d

# Clean up old files
# у каждого запроса свой каталог в temp/ - удалять и файлы, и каталоги
docker compose exec bot find temp/ -mindepth 1 -mtime +1 -delete

echo "✅ Deployment completed!"
