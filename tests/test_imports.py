"""Пакет загрузчиков импортируется целиком и узнаёт площадку по ссылке."""

import pytest

import downloaders
from downloaders import detect_platform


@pytest.mark.parametrize(
    "name",
    ["download_tiktok", "download_youtube", "download_instagram", "download_twitter"],
)
def test_downloader_exported(name):
    assert callable(getattr(downloaders, name))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://tiktok.com/@user/video/123", "tiktok"),
        ("https://instagram.com/p/ABC123/", "instagram"),
    ],
)
def test_detect_platform(url, expected):
    assert detect_platform(url) == expected
