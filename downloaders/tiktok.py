"""TikTok downloader."""
import asyncio
from typing import Dict, Any

from utils.i18n import t
from downloaders.common import find_file, new_workdir, remove_workdir, user_reason, ydl_limits


def _download_sync(url: str) -> Dict[str, Any]:
    import yt_dlp

    workdir = new_workdir("tiktok")
    try:
        ydl_opts = {
            'format': 'best',
            'quiet': True,
            'no_warnings': True,
            **ydl_limits(workdir, "tiktok"),
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        file = find_file(workdir)
        if not file:
            remove_workdir(workdir)
            return {
                "success": False,
                "error": t("errors.generic_limits"),
            }

        return {
            "success": True,
            "file_path": str(file),
            "content_type": "video",
            "file_size": file.stat().st_size,
            "title": info.get("title", "TikTok Video"),
        }
    except Exception as e:
        remove_workdir(workdir)
        return {
            "success": False,
            "error": user_reason(e, t("errors.platform_failed"), "tiktok")
        }


async def download_tiktok_video(url: str) -> Dict[str, Any]:
    """Download TikTok video. yt-dlp синхронный - в отдельном потоке, чтобы не держать бота."""
    return await asyncio.to_thread(_download_sync, url)
