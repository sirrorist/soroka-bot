"""PO Token generator for YouTube downloads."""

import json
import subprocess
import tempfile
from pathlib import Path

from loguru import logger


class POTokenGenerator:
    """Автоматическая генерация PO Token для YouTube."""

    @staticmethod
    def generate_from_ytdlp(client: str = "android") -> str | None:
        """
        Генерация PO Token через yt-dlp info extraction.

        Args:
            client: Тип клиента (android, ios)
            test_video: YouTube video ID для извлечения токена

        Returns:
            PO Token или None при ошибке
        """
        import random

        test_videos = [
            "dQw4w9WgXcQ",  # Rick Roll
            "9bZkp7q19f0",  # PSY Gangnam Style (backup)
            "kJQP7kiw5Fk",  # Eminem Love the Way You Lie (backup)
        ]

        test_video = random.choice(test_videos)

        logger.info(f"Generating PO Token for {client} client...")

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                # Запускаем yt-dlp для извлечения метаданных
                cmd = [
                    "yt-dlp",
                    f"https://www.youtube.com/watch?v={test_video}",
                    "--write-info-json",
                    "--skip-download",
                    "--no-warnings",
                    "--quiet",
                    "-o",
                    str(Path(tmpdir) / "video"),
                    "--extractor-args",
                    f"youtube:player_client={client}",
                ]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

                if result.returncode != 0:
                    logger.warning(f"yt-dlp extraction failed: {result.stderr[:200]}")
                    return None

                # Ищем info.json файл
                info_files = list(Path(tmpdir).glob("*.info.json"))
                if not info_files:
                    logger.warning("No info.json file generated")
                    return None

                # Парсим JSON и извлекаем PO Token
                with open(info_files[0], encoding="utf-8") as f:
                    info = json.load(f)

                # Ищем токен в разных местах
                po_token = None

                # Вариант 1: Прямо в info
                if "po_token" in info:
                    if isinstance(info["po_token"], dict):
                        po_token = info["po_token"].get(client)
                    else:
                        po_token = info["po_token"]

                # Вариант 2: В player_response
                if not po_token and "player_response" in info:
                    player_resp = info["player_response"]
                    if "poToken" in player_resp:
                        po_token = player_resp["poToken"].get(client)

                # Вариант 3: В format metadata
                if not po_token and "formats" in info:
                    for fmt in info["formats"]:
                        if "po_token" in fmt:
                            po_token = fmt["po_token"]
                            break

                if po_token:
                    logger.success(
                        f"✅ PO Token generated for {client}: {po_token[:30]}..."
                    )
                    return po_token
                else:
                    logger.warning(f"❌ PO Token not found in metadata for {client}")
                    return None

            except subprocess.TimeoutExpired:
                logger.error("PO Token generation timeout (30s)")
                return None
            except Exception as e:
                logger.error(f"PO Token generation error: {e}")
                return None

    @staticmethod
    def generate_fallback() -> str:
        """
        Fallback: возвращает пустой токен.

        YouTube всё равно вернёт какие-то форматы, просто не самые лучшие.
        """
        logger.warning(
            "Using fallback (no PO Token) - formats may be limited to 360p-480p"
        )
        return ""
