"""PO Token cache manager for YouTube downloads."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger


class POTokenCache:
    """Управление кэшем PO Token с автоматическим обновлением."""

    def __init__(self, cache_file: str = "po_token_cache.json"):
        """
        Инициализация кэша токенов.

        Args:
            cache_file: Путь к файлу кэша
        """
        self.cache_file = Path(cache_file)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        """Загрузить кэш из файла."""
        if not self.cache_file.exists():
            return {}

        try:
            with open(self.cache_file, encoding="utf-8") as f:
                cache = json.load(f)
                logger.debug(f"PO Token cache loaded from {self.cache_file}")
                return cache
        except Exception as e:
            logger.warning(f"Failed to load PO Token cache: {e}")
            return {}

    def _save_cache(self):
        """Сохранить кэш в файл."""
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
                logger.debug(f"PO Token cache saved to {self.cache_file}")
        except Exception as e:
            logger.error(f"Failed to save PO Token cache: {e}")

    def get_token(self, client: str = "android") -> str | None:
        """
        Получить токен для клиента.

        Args:
            client: Тип клиента (android, ios)

        Returns:
            PO Token или None если истёк/отсутствует
        """
        if client not in self._cache:
            logger.debug(f"No cached PO Token for {client}")
            return None

        token_data = self._cache[client]
        expires_at = datetime.fromisoformat(token_data["expires_at"])

        if datetime.now() >= expires_at:
            logger.info(f"PO Token for {client} expired at {expires_at}")
            return None

        logger.debug(
            f"Using cached PO Token for {client} (expires: {expires_at.strftime('%Y-%m-%d %H:%M')})"
        )
        return token_data["token"]

    def set_token(self, client: str, token: str, ttl_days: int = 3) -> None:
        """
        Сохранить токен в кэш.

        Args:
            client: Тип клиента (android, ios)
            token: PO Token
            ttl_days: Срок жизни токена в днях (по умолчанию 3)
        """
        expires_at = datetime.now() + timedelta(days=ttl_days)

        self._cache[client] = {
            "token": token,
            "created_at": datetime.now().isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        self._save_cache()
        logger.info(
            f"PO Token for {client} cached until {expires_at.strftime('%Y-%m-%d %H:%M')}"
        )

    def clear_token(self, client: str):
        """Удалить токен из кэша."""
        if client in self._cache:
            del self._cache[client]
            self._save_cache()
            logger.info(f"PO Token for {client} cleared from cache")

    def clear_all(self):
        """Очистить весь кэш."""
        self._cache = {}
        self._save_cache()
        logger.info("PO Token cache cleared")
