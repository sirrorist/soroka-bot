"""handlers/stats.py: экранирование имени; main.py: секрет вебхука."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from config import settings
from handlers import stats


def _fake_scope(session):
    @asynccontextmanager
    async def session_scope():
        yield session

    return session_scope


def _stats_session(first_name):
    fresh_user = SimpleNamespace(
        first_name=first_name, is_premium=True, downloads_today=1
    )
    results = [
        MagicMock(scalar_one=MagicMock(return_value=fresh_user)),
        MagicMock(
            first=MagicMock(return_value=SimpleNamespace(total=2, total_size=1024))
        ),
        MagicMock(all=MagicMock(return_value=[("tiktok", 2)])),
    ]
    session = MagicMock()
    session.execute = AsyncMock(side_effect=results)
    return session


@pytest.mark.asyncio
async def test_stats_escapes_first_name(monkeypatch):
    monkeypatch.setattr(
        stats, "session_scope", _fake_scope(_stats_session('<b>x</b> & <a href="t">'))
    )
    message = SimpleNamespace(
        answer=AsyncMock()
    )  # без атрибута message: ветка команды, не callback

    await stats.show_stats(message, user=SimpleNamespace(id=1))

    text = message.answer.await_args.args[0]
    assert (
        "&lt;b&gt;x&lt;/b&gt; &amp; &lt;a href=&quot;t&quot;&gt; · ⭐ Premium" in text
    )
    assert "<b>x</b>" not in text
    assert "<a href" not in text
    # своя разметка не пострадала
    assert "<b>📊 Ваша статистика</b>" in text


@pytest.mark.asyncio
async def test_stats_without_first_name_shows_placeholder(monkeypatch):
    monkeypatch.setattr(stats, "session_scope", _fake_scope(_stats_session(None)))
    message = SimpleNamespace(
        answer=AsyncMock()
    )  # без атрибута message: ветка команды, не callback

    await stats.show_stats(message, user=SimpleNamespace(id=1))

    assert "Без имени · ⭐ Premium" in message.answer.await_args.args[0]


def test_webhook_secret_is_never_empty():
    assert main.WEBHOOK_SECRET
    if settings.webhook_secret is None:
        assert len(main.WEBHOOK_SECRET) >= 32
    else:
        assert main.WEBHOOK_SECRET == settings.webhook_secret


@pytest.mark.asyncio
async def test_set_webhook_gets_the_secret(monkeypatch):
    monkeypatch.setattr(settings, "webhook_url", "https://example.invalid")
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "s3cret-value")
    bot = MagicMock()
    bot.set_webhook = AsyncMock()

    await main.on_startup(bot)

    bot.set_webhook.assert_awaited_once()
    kwargs = bot.set_webhook.await_args.kwargs
    assert kwargs["secret_token"] == "s3cret-value"
    assert kwargs["url"] == "https://example.invalid/webhook"


@pytest.mark.asyncio
async def test_request_handler_gets_the_same_secret(monkeypatch):
    """main() передаёт тот же секрет в SimpleRequestHandler, без него апдейты не проверяются."""
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "s3cret-value")
    monkeypatch.setattr(settings, "webhook_url", "https://example.invalid")
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    # схема - предмет tests/test_migrations.py; здесь базы нет вовсе
    monkeypatch.setattr(main, "check_schema", AsyncMock())
    monkeypatch.setattr(main, "Bot", MagicMock())
    monkeypatch.setattr(main, "setup_application", MagicMock())
    handler_cls = MagicMock()
    monkeypatch.setattr(main, "SimpleRequestHandler", handler_cls)
    web = MagicMock()
    web.AppRunner.return_value = MagicMock(setup=AsyncMock(), cleanup=AsyncMock())
    web.TCPSite.return_value = MagicMock(start=AsyncMock())
    monkeypatch.setattr(main, "web", web)

    async def stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "asyncio", SimpleNamespace(sleep=stop))

    with pytest.raises(asyncio.CancelledError):
        await main.main()

    handler_cls.assert_called_once()
    assert handler_cls.call_args.kwargs["secret_token"] == "s3cret-value"
