#!/bin/bash

# Deployment script for remote server

echo "🚀 Starting deployment..."

# Pull latest changes
git pull origin main

# Build and restart containers
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Run database migrations
docker-compose exec bot alembic upgrade head

# Clean up old files
# у каждого запроса свой каталог в temp/ - удалять и файлы, и каталоги
docker-compose exec bot find temp/ -mindepth 1 -mtime +1 -delete

echo "✅ Deployment completed!"
