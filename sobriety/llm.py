"""Private AI conversation through an OpenAI-compatible router.

The main thread owns SQLite and Telegram polling. Generation runs in worker
threads: they stream the answer into a Telegram draft, send the final message
and hand the finished turn back through a queue; the main thread stores it.
Nothing here logs conversation text, the key or the router address.
"""

import http.client
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .api import ApiError, NetworkError, NoRedirect, Telegram
from .diagnostics import operation

log = logging.getLogger("sobriety")

DEFAULT_SYSTEM = (
    "Ты - Собр, тёплый и честный собеседник в личном чате счётчика трезвых дней. "
    "Поддерживай человека, который держит трезвость: слушай, отвечай по существу, "
    "без морализаторства и без длинных лекций. Пиши по-русски, обычным текстом "
    "без Markdown, коротко - обычно 1-5 предложений, если не просят подробнее. "
    "Ты не врач: при угрозе жизни, тяжёлой абстиненции или мыслях о самоповреждении "
    "мягко, но прямо советуй срочно обратиться к врачу или в экстренную службу 112."
)
MAX_INPUT = 2000  # characters of one user message sent to the model
MAX_STORED = 4000  # characters of one stored assistant answer
TG_LIMIT = 4096
DRAFT_INTERVAL = 0.4  # seconds between draft updates


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    system: str = DEFAULT_SYSTEM
    max_tokens: int = 2000
    history_chars: int = 6000
    workers: int = 2
    timeout: float = 150.0

    @classmethod
    def from_env(cls):
        """None when the feature is off; ValueError on a partial or bad setup."""
        names = ("SOBRIETY_LLM_BASE_URL", "SOBRIETY_LLM_API_KEY", "SOBRIETY_LLM_MODEL")
        values = [os.environ.get(name, "").strip() for name in names]
        if not any(values):
            return None
        base_url, api_key, model = values
        if not all(values):
            raise ValueError
        if not re.fullmatch(
            r"https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?", base_url
        ):
            raise ValueError
        if re.search(r"\s", api_key) or not re.fullmatch(r"[\w./:@+-]{1,200}", model):
            raise ValueError

        def number(name, default, low, high):
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            value = int(raw)
            if not low <= value <= high:
                raise ValueError
            return value

        system = os.environ.get("SOBRIETY_LLM_SYSTEM", "").strip() or DEFAULT_SYSTEM
        return cls(
            base_url.rstrip("/"),
            api_key,
            model,
            system,
            number("SOBRIETY_LLM_MAX_TOKENS", 2000, 256, 8000),
            number("SOBRIETY_LLM_HISTORY_CHARS", 6000, 0, 60000),
        )


def window(history, budget):
    """Newest turns that fit the character budget, starting with a user turn."""
    picked, used = [], 0
    for item in reversed(history):
        used += len(item["content"])
        if used > budget:
            break
        picked.append(item)
    picked.reverse()
    while picked and picked[0]["role"] != "user":
        picked.pop(0)
    return [{"role": i["role"], "content": i["content"]} for i in picked]


def split(text, limit=TG_LIMIT):
    """Telegram-sized parts, cut at a paragraph, line or space when possible."""
    parts = []
    while len(text) > limit:
        cut = max(text.rfind(sep, 0, limit) for sep in ("\n\n", "\n", " "))
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


class RouterError(Exception):
    pass


