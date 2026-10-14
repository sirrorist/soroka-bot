"""Configuration module for the bot."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    # Bot Configuration
    bot_token: str = Field(...)
    bot_username: str | None = Field(default=None)
    # Для машин, откуда Telegram напрямую недоступен. aiogram не читает HTTPS_PROXY
    # из окружения, поэтому адрес задаётся отдельно; нужен пакет aiohttp-socks
    telegram_proxy: str | None = Field(default=None)

    # Database
    database_url: str = Field(...)
    postgres_user: str = Field(default="postgres")
    postgres_password: str = Field(default="postgres")
    postgres_db: str = Field(default="content_downloader")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Application Settings
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    max_file_size_mb: int = Field(default=100)
    temp_dir: str = Field(default="./temp")
    storage_dir: str = Field(default="./storage")
    download_timeout_s: int = Field(default=300)
    max_parallel_downloads: int = Field(default=3)

    # Rate Limiting
    free_user_limit: int = Field(default=7)
    premium_user_limit: int = Field(default=1000)
    # Цены Premium в рублях: личный и для чата (слово владельца 2026-09-30)
    premium_price_month: int = Field(default=99)
    premium_price_year: int = Field(default=599)
    chat_premium_price_month: int = Field(default=99)
    chat_premium_price_year: int = Field(default=599)
    # Группа: лимит на чат. Premium чата - PREMIUM_USER_LIMIT скачиваний и свой объём
    group_free_limit: int = Field(default=10)
    group_free_daily_mb: int = Field(default=350)
    group_premium_daily_mb: int = Field(default=5120)

    # Команды бота в группе - только с этим префиксом, чтобы не отвечать на чужие
    # /start и /help. В Telegram команда - только a-z, 0-9 и "_", дефис не годится
    group_command_prefix: str = Field(default="sr_")
    # Кому бот пересылает заявки на Premium: id владельца в Telegram
    admin_id: int | None = Field(default=None)

    # Steam Web API для /sdate; не задан - команда отвечает, что не настроена
    steam_api_key: str | None = Field(default=None)

    # API Keys
    twitter_api_key: str | None = Field(default=None)
    twitter_api_secret: str | None = Field(default=None)
    instagram_username: str | None = Field(default=None)
    instagram_password: str | None = Field(default=None)

    # Server Configuration
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8001)
    webhook_url: str | None = Field(default=None)
    # Не задан - бот придумывает новый при каждом запуске и сам сообщает его Telegram
    webhook_secret: str | None = Field(default=None)

    # Monitoring
    sentry_dsn: str | None = Field(default=None)

    # имя переменной окружения - имя поля, регистр не важен
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
