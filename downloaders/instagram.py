"""Instagram downloader."""

import asyncio
from typing import Any

from downloaders.common import (
    find_file,
    new_workdir,
    remove_workdir,
    user_reason,
    ydl_limits,
)
from utils.i18n import t


def _download_sync(url: str) -> dict[str, Any]:
    import yt_dlp

    workdir = new_workdir("instagram")
    try:
        ydl_opts = {
            "format": "best",
            "quiet": True,
            "no_warnings": True,
            **ydl_limits(workdir, "instagram"),
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

        # Determine content type
        ext = file.suffix.lower()
        content_type = "video" if ext in [".mp4", ".mov", ".webm"] else "photo"

        return {
            "success": True,
            "file_path": str(file),
            "content_type": content_type,
            "file_size": file.stat().st_size,
            "title": info.get("title", "Instagram Content"),
        }
    except Exception as e:
        remove_workdir(workdir)
        return {
            "success": False,
            "error": user_reason(e, t("errors.platform_failed"), "instagram"),
        }


async def download_instagram_content(url: str) -> dict[str, Any]:
    """Download Instagram content (photo or video) в отдельном потоке."""
    return await asyncio.to_thread(_download_sync, url)
