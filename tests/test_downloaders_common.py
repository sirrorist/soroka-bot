"""downloaders/common.py: каталог запроса, ограничения yt-dlp, отказ идти на чужие адреса."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import settings
from downloaders import common
from downloaders.common import (
    DownloadLimitError,
    find_file,
    new_workdir,
    remove_workdir,
    short_error,
    ydl_limits,
)

MB = 1024 * 1024


# ---------- каталог запроса ----------

def test_new_workdir_is_unique_direct_child_of_temp_dir(workroot):
    a = new_workdir("tiktok")
    b = new_workdir("tiktok")

    assert a != b
    assert a.is_dir() and b.is_dir()
    assert a.parent == workroot and b.parent == workroot
    assert a.name.startswith("tiktok_")


def test_remove_workdir_removes_request_dir_given_dir_or_file_inside(workroot):
    by_dir = new_workdir("youtube")
    (by_dir / "v.mp4").write_bytes(b"x")
    by_file = new_workdir("youtube")
    (by_file / "v.mp4").write_bytes(b"x")

    remove_workdir(by_dir)
    remove_workdir(str(by_file / "v.mp4"))

    assert not by_dir.exists()
    assert not by_file.exists()
    assert workroot.is_dir()


def test_remove_workdir_is_idempotent(workroot):
    w = new_workdir("tiktok")
    remove_workdir(w)
    remove_workdir(w)  # второй вызов не падает
    assert not w.exists()


def test_remove_workdir_never_removes_temp_dir_itself(workroot):
    w = new_workdir("tiktok")
    (w / "v.mp4").write_bytes(b"x")
    stray = workroot / "stray.txt"
    stray.write_bytes(b"x")

    remove_workdir(workroot)  # сам temp_dir
    remove_workdir(stray)  # файл прямо в temp_dir: "каталог запроса" был бы temp_dir

    assert workroot.is_dir()
    assert stray.exists()
    assert (w / "v.mp4").exists()


def test_remove_workdir_ignores_paths_outside_temp_dir(workroot, tmp_path):
    workroot.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_bytes(b"x")

    remove_workdir(outside)
    remove_workdir(outside / "precious.txt")
    remove_workdir(tmp_path)

    assert (outside / "precious.txt").exists()


def test_remove_workdir_touches_only_direct_subdirs_of_temp_dir(workroot):
    w = new_workdir("tiktok")
    nested = w / "sub"
    nested.mkdir()
    (nested / "v.mp4").write_bytes(b"x")

    remove_workdir(nested)  # внук temp_dir, не каталог запроса
    remove_workdir(nested / "v.mp4")

    assert (nested / "v.mp4").exists()


def test_remove_workdir_does_not_follow_symlink_out_of_temp_dir(workroot, tmp_path):
    workroot.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_bytes(b"x")
    link = workroot / "tiktok_evil"
    link.symlink_to(outside, target_is_directory=True)

    remove_workdir(link)
    remove_workdir(link / "precious.txt")

    assert (outside / "precious.txt").exists()


def test_find_file_skips_partial_downloads_and_picks_finished_file(tmp_path):
    (tmp_path / "video.f137.mp4.part").write_bytes(b"x" * 5000)  # больше готового
    (tmp_path / "video.mp4.ytdl").write_bytes(b"x" * 6000)
    (tmp_path / "video.mp4").write_bytes(b"x" * 10)

    assert find_file(tmp_path) == tmp_path / "video.mp4"


def test_find_file_returns_none_when_only_partials_or_empty(tmp_path):
    assert find_file(tmp_path) is None
    (tmp_path / "a.mp4.part").write_bytes(b"x")
    (tmp_path / "a.mp4.ytdl").write_bytes(b"x")
    (tmp_path / "subdir").mkdir()
    assert find_file(tmp_path) is None


def test_find_file_takes_largest_of_several(tmp_path):
    (tmp_path / "thumb.jpg").write_bytes(b"x" * 10)
    (tmp_path / "video.mp4").write_bytes(b"x" * 100)
    assert find_file(tmp_path) == tmp_path / "video.mp4"


# ---------- ydl_limits ----------

def _guard(tmp_path, max_mb=None, platform="tiktok"):
    return ydl_limits(tmp_path, platform, max_mb=max_mb)["progress_hooks"][0]


def test_ydl_limits_static_options(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_file_size_mb", 7)
    opts = ydl_limits(tmp_path, "youtube")

    assert opts["noplaylist"] is True
    assert opts["playlist_items"] == "1"
    assert opts["max_filesize"] == 7 * MB
    assert opts["socket_timeout"] == 30
    assert opts["outtmpl"] == str(tmp_path / "youtube_%(id)s.%(ext)s")
    assert len(opts["progress_hooks"]) == 1


def test_ydl_limits_explicit_max_mb_overrides_settings(tmp_path):
    assert ydl_limits(tmp_path, "tiktok", max_mb=3)["max_filesize"] == 3 * MB


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "instagram", "twitter"])
def test_ydl_limits_allowed_extractors_never_include_generic(tmp_path, platform):
    extractors = ydl_limits(tmp_path, platform)["allowed_extractors"]
    assert extractors  # пустой список yt-dlp мог бы читать как "все"
    assert all("generic" not in e.lower() for e in extractors)
    assert "default" not in extractors and "all" not in extractors


def test_ydl_limits_allowed_extractors_are_per_platform(tmp_path):
    assert ydl_limits(tmp_path, "youtube")["allowed_extractors"] == ["youtube"]
    assert "tiktok" in ydl_limits(tmp_path, "tiktok")["allowed_extractors"]


def test_guard_raises_when_bytes_exceed_limit(tmp_path):
    guard = _guard(tmp_path, max_mb=1)

    with pytest.raises(DownloadLimitError, match="файл больше 1 МБ"):
        guard({"downloaded_bytes": MB + 1})


def test_guard_allows_exactly_limit_and_missing_or_none_bytes(tmp_path):
    guard = _guard(tmp_path, max_mb=1)

    guard({"downloaded_bytes": MB})
    guard({"downloaded_bytes": 0})
    guard({"downloaded_bytes": None})
    guard({})
    guard({"status": "finished"})


def test_guard_raises_after_deadline_but_not_before(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(common, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(settings, "download_timeout_s", 60)
    guard = _guard(tmp_path)

    now[0] = 1000.0 + 60  # ровно в срок - ещё можно
    guard({"downloaded_bytes": 1})
    now[0] = 1000.0 + 60.5
    with pytest.raises(DownloadLimitError, match="слишком много времени"):
        guard({"downloaded_bytes": 1})


def test_guard_deadline_counts_from_ydl_limits_call_not_from_hook_call(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(common, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(settings, "download_timeout_s", 10)
    now[0] = 500.0
    guard = _guard(tmp_path)  # срок = 510

    now[0] = 509.0
    guard({})
    now[0] = 511.0
    with pytest.raises(DownloadLimitError):
        guard({})


def test_match_filter_rejects_live_and_long_accepts_short_and_unknown_duration(tmp_path):
    match = ydl_limits(tmp_path, "youtube")["match_filter"]

    assert match({"title": "t", "is_live": True}) is not None
    assert match({"title": "t", "duration": 20 * 60 + 1}) is not None
    assert match({"title": "t", "duration": 20 * 60}) is None
    assert match({"title": "t", "duration": 30, "is_live": False}) is None
    assert match({"title": "t"}) is None


# ---------- short_error ----------

def test_short_error_returns_limit_message_from_cause_chain():
    try:
        try:
            raise DownloadLimitError("файл больше 1 МБ")
        except DownloadLimitError as inner:
            raise RuntimeError("yt-dlp wrapper") from inner
    except RuntimeError as e:
        assert short_error(e) == "файл больше 1 МБ"


def test_short_error_strips_prefix_and_truncates():
    assert short_error(Exception("ERROR: nope")) == "nope"
    assert short_error(Exception("x" * 500)) == "x" * 200


# ---------- интеграция: загрузчик не ходит на чужой адрес ----------

class _Recorder(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.hits.append(self.path)
        body = b"<html><body>hi</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET  # noqa: N815

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.hits = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module, func",
    [
        ("downloaders.tiktok", "download_tiktok_video"),
        ("downloaders.instagram", "download_instagram_content"),
        ("downloaders.twitter", "download_twitter_content"),
    ],
)
async def test_downloader_makes_no_request_to_local_server_and_leaves_no_files(
    local_server, workroot, module, func
):
    import importlib

    download = getattr(importlib.import_module(module), func)
    url = f"http://127.0.0.1:{local_server.server_port}/video.mp4"

    # Контроль: тот же адрес с generic-экстрактором сервер видит. Без этого "ноль
    # запросов" ничего бы не доказывал (сервер мог быть недоступен для yt-dlp).
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        try:
            ydl.extract_info(url, download=False)
        except Exception:
            pass
    assert local_server.hits, "контрольный запрос не дошёл до локального сервера"
    local_server.hits.clear()

    result = await download(url)

    assert result["success"] is False
    assert isinstance(result["error"], str) and result["error"]
    assert local_server.hits == []
    assert list(Path(workroot).iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module, func",
    [
        ("downloaders.tiktok", "download_tiktok_video"),
        ("downloaders.youtube", "download_youtube_video"),
        ("downloaders.instagram", "download_instagram_content"),
        ("downloaders.twitter", "download_twitter_content"),
    ],
)
async def test_downloader_runs_blocking_ytdlp_outside_event_loop_thread(
    monkeypatch, module, func
):
    import importlib

    mod = importlib.import_module(module)
    seen = {}

    def fake_sync(url):
        seen["thread"] = threading.get_ident()
        seen["url"] = url
        return {"success": False, "error": "stub"}

    monkeypatch.setattr(mod, "_download_sync", fake_sync)

    result = await getattr(mod, func)("https://youtu.be/abc")

    assert result == {"success": False, "error": "stub"}
    assert seen["url"] == "https://youtu.be/abc"
    assert seen["thread"] != threading.get_ident()
