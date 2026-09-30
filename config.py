"""Configuration module for the bot."""
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    """Application settings."""
    
    # Bot Configuration
    bot_token: str = Field(..., env="BOT_TOKEN")
    bot_username: Optional[str] = Field(default=None, env="BOT_USERNAME")
    # Для машин, откуда Telegram напрямую недоступен. aiogram не читает HTTPS_PROXY
    # из окружения, поэтому адрес задаётся отдельно; нужен пакет aiohttp-socks
    telegram_proxy: Optional[str] = Field(default=None, env="TELEGRAM_PROXY")
    
    # Database
    database_url: str = Field(..., env="DATABASE_URL")
    postgres_user: str = Field(default="postgres", env="POSTGRES_USER")
    postgres_password: str = Field(default="postgres", env="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="content_downloader", env="POSTGRES_DB")
    
    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0", env="REDIS_URL")
    
    # Application Settings
    debug: bool = Field(default=False, env="DEBUG")
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    max_file_size_mb: int = Field(default=100, env="MAX_FILE_SIZE_MB")
    temp_dir: str = Field(default="./temp", env="TEMP_DIR")
    storage_dir: str = Field(default="./storage", env="STORAGE_DIR")
    download_timeout_s: int = Field(default=300, env="DOWNLOAD_TIMEOUT_S")
    max_parallel_downloads: int = Field(default=3, env="MAX_PARALLEL_DOWNLOADS")
    
    # Rate Limiting
    free_user_limit: int = Field(default=7, env="FREE_USER_LIMIT")
    premium_user_limit: int = Field(default=1000, env="PREMIUM_USER_LIMIT")
    # Цены Premium в рублях: личный и для чата (слово владельца 2026-09-30)
    premium_price_month: int = Field(default=99, env="PREMIUM_PRICE_MONTH")
    premium_price_year: int = Field(default=599, env="PREMIUM_PRICE_YEAR")
    chat_premium_price_month: int = Field(default=99, env="CHAT_PREMIUM_PRICE_MONTH")
    chat_premium_price_year: int = Field(default=599, env="CHAT_PREMIUM_PRICE_YEAR")
    # Группа: лимит на чат. Premium чата - PREMIUM_USER_LIMIT скачиваний и свой объём
    group_free_limit: int = Field(default=10, env="GROUP_FREE_LIMIT")
    group_free_daily_mb: int = Field(default=350, env="GROUP_FREE_DAILY_MB")
    group_premium_daily_mb: int = Field(default=5120, env="GROUP_PREMIUM_DAILY_MB")

    # Команды бота в группе - только с этим префиксом, чтобы не отвечать на чужие
    # /start и /help. В Telegram команда - только a-z, 0-9 и "_", дефис не годится
    group_command_prefix: str = Field(default="sr_", env="GROUP_COMMAND_PREFIX")
    # Кому бот пересылает заявки на Premium: id владельца в Telegram
    admin_id: Optional[int] = Field(default=None, env="ADMIN_ID")

    # Steam Web API для /sdate; не задан - команда отвечает, что не настроена
    steam_api_key: Optional[str] = Field(default=None, env="STEAM_API_KEY")
    
    # API Keys
    twitter_api_key: Optional[str] = Field(default=None, env="TWITTER_API_KEY")
    twitter_api_secret: Optional[str] = Field(default=None, env="TWITTER_API_SECRET")
    instagram_username: Optional[str] = Field(default=None, env="INSTAGRAM_USERNAME")
    instagram_password: Optional[str] = Field(default=None, env="INSTAGRAM_PASSWORD")
    
    # Server Configuration
    host: str = Field(default="0.0.0.0", env="HOST")
    port: int = Field(default=8001, env="PORT")
    webhook_url: Optional[str] = Field(default=None, env="WEBHOOK_URL")
    # Не задан - бот придумывает новый при каждом запуске и сам сообщает его Telegram
    webhook_secret: Optional[str] = Field(default=None, env="WEBHOOK_SECRET")
    
    # Monitoring
    sentry_dsn: Optional[str] = Field(default=None, env="SENTRY_DSN")
    
    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
