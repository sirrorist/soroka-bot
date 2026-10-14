# Инструкция по развертыванию

## Подготовка сервера

### Требования
- Ubuntu 20.04+ / Debian 11+
- Docker и Docker Compose
- Минимум 2GB RAM
- 10GB свободного места

## Настройка проекта

### 1. Клонирование репозитория

```bash
git clone <repo-url>
cd tiktube-download-bot
```

### 2. Создание .env файла

```bash
cp env_template.txt .env
nano .env
```

Заполните следующие переменные:
- `BOT_TOKEN` - получите у @BotFather в Telegram
- `BOT_USERNAME` - имя вашего бота
- `DATABASE_URL` - строка подключения к БД
- `REDIS_URL` - строка подключения к Redis
- `WEBHOOK_URL` - URL вашего сервера (например: https://domain.name)

### 3. Настройка Nginx для webhook

Создайте файл `/etc/nginx/sites-available/telegram-bot`:

```nginx
server {
    server_name domain.name;

    location /webhook {
        proxy_pass http://localhost:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Активируйте конфигурацию:
```bash
sudo ln -s /etc/nginx/sites-available/telegram-bot /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 4. Настройка сертификата

```bash
# Установка Nginx
sudo apt install nginx certbot python3-certbot-nginx -y

# Настройка SSL сертификата
sudo certbot --nginx -d domain.name
```

## Запуск приложения

### Разработка (Polling)

```bash
# Установка зависимостей
pip install -r requirements.txt

# Таблицы базы данных - миграциями; бот без них не запустится
alembic upgrade head

# Запуск
python main.py
```

### Продакшн (Docker)

```bash
# Сборка, таблицы базы данных, запуск
docker-compose build
docker-compose run --rm bot alembic upgrade head
docker-compose up -d

# Просмотр логов
docker-compose logs -f bot

# Остановка
docker-compose down
```

## Обновление

```bash
# Обновление кода
git pull origin main

# Пересборка, миграции, перезапуск (старый бот работает до последней строки)
docker-compose build --no-cache
docker-compose run --rm bot alembic upgrade head
docker-compose up -d

# Очистка старых файлов
docker-compose exec bot find temp/ -type f -mtime +1 -delete
```

## Мониторинг

### Просмотр логов
```bash
docker-compose logs -f bot
tail -f logs/bot_*.log
```

### Проверка статуса
```bash
docker-compose ps
docker stats
```

### Бэкап базы данных
```bash
docker-compose exec db pg_dump -U postgres content_downloader > backup.sql
```

## Безопасность

1. **Измените пароли** в `.env` файле
2. **Настройте firewall**:
   ```bash
   sudo ufw allow 22/tcp
   sudo ufw allow 80/tcp
   sudo ufw allow 443/tcp
   sudo ufw enable
   ```
3. **Регулярно обновляйте** систему и зависимости
4. **Используйте SSL** для webhook

## Troubleshooting

### Бот не отвечает
- Проверьте токен в `.env`
- Проверьте логи: `docker-compose logs bot`
- Убедитесь, что webhook установлен: проверьте в Telegram API

### Ошибки базы данных
- Проверьте подключение: `docker-compose exec db psql -U postgres -d content_downloader`
- Проверьте переменные окружения

### Нехватка места
- Очистите старые файлы: `docker-compose exec bot find temp/ -type f -mtime +1 -delete`
- Увеличьте место на диске или настройте автоматическую очистку
