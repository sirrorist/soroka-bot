# Начало работы

## Шаг 1: Создание Telegram бота

### 1.1 Получение токена

1. Откройте Telegram и найдите [@BotFather](https://t.me/BotFather)
2. Отправьте команду `/newbot`
3. Следуйте инструкциям:
   - Введите имя бота (например: "Content Downloader Bot")
   - Введите username бота (должен заканчиваться на `bot`, например: `content_downloader_bot`)
4. BotFather предоставит вам **токен** вида: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`
5. Сохраните этот токен - он понадобится для настройки

### 1.2 Настройка бота (опционально)

Вы можете настроить бота через BotFather:

```
/setdescription - описание бота
/setabouttext - информация о боте
/setuserpic - аватар бота
/setcommands - команды бота
```

Рекомендуемые команды:
```
start - Начать работу с ботом
download - Скачать контент
stats - Статистика
premium - Premium подписка
help - Помощь
about - О боте
```

## Шаг 2: Настройка проекта

### 2.1 Клонирование/скачивание проекта

```bash
git clone <repo-url>
cd tiktube-download-bot
```

### 2.2 Создание виртуального окружения

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/Mac
python3 -m venv venv
source venv/bin/activate
```

### 2.3 Установка зависимостей

```bash
pip install -r requirements.txt
```

### 2.4 Настройка переменных окружения

1. Скопируйте шаблон:
   ```bash
   # Windows
   copy env_template.txt .env
   
   # Linux/Mac
   cp env_template.txt .env
   ```

2. Откройте файл `.env` и заполните:
   ```env
   BOT_TOKEN=ваш_токен_от_BotFather
   BOT_USERNAME=ваш_username_бота
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/content_downloader
   REDIS_URL=redis://localhost:6379/0
   DEBUG=True
   ```

## Шаг 3: Настройка базы данных

### Вариант A: Docker (рекомендуется)

```bash
# Запуск только PostgreSQL и Redis
docker-compose up -d db redis

# Проверка статуса
docker-compose ps
```

### Вариант B: Локальная установка

#### PostgreSQL

**Windows:**
1. Скачайте с [postgresql.org](https://www.postgresql.org/download/windows/)
2. Установите с настройками по умолчанию
3. Запомните пароль пользователя postgres

**Linux:**
```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
sudo -u postgres psql
```

**Mac:**
```bash
brew install postgresql
brew services start postgresql
```

Создание базы данных:
```sql
CREATE DATABASE content_downloader;
```

#### Redis

**Windows:**
- Используйте WSL или скачайте с [redis.io](https://redis.io/download)
- Или используйте Docker

**Linux:**
```bash
sudo apt install redis-server
sudo systemctl start redis
```

**Mac:**
```bash
brew install redis
brew services start redis
```

## Шаг 4: Инициализация проекта

```bash
# Создание необходимых директорий
python setup.py
```

Это создаст папки:
- `temp/` - для временных файлов
- `storage/` - для постоянного хранения
- `logs/` - для логов

## Шаг 5: Запуск бота

```bash
# таблицы базы данных - миграциями; бот без них не запустится
alembic upgrade head
python main.py
```

Вы должны увидеть:
```
HH:mm:ss | INFO     | Bot is starting up...
HH:mm:ss | INFO     | Database initialized successfully
HH:mm:ss | INFO     | Running in polling mode
HH:mm:ss | INFO     | Starting bot in polling mode...
```

## Шаг 6: Тестирование

1. Откройте Telegram
2. Найдите вашего бота по username
3. Отправьте `/start`
4. Отправьте ссылку на видео (например, YouTube)

### Примеры ссылок для тестирования:

- **YouTube**: `https://www.youtube.com/watch?v=dQw4w9WgXcQ`
- **TikTok**: `https://www.tiktok.com/@username/video/1234567890`
- **Instagram**: `https://www.instagram.com/p/ABC123/`

## Решение проблем

### Ошибка: "BOT_TOKEN is required"
- Убедитесь, что файл `.env` существует
- Проверьте, что `BOT_TOKEN` заполнен в `.env`
- Убедитесь, что токен правильный (без пробелов, кавычек)

### Ошибка подключения к базе данных
- Проверьте, что PostgreSQL запущен
- Проверьте строку подключения в `.env`
- Убедитесь, что база данных `content_downloader` создана

### Бот не отвечает
- Проверьте логи в консоли
- Убедитесь, что бот запущен
- Проверьте интернет-соединение
- Убедитесь, что токен правильный

### Ошибки при скачивании
- Некоторые платформы могут блокировать запросы
- Проверьте интернет-соединение
- Убедитесь, что ссылка корректна
- Проверьте логи для деталей ошибки

## Следующие шаги

После успешного запуска:

1. ✅ Прочитайте [QUICKSTART.md](./QUICKSTART.md) для дополнительной информации
2. ✅ Изучите [DEPLOYMENT.md](./DEPLOYMENT.md) для развертывания на сервере
3. ✅ Ознакомьтесь с [MONETIZATION.md](./MONETIZATION.md) для стратегии монетизации
4. ✅ Настройте webhook для продакшн (см. DEPLOYMENT.md)

## Полезные ссылки

- [Документация aiogram](https://docs.aiogram.dev/)
- [Документация yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [FastAPI документация](https://fastapi.tiangolo.com/)

---

**Готово! Ваш бот должен работать! 🎉**
