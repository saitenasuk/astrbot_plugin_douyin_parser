"""DTK v5 REST client and the small, platform-neutral model used by the plugin.

This module has no AstrBot dependency. DTK credentials are only sent to the REST
API; the media URLs returned by DTK are downloaded separately by the OneBot backend.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx


class DTKError(Exception):
    """A safe, structured failure; never echo arbitrary server prose to QQ."""

    def __init__(
        self,
        code: str,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.request_id = request_id
        self.task_id = task_id
        self.retry_after = retry_after

    def user_message(self) -> str:
        messages = {
            "MISSING_URL": "未配置 DTK API 地址",
            "INVALID_BASE_URL": "DTK 地址无效，请填写 http(s)://主机:端口，可包含反向代理路径前缀",
            "UNAUTHENTICATED": "DTK 认证失败，请检查插件的 API Key",
            "FORBIDDEN_SCOPE": "DTK API Key 缺少对应平台的读取权限",
            "NOT_FOUND": "作品不存在、已删除或不可访问",
            "CONTENT_PRIVATE": "作品为私密内容，当前 DTK 身份无法访问",
            "INVALID_URL": "链接无效或不属于支持的平台",
            "INVALID_PARAM": "DTK 拒绝了请求参数，请检查服务版本与配置",
            "UNSUPPORTED_CONTENT": "暂不支持该内容，请发送抖音或 TikTok 的视频/图文作品链接",
            "IDENTITY_POOL_EXHAUSTED": "DTK 暂无可用的平台身份，请检查身份池或稍后再试",
            "ENDPOINT_CIRCUIT_OPEN": "DTK 上游接口暂时熔断，请稍后再试",
            "UPSTREAM_RISK_CONTROL": "平台触发风控，请检查 DTK 身份状态",
            "UPSTREAM_CHANGED": "平台返回结构发生变化，请检查 DTK 服务",
            "SIGNING_FAILED": "DTK 签名失败，请检查身份及浏览器服务",
            "TASK_NOT_FOUND": "DTK 任务不存在或结果已过期，请重新发送链接",
            "QUEUE_FULL": "DTK 任务队列已满，请稍后再试",
            "RATE_LIMITED": "DTK 请求频率超限，请稍后再试或调大任务轮询间隔",
            "CANCELLED": "DTK 解析任务已被取消",
            "NOT_CONFIGURED": "DTK 所需服务尚未配置",
            "HTTP_TIMEOUT": "连接 DTK 超时，请检查服务或调大 API 请求超时",
            "TASK_TIMEOUT": "等待 DTK 解析结果超时，请检查队列或调大任务等待超时",
            "CONNECTION_ERROR": "无法连接 DTK，请检查 API 地址及网络",
            "INVALID_RESPONSE": "DTK 响应不符合 v5 接口格式，请检查地址、版本及反向代理",
        }
        message = messages.get(self.code, "DTK 解析失败，请查看插件和 DTK 日志")
        if self.retry_after is not None and self.retry_after > 0:
            message += f"（建议 {self.retry_after} 秒后重试）"
        return message


def _optional_int(value: Any) -> int | None:
    """Keep absent metrics distinct from measured zero."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= 0 else None


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parts = urlsplit(value)
        if parts.scheme in ("http", "https") and parts.hostname and not parts.username:
            _ = parts.port  # Validate malformed/out-of-range ports too.
            return value
    except ValueError:
        pass
    return None


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise DTKError("MISSING_URL")
    if not _http_url(value):
        raise DTKError("INVALID_BASE_URL")
    parts = urlsplit(value)
    if parts.query or parts.fragment:
        raise DTKError("INVALID_BASE_URL")
    path = parts.path.rstrip("/")
    # Accept either the service root or an explicitly entered API root.
    path = path.removesuffix("/api/v1")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass(frozen=True)
class MediaAsset:
    urls: tuple[str, ...]
    size_bytes: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class AlbumItem:
    image: MediaAsset
    motion: MediaAsset | None = None


