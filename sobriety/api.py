"""Small Bot API client. No token-bearing exceptions escape this boundary."""

import http.client
import json
import logging
import time
import urllib.error
import urllib.request

from .diagnostics import action, redact

log = logging.getLogger("sobriety")


class ApiError(Exception):
    def __init__(self, code, retry_after=None, *, method=None, description=None):
        code = code if type(code) is int else None
        retry_after = (
            retry_after if type(retry_after) is int and retry_after > 0 else None
        )
        super().__init__(f"Telegram API error {code}")
        self.code = code
        self.retry_after = retry_after
        self.method = method
        self.description = description

    @property
    def message_not_modified(self):
        return (
            self.code == 400
            and self.method == "editMessageText"
            and isinstance(self.description, str)
            and (
                self.description == "Bad Request: message is not modified"
                or self.description.startswith("Bad Request: message is not modified:")
            )
        )

    @property
    def callback_expired(self):
        return (
            self.code == 400
            and self.method == "answerCallbackQuery"
            and self.description
            == "Bad Request: query is too old and response timeout expired or query ID is invalid"
        )


class NetworkError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Telegram:
    def __init__(self, token):
        self._token = token
        self._opener = urllib.request.build_opener(NoRedirect())

    def call(self, method, fields=None, timeout=15):
        started = time.monotonic()
        try:
            result = self._call(method, fields, timeout)
        except ApiError as exc:
            log.log(
                logging.INFO
                if exc.message_not_modified or exc.callback_expired
                else logging.WARNING,
                "Telegram method=%s action=%s code=%s error=%s retry_after=%s",
                method,
                action.get(),
                exc.code,
                exc.description or "описание отсутствует",
                exc.retry_after,
            )
            raise
        except NetworkError:
            log.warning(
                "Telegram method=%s action=%s: ошибка соединения", method, action.get()
            )
            raise
        log.info(
            "Telegram method=%s: OK (%.0f ms)",
            method,
            (time.monotonic() - started) * 1000,
        )
        return result

    def _call(self, method, fields=None, timeout=15):
        fields = {k: v for k, v in (fields or {}).items() if v is not None}
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(fields).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    data = json.load(response)
            except urllib.error.HTTPError as exc:
                with exc:
                    try:
                        data = json.load(exc)
                    except (ValueError, OSError):
                        raise ApiError(exc.code, method=method) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise NetworkError("Telegram connection failed") from None
        if not isinstance(data, dict):
            raise ApiError(None, method=method)
        if data.get("ok") is not True:
            parameters = data.get("parameters") or {}
            retry = (
                parameters.get("retry_after") if isinstance(parameters, dict) else None
            )
            description = data.get("description")
            if isinstance(description, str):
                # Never echo a token, URL or a full reflected outgoing message.
                secret_text = fields.get("text")
                secrets = (
                    self._token,
                    secret_text if isinstance(secret_text, str) else "",
                )
                description = " ".join(redact(description, secrets).split())[:500]
            else:
                description = None
            raise ApiError(
                data.get("error_code"), retry, method=method, description=description
            )
        return data.get("result")
