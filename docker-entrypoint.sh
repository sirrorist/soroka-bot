#!/bin/sh
# Бот работает не от root. temp/, storage/, logs/ приходят с хоста (bind mount) и
# после прежних версий образа принадлежат root - отдаём их пользователю app,
# затем сбрасываем права root и запускаем команду уже от app.
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R app:app /app/temp /app/storage /app/logs
    exec setpriv --reuid=app --regid=app --init-groups -- "$@"
fi

exec "$@"
