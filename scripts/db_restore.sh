#!/bin/bash
# Восстановить базу бота из дампа scripts/db_backup.sh. ЗАМЕНЯЕТ текущие данные.
#
#   ./scripts/db_restore.sh ~/soroka-backups/soroka-20260930-120000.dump
#
# Перед запуском остановить бота, чтобы он не писал в базу посреди восстановления:
#   docker compose stop bot
# Контейнер - DB_CONTAINER, по умолчанию tiktube_download_db.
set -euo pipefail

container=${DB_CONTAINER:-tiktube_download_db}
file=${1:?"Укажите файл дампа: $0 путь/к/файлу.dump"}

if [ ! -s "$file" ]; then
    echo "ОШИБКА: файла $file нет или он пустой" >&2
    exit 1
fi
case "$file" in
    *.part) echo "ОШИБКА: $file - непроверенный (оборванный) дамп" >&2; exit 1 ;;
esac
# --clean сначала удаляет таблицы: дамп проверяется до этого, а не после
if ! docker exec -i "$container" pg_restore --list < "$file" | grep -qE ' TABLE (public )?users '; then
    echo "ОШИБКА: $file не читается как дамп базы бота" >&2
    exit 1
fi

if docker ps --format '{{.Names}}' | grep -qx 'tiktube_download_bot'; then
    echo "ВНИМАНИЕ: бот запущен и может писать в базу. Сначала: docker compose stop bot" >&2
    exit 1
fi

echo "Будет восстановлено из: $file"
echo "Текущие данные в базе контейнера $container будут заменены."
read -r -p 'Чтобы продолжить, напишите RESTORE: ' answer
if [ "$answer" != "RESTORE" ]; then
    echo "Отменено."
    exit 1
fi

# --clean --if-exists: сначала удалить то, что есть, потом создать из дампа
docker exec -i "$container" sh -c \
    'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < "$file"

echo "Восстановлено. Сейчас в базе:"
docker exec -i "$container" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT 'users: ' || count(*) FROM users
UNION ALL SELECT 'downloads: ' || count(*) FROM downloads;
SQL
