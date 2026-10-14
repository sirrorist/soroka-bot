"""Handlers package."""

from aiogram import Dispatcher, F
from aiogram.enums import ChatType

from . import download, language, premium, start, stats, steam
from .download import register_download_handlers
from .group import register_group_handlers
from .language import register_language_handlers
from .premium import register_premium_handlers
from .start import register_start_handlers
from .stats import register_stats_handlers
from .steam import register_steam_handlers


def register_handlers(dp: Dispatcher) -> None:
    """Register all handlers.

    Порядок важен: сообщение забирает первый подходящий обработчик. Группы - первыми
    (их роутер отфильтрован по типу чата), скачивание по любому тексту - последним.
    """
    # вне лички команды без префикса не берутся: в группе у них свой роутер
    for module in (start, stats, premium, steam, language, download):
        module.router.message.filter(F.chat.type == ChatType.PRIVATE)
    register_group_handlers(dp)
    register_start_handlers(dp)
    register_stats_handlers(dp)
    register_premium_handlers(dp)
    register_steam_handlers(dp)
    register_language_handlers(dp)
    register_download_handlers(dp)
