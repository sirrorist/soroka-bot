"""Downloaders package."""
from .common import extract_url
from .tiktok import download_tiktok_video
from .youtube import download_youtube_video
from .instagram import download_instagram_content
from .twitter import download_twitter_content


def detect_platform(url: str) -> str | None:
    """Detect platform from URL."""
    found = extract_url(url)
    return found[0] if found else None


async def download_tiktok(url: str) -> dict[str, any]:
    """Download TikTok content."""
    return await download_tiktok_video(url)


async def download_youtube(url: str) -> dict[str, any]:
    """Download YouTube content."""
    return await download_youtube_video(url)


async def download_instagram(url: str) -> dict[str, any]:
    """Download Instagram content."""
    return await download_instagram_content(url)


async def download_twitter(url: str) -> dict[str, any]:
    """Download Twitter/X content."""
    return await download_twitter_content(url)
