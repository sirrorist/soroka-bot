# Быстрый старт

## Локальная разработка

### 1. Установка зависимостей

```bash
# Создание виртуального окружения (рекомендуется)
python -m venv venv

# Активация виртуального окружения
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Установка зависимостей
pip install -r requirements.txt
```

### 2. Настройка окружения

```bash
# Создание .env файла
# Windows:
copy env_template.txt .env
# Linux/Mac:
cp env_template.txt .env

# Редактирование .env файла
nano env_template.txt
# Заполните BOT_TOKEN (получите у @BotFather в Telegram)
```

Минимальная конфигурация для начала:
```env
BOT_TOKEN=your_bot_token_here
BOT_USERNAME=your_bot_username
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/content_downloader
REDIS_URL=redis://localhost:6379/0
DEBUG=True
```

### 3. Настройка базы данных

#### Вариант 1: Docker (рекомендуется)
```bash
# Запуск только БД и Redis
docker-compose up -d db redis
```

#### Вариант 2: Локальная установка
```bash
# PostgreSQL
# Windows: скачайте с postgresql.org
# Linux: sudo apt install postgresql
# Mac: brew install postgresql

# Redis
# Windows: скачайте с redis.io или используйте WSL
# Linux: sudo apt install redis-server
# Mac: brew install redis
```

### 4. Инициализация проекта

```bash
# Создание необходимых директорий
python setup.py

# Таблицы базы данных - миграциями; бот без них не запустится
alembic upgrade head
```

### 5. Запуск бота

```bash
python main.py
```

Бот запустится в режиме polling (для разработки).

## Тестирование

### Проверка работы бота

1. Найдите вашего бота в Telegram по username
2. Отправьте команду `/start`
3. Отправьте ссылку на видео (например, YouTube или TikTok)
4. Дождитесь скачивания

### Примеры ссылок для тестирования

- YouTube: `https://www.youtube.com/watch?v=dQw4w9WgXcQ`
- TikTok: `https://www.tiktok.com/@username/video/1234567890`
- Instagram: `https://www.instagram.com/p/ABC123/`

## Docker (продакшн)

### Полный запуск через Docker

```bash
# Создайте .env файл с настройками
cp env_template.txt .env
# Отредактируйте .env

# Сборка, таблицы базы данных, запуск всех сервисов
docker-compose build
docker-compose run --rm bot alembic upgrade head
docker-compose up -d

# Просмотр логов
docker-compose logs -f bot

# Остановка
docker-compose down
```

## Решение проблем

### Бот не отвечает
- Проверьте токен в `.env`
- Убедитесь, что бот запущен (проверьте логи)
- Проверьте подключение к интернету

### Ошибки базы данных
- Убедитесь, что PostgreSQL запущен
- Проверьте строку подключения в `.env`
- Проверьте права доступа к БД

### Ошибки при скачивании
- Некоторые платформы могут блокировать запросы
- Проверьте интернет-соединение
- Убедитесь, что ссылка корректна

### Нехватка места
- Очистите папку `temp/` от старых файлов
- Настройте автоматическую очистку в cron/systemd

## Следующие шаги

1. ✅ Настройте webhook для продакшн (см. DEPLOYMENT.md)
2. ✅ Настройте мониторинг и логирование
3. ✅ Добавьте обработку ошибок
4. ✅ Настройте автоматические бэкапы БД
5. ✅ Оптимизируйте производительность

## Полезные команды

```bash
# Просмотр логов
tail -f logs/bot_*.log

# Очистка временных файлов
find temp/ -type f -mtime +1 -delete

# Бэкап базы данных
docker-compose exec db pg_dump -U postgres content_downloader > backup.sql

# Восстановление из бэкапа
docker-compose exec -T db psql -U postgres content_downloader < backup.sql
```