def stream(config, messages, stop, opener=None):
    """Yield content pieces from the router's SSE stream; reasoning is ignored."""
    body = {
        "model": config.model,
        "messages": messages,
        "max_tokens": config.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        # The combo's models reason before answering and the reasoning counts
        # against max_tokens; with a small budget the visible answer is empty.
        # Measured 2026-11-12: "low" halves the reasoning, enable_thinking=false
        # is ignored by part of the combo.
        "reasoning_effort": "low",
    }
    request = urllib.request.Request(
        config.base_url + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + config.api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    # A redirect would carry the Authorization header to another host.
    opener = opener or urllib.request.build_opener(NoRedirect())
    deadline = time.monotonic() + config.timeout
    for attempt in (1, 2):
        try:
            response = opener.open(request, timeout=60)
            break
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            # A cold router answers 503 while its model catalog is being built.
            if code in (502, 503, 504) and attempt == 1 and not stop.is_set():
                time.sleep(2)
                continue
            raise RouterError(f"HTTP {code}") from None
        except (OSError, http.client.HTTPException):
            raise RouterError("connection") from None
    with response:
        try:
            for raw in response:
                if stop.is_set():
                    return
                if time.monotonic() > deadline:
                    raise RouterError("timeout")
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    raise RouterError("stream error")
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    log.info(
                        "LLM: модель %s, токены вход=%s выход=%s",
                        chunk.get("model"),
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                    )
                for choice in chunk.get("choices") or []:
                    delta = (choice or {}).get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        yield piece
        except (OSError, http.client.HTTPException):
            raise RouterError("connection lost") from None


@dataclass
class Job:
    user_id: int
    chat_id: int
    text: str
    messages: list
    draft_id: int
    stop: threading.Event


class ChatService:
    def __init__(self, config, token, api=None, streamer=stream, clock=time.monotonic):
        self.config = config
        self.api = api or Telegram(token)
        self.streamer = streamer
        self.clock = clock
        self.jobs = queue.Queue(maxsize=config.workers * 2)
        self.results = queue.Queue()
        self.lock = threading.Lock()
        self.active = {}  # user_id -> Job
        self.threads = []

    def start(self):
        for number in range(self.config.workers):
            thread = threading.Thread(
                target=self._work, name=f"llm-{number}", daemon=True
            )
            thread.start()
            self.threads.append(thread)

    def shutdown(self):
        with self.lock:
            for job in self.active.values():
                job.stop.set()
        for _ in self.threads:
            try:
                self.jobs.put_nowait(None)
            except queue.Full:
                pass

    def busy(self, user_id):
        with self.lock:
            return user_id in self.active

    def submit(self, user_id, chat_id, text, history):
        """Queue a reply. Returns 'busy', 'full' or 'ok'."""
        messages = [{"role": "system", "content": self.config.system}]
        messages += window(history, self.config.history_chars)
        messages.append({"role": "user", "content": text[:MAX_INPUT]})
        job = Job(
            user_id,
            chat_id,
            text[:MAX_INPUT],
            messages,
            secrets.randbelow(2**31 - 1) + 1,
            threading.Event(),
        )
        with self.lock:
            if user_id in self.active:
                return "busy"
            try:
                self.jobs.put_nowait(job)
            except queue.Full:
                return "full"
            self.active[user_id] = job
        return "ok"

    def stop(self, chat_id, draft_id):
        with self.lock:
            for job in self.active.values():
                if job.chat_id == chat_id and job.draft_id == draft_id:
                    job.stop.set()
                    return True
        return False

    def drain(self):
        """Finished turns, for the main thread to store."""
        done = []
        while True:
            try:
                done.append(self.results.get_nowait())
            except queue.Empty:
                return done

    def _work(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                with operation("llm"):
                    answer = self._run(job)
                if answer:
                    self.results.put((job.user_id, job.text, answer[:MAX_STORED]))
            except Exception:
                log.exception("LLM: неожиданная ошибка генерации")
            finally:
                with self.lock:
                    self.active.pop(job.user_id, None)

    def _draft(self, job, text):
        try:
            self.api.call(
                "sendMessageDraft",
                {
                    "chat_id": job.chat_id,
                    "draft_id": job.draft_id,
                    "text": text,
                    "can_stop": True,
                },
            )
            return 0
        except ApiError as exc:
            if exc.code in (401, 404):
                raise
            # Drafts are cosmetic: back off on 429 and keep generating.
            return exc.retry_after or 2
        except NetworkError:
            return 2

    def _send(self, chat_id, text):
        for part in split(text):
            for attempt in (1, 2, 3):
                try:
                    self.api.call("sendMessage", {"chat_id": chat_id, "text": part})
                    break
                except ApiError as exc:
                    if exc.code != 429 or attempt == 3:
                        raise
                    time.sleep(min(30, exc.retry_after or 3))
                except NetworkError:
                    if attempt == 3:
                        raise
                    time.sleep(2)

    def _run(self, job):
        started = self.clock()
        self._draft(job, "")  # "Thinking..." placeholder
        text, shown, next_draft = "", "", self.clock() + DRAFT_INTERVAL
        failed = False
        try:
            for piece in self.streamer(self.config, job.messages, job.stop):
                text += piece
                now = self.clock()
                if now >= next_draft and text.strip() and text != shown:
                    shown = text
                    delay = self._draft(job, text[:TG_LIMIT])
                    next_draft = self.clock() + max(DRAFT_INTERVAL, delay)
        except RouterError as exc:
            failed = True
            log.warning("LLM: роутер недоступен (%s)", exc)
        text = text.strip()
        stopped = job.stop.is_set()
        log.info(
            "LLM: ответ %s символов за %.1f с%s",
            len(text),
            self.clock() - started,
            ", остановлен" if stopped else "",
        )
        if not text:
            if not stopped:
                self._send(
                    job.chat_id,
                    "Не получилось ответить. Попробуйте ещё раз чуть позже.",
                )
            return None
        if failed:
            text += "\n\n[ответ оборвался]"
        elif stopped:
            text += " …"
        self._send(job.chat_id, text)
        return text