@dataclass(frozen=True)
class Post:
    platform: str
    content_id: str
    kind: str
    description: str
    author: str
    web_url: str
    duration_ms: int | None
    stats: dict[str, int | None]
    covers: tuple[MediaAsset, ...]
    thumbnail: MediaAsset | None
    video: MediaAsset | None
    images: tuple[AlbumItem, ...]

    def cover(self) -> MediaAsset | None:
        """Return the ordinary cover, never the storyboard thumbnail."""
        if self.covers:
            return self.covers[0]
        return self.images[0].image if self.images else None


def _asset(
    value: Any,
    *,
    raw: bool = False,
    duration_ms: int | None = None,
) -> MediaAsset | None:
    if not isinstance(value, dict):
        return None
    candidates = [value.get("url")]
    mirrors = value.get("url_list" if raw else "urls")
    if isinstance(mirrors, list):
        candidates.extend(mirrors)
    urls = tuple(dict.fromkeys(url for item in candidates if (url := _http_url(item))))
    if not urls:
        return None
    return MediaAsset(
        urls=urls,
        size_bytes=_optional_int(value.get("data_size" if raw else "size_bytes")),
        duration_ms=duration_ms,
    )


def _motion_asset(value: Any) -> MediaAsset | None:
    """v5's Image model omits live-photo clips; supplement from include_raw."""
    if not isinstance(value, dict):
        return None
    duration_ms = _optional_int(value.get("duration"))
    for key in ("play_addr", "download_addr"):
        asset = _asset(value.get(key), raw=True, duration_ms=duration_ms)
        if asset:
            return asset
    return None


def parse_post(data: Any) -> Post:
    """Consume DTK's normalized Content, not a v4 aweme_detail payload."""
    if not isinstance(data, dict):
        raise DTKError("INVALID_RESPONSE")
    # /parse also accepts author links. Do not mistake an Author for Content.
    if "content_id" not in data:
        if "uid" in data and "nickname" in data:
            raise DTKError("UNSUPPORTED_CONTENT")
        raise DTKError("INVALID_RESPONSE")
    content_id = data.get("content_id")
    platform, kind = data.get("platform"), data.get("kind")
    if not isinstance(content_id, str) or not content_id:
        raise DTKError("INVALID_RESPONSE")
    if platform not in ("douyin", "tiktok") or kind not in ("video", "image_album"):
        raise DTKError("UNSUPPORTED_CONTENT")
    if data.get("is_private"):
        raise DTKError("CONTENT_PRIVATE")
    if data.get("is_deleted"):
        raise DTKError("NOT_FOUND")

    media = data.get("media")
    author = data.get("author")
    stats = data.get("stats")
    if not all(isinstance(value, dict) for value in (media, author, stats)):
        raise DTKError("INVALID_RESPONSE")
    for key in ("covers", "streams", "images"):
        if media.get(key) is not None and not isinstance(media[key], list):
            raise DTKError("INVALID_RESPONSE")
    duration_ms = _optional_int(data.get("duration_ms"))
    covers = tuple(asset for item in (media.get("covers") or []) if (asset := _asset(item)))
    video = None
    streams = [media.get("video"), *(media.get("streams") or [])]
    for stream in streams:
        if isinstance(stream, dict) and not stream.get("watermark"):
            video = _asset(stream, duration_ms=duration_ms)
            if video:
                break

    raw = data.get("raw")
    raw = raw if isinstance(raw, dict) else {}
    raw_images = raw.get("images")
    raw_images = raw_images if isinstance(raw_images, list) else []
    images = []
    for index, image in enumerate(media.get("images") or []):
        asset = _asset(image)
        if not asset:
            continue
        raw_image = raw_images[index] if index < len(raw_images) else None
        motion = None
        if platform == "douyin" and isinstance(raw_image, dict):
            motion = _motion_asset(raw_image.get("video"))
        images.append(AlbumItem(image=asset, motion=motion))

    thumbnail = None
    raw_video = raw.get("video")
    if isinstance(raw_video, dict) and isinstance(raw_video.get("big_thumbs"), list):
        for thumb in raw_video["big_thumbs"]:
            if isinstance(thumb, dict) and (url := _http_url(thumb.get("img_url"))):
                thumbnail = MediaAsset((url,))
                break

    return Post(
        platform=platform,
        content_id=content_id,
        kind=kind,
        description=data.get("description") or data.get("title") or "无标题",
        author=author.get("nickname") or "未知",
        web_url=_http_url(data.get("web_url")) or "",
        duration_ms=duration_ms,
        stats={
            key: _optional_int(stats.get(key))
            for key in ("digg_count", "comment_count", "collect_count", "share_count")
        },
        covers=covers,
        thumbnail=thumbnail,
        video=video,
        images=tuple(images),
    )


