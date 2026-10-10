"""Shared SnowLuma/NapCat OneBot action handling, without backend credentials.

AstrBot's aiocqhttp ActionFailed keeps the response in ``result``. Read its
``wording`` instead of logging only the exception class, but never expose raw
URLs, headers or local paths from failure payloads in diagnostics or QQ error replies.
"""

from __future__ import annotations

import re
from typing import Any

_URL = re.compile(r"(?:https?://|file://)[^\s\"'<>]+", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_DTK_KEY = re.compile(r"\bdtk_[A-Za-z0-9_-]+\b")
_SECRET = re.compile(
    r"\b(?:authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|token|"
    r"password|secret|sig[_-]?session|session[_-]?key|u[_-]?key)\b"
    r"[\"']?\s*[:=]\s*[^\r\n]*",
    re.IGNORECASE,
)
_QUOTED_PATH = re.compile(r"""(["'])(?:[A-Za-z]:[\\/]|/|\\\\)[^\r\n]*?\1""")
_WINDOWS_PATH = re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\)[^\s\"'<>]+")
_POSIX_PATH = re.compile(r"(?<![\w/])/[\w.~-]+(?:/[^\s\"'<>]*)?")


def sanitize_detail(value: str, max_length: int = 600) -> str:
    value = _URL.sub("[URL hidden]", value)
    value = _BEARER.sub("Bearer [hidden]", value)
    value = _DTK_KEY.sub("[API key hidden]", value)
    value = _SECRET.sub("[credential hidden]", value)
    value = _QUOTED_PATH.sub("[path hidden]", value)
    value = _WINDOWS_PATH.sub("[path hidden]", value)
    value = _POSIX_PATH.sub("[path hidden]", value)
    # Keep the error, not a backend stack trace, and prevent log line injection.
    first_line = value.splitlines()[0] if value.splitlines() else ""
    return " ".join(first_line.split())[:max_length]


def _retcode(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


class OneBotActionError(Exception):
    def __init__(
        self,
        action: str,
        reason: str,
        *,
        retcode: int | None = None,
        detail: str = "",
        confirmed_failure: bool = False,
    ) -> None:
        self.action = action
        self.reason = reason
        self.retcode = retcode
        self.detail = sanitize_detail(detail)
        self.confirmed_failure = confirmed_failure
        super().__init__(self.diagnostic())

    def diagnostic(self) -> str:
        return (
            f"action={self.action}, reason={self.reason}, "
            f"retcode={self.retcode}, detail={self.detail}"
        )

    @property
    def upload_failed(self) -> bool:
        # A missing response is not proof of failure. Never resend a potentially
        # successful album after an API/WebSocket timeout.
        return self.confirmed_failure and self.reason.startswith("HIGHWAY_")

    def user_message(self) -> str:
        if self.reason == "HIGHWAY_CONNECT_TIMEOUT":
            return "媒体已下载，但 QQ Highway 上传连接超时，请检查 OneBot 后端到 QQ 的网络"
        if self.reason == "HIGHWAY_UPLOAD_FAILED":
            return "媒体已下载，但 QQ Highway 上传失败，请查看 OneBot 后端上传日志"
        if self.reason == "ONEBOT_TIMEOUT":
            if self.action == "download_file":
                return "等待 OneBot 下载响应超时，后端下载可能仍在继续，请检查后端连接和下载日志"
            return "等待 OneBot 响应超时，发送结果尚不确定，请先确认聊天中是否已收到媒体"
        if self.reason == "ONEBOT_CONNECTION_ERROR":
            return "无法连接 OneBot 后端，请检查 AstrBot 与 SnowLuma/NapCat 的连接"
        if self.action == "download_file":
            if self.reason == "DOWNLOAD_TIMEOUT":
                return "OneBot 媒体下载超时，请检查后端访问平台 CDN 的网络和下载超时设置"
            if self.reason == "DOWNLOAD_UNSUPPORTED":
                return "OneBot 后端不支持 download_file，请检查后端版本和接口配置"
            return "媒体下载失败，请检查 OneBot 后端访问平台 CDN 的网络及 download_file 接口"
        message = "媒体发送失败，请查看 OneBot 后端和 QQ 的发送日志"
        if self.retcode is not None:
            message += f"（retcode={self.retcode}）"
        return message

    @classmethod
    def from_failure(
        cls, action: str, result: Any = None, exception: Exception | None = None
    ) -> OneBotActionError:
        result = result if isinstance(result, dict) else {}
        parts = [
            result[key]
            for key in ("wording", "message", "msg")
            if isinstance(result.get(key), str) and result[key]
        ]
        if not parts and exception is not None:
            for key in ("wording", "message", "msg"):
                value = getattr(exception, key, None)
                if isinstance(value, str) and value:
                    parts.append(value)
        if not parts and exception is not None:
            # ActionFailed.__str__ can dump the entire response, including
            # fields unrelated to the error. Never render such a response.
            parts.append(f"{type(exception).__name__} (no wording)" if result else str(exception))
        detail = "; ".join(parts)
        lower = detail.lower()
        exception_name = type(exception).__name__ if exception else ""
        confirmed = result.get("status") == "failed" or exception_name == "ActionFailed"
        if exception_name in ("TimeoutError", "TimingError"):
            reason = "ONEBOT_TIMEOUT"
            confirmed = False
        elif exception_name in ("ApiNotAvailable", "NetworkError", "HttpFailed"):
            reason = "ONEBOT_CONNECTION_ERROR"
        elif action.startswith("send_") and "highway" in lower:
            reason = (
                "HIGHWAY_CONNECT_TIMEOUT"
                if "tcp connect timeout" in lower
                else "HIGHWAY_UPLOAD_FAILED"
            )
        elif action == "download_file":
            reason = (
                "DOWNLOAD_UNSUPPORTED"
                if result.get("retcode") in (1404, "1404")
                or any(
                    marker in lower
                    for marker in (
                        "unknown action",
                        "unsupported action",
                        "action not found",
                        "download_file is not supported",
                        "download_file not supported",
                    )
                )
                else "DOWNLOAD_ERROR"
            )
        else:
            reason = "ONEBOT_ACTION_FAILED"
        code = _retcode(result.get("retcode"))
        if code is None and exception is not None:
            code = _retcode(getattr(exception, "retcode", None))
        return cls(action, reason, retcode=code, detail=detail, confirmed_failure=confirmed)


class MediaDownloadError(OneBotActionError):
    def __init__(
        self,
        detail: str = "",
        *,
        reason: str = "DOWNLOAD_ERROR",
        last_error: OneBotActionError | None = None,
    ) -> None:
        super().__init__(
            "download_file",
            last_error.reason if last_error else reason,
            retcode=last_error.retcode if last_error else None,
            detail=last_error.detail if last_error else detail,
            confirmed_failure=last_error.confirmed_failure if last_error else False,
        )


async def call_action(bot, action: str, **payload: Any) -> Any:
    try:
        result = await bot.call_action(action, **payload)
    except Exception as exc:
        # No aiocqhttp import needed: support both its ActionFailed.result and
        # adapters that expose wording/message attributes instead.
        result = getattr(exc, "result", None)
        if not isinstance(result, dict) and exc.args and isinstance(exc.args[0], dict):
            result = exc.args[0]
        raise OneBotActionError.from_failure(action, result, exc) from exc
    if isinstance(result, dict) and result.get("status") == "failed":
        raise OneBotActionError.from_failure(action, result)
    return result
