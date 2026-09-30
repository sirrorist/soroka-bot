#!/bin/bash
# Резервная копия базы бота: pg_dump из контейнера Postgres в файл на хосте.
#
#   ./scripts/db_backup.sh              # в ~/soroka-backups
#   ./scripts/db_backup.sh /куда/класть
#
# Дамп - в формате pg_dump -Fc (сжатый, восстанавливается scripts/db_restore.sh).
# В дампе пользователи бота: каталог и файл - только для владельца, в git не класть
# (*.dump в .gitignore). Контейнер - DB_CONTAINER, по умолчанию tiktube_download_db.
set -euo pipefail

container=${DB_CONTAINER:-tiktube_download_db}
dir=${1:-$HOME/soroka-backups}

umask 077
mkdir -p "$dir"
file="$dir/soroka-$(date -u +%Y%m%d-%H%M%S).dump"
# пока дамп не проверен, он .part: оборванный pg_dump не должен выглядеть копией
part="$file.part"

# $POSTGRES_USER и $POSTGRES_DB - из окружения контейнера, пароль не нужен: локальный сокет
docker exec "$container" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$part"

# дамп, который не читается, хуже, чем никакого: проверить сразу
tables=$(docker exec -i "$container" pg_restore --list < "$part" | grep -cE ' TABLE (public )?(users|downloads|chats) ' || true)
if [ "$tables" -lt 2 ]; then
    echo "ОШИБКА: в дампе $part не нашлось таблиц users и downloads - это не копия" >&2
    exit 1
fi
mv "$part" "$file"

echo "Готово: $file ($(du -h "$file" | cut -f1), таблиц бота: $tables)"
docker exec -i "$container" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT 'users: ' || count(*) FROM users
UNION ALL SELECT 'downloads: ' || count(*) FROM downloads;
SQL