class DTKClient:
    """Submit once and poll the same task until completion, failure or deadline."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        request_timeout: float = 30,
        task_timeout: float = 120,
        poll_interval: float = 2,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key.strip()
        self.request_timeout = request_timeout
        self.task_timeout = task_timeout
        self.poll_interval = poll_interval
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def _request(
        self, base_url: str, method: str, path: str, **kwargs: Any
    ) -> tuple[dict, str | None]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = await self._http.request(
                method,
                f"{base_url}/api/v1/{path}",
                headers=headers,
                params={"lang": "zh"},
                timeout=self.request_timeout,
                # Never forward a credential to a redirect destination.
                follow_redirects=False,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise DTKError("HTTP_TIMEOUT") from exc
        except httpx.InvalidURL as exc:
            raise DTKError("INVALID_BASE_URL") from exc
        except httpx.RequestError as exc:
            raise DTKError("CONNECTION_ERROR") from exc

        request_id = response.headers.get("X-Request-ID")
        try:
            body = response.json()
        except ValueError as exc:
            raise DTKError("INVALID_RESPONSE", request_id=request_id) from exc
        if not isinstance(body, dict):
            raise DTKError("INVALID_RESPONSE", request_id=request_id)
        meta = body.get("meta")
        if isinstance(meta, dict):
            request_id = request_id or meta.get("request_id")
        if body.get("success") is False:
            raise self._error(body.get("error"), request_id=request_id)
        if not response.is_success or body.get("success") is not True:
            raise DTKError("INVALID_RESPONSE", request_id=request_id)
        payload = body.get("data")
        if not isinstance(payload, dict):
            raise DTKError("INVALID_RESPONSE", request_id=request_id)
        return payload, request_id

    @staticmethod
    def _error(error: Any, **context: Any) -> DTKError:
        if not isinstance(error, dict):
            return DTKError("INVALID_RESPONSE", **context)
        code = error.get("code")
        return DTKError(
            code if isinstance(code, str) else "INVALID_RESPONSE",
            retry_after=_optional_int(error.get("retry_after")),
            **context,
        )

    async def parse_url(self, url: str) -> Post:
        base_url = normalize_base_url(self.base_url)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.task_timeout
        task_id = None
        request_id = None

        async def request(method: str, path: str, **kwargs: Any) -> tuple[dict, str | None]:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(
                self._request(base_url, method, path, **kwargs), timeout=remaining
            )

        try:
            payload, request_id = await request(
                "POST",
                "parse",
                # No wait= parameter: respect any instance's synchronous ceiling.
                # raw only supplements live photos and big_thumbs; ordinary
                # media, metadata and statistics use the v5 normalized contract.
                json={"url": url, "include_raw": True},
            )
            if "content_id" in payload or "uid" in payload:
                return parse_post(payload)
            try:
                task_id = str(UUID(payload["task_id"]))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise DTKError("INVALID_RESPONSE", request_id=request_id) from exc

            while True:
                payload, request_id = await request("GET", f"tasks/{task_id}")
                state = payload.get("state")
                if state == "done":
                    return parse_post(payload.get("data"))
                if state == "failed":
                    raise self._error(payload.get("error"), request_id=request_id, task_id=task_id)
                if state not in ("queued", "running"):
                    raise DTKError("INVALID_RESPONSE", request_id=request_id, task_id=task_id)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.sleep(min(self.poll_interval, remaining))
        except asyncio.TimeoutError as exc:
            raise DTKError("TASK_TIMEOUT", request_id=request_id, task_id=task_id) from exc
        except DTKError as exc:
            exc.request_id = exc.request_id or request_id
            exc.task_id = exc.task_id or task_id
            raise
