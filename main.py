from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import quote, urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .dtk_client import AlbumItem, DTKClient, DTKError, MediaAsset, Post
from .onebot_media import MediaDownloadError, OneBotActionError, call_action

_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'，。！？；：、【】（）《》\u4e00-\u9fff]+", re.IGNORECASE
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _extract_url(text: str) -> str | None:
    for match in _URL_PATTERN.finditer(text):
        url = match.group().rstrip(".,!;:)]}")
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            continue
        if not parts.username and any(
            host == domain or host.endswith(f".{domain}") for domain in ("douyin.com", "tiktok.com")
        ):
            return url
    return None


def _config_number(config: AstrBotConfig, key: str, default: int) -> int:
    try:
        return max(0, int(config.get(key, default)))
    except (TypeError, ValueError, OverflowError):
        return default


def _format_duration(ms: int | None) -> str:
    if ms is None:
        return "未知"
    seconds = ms // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def _shorten_number(number: int | None) -> str:
    if number is None:
        return "未知"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}m"
    if number >= 1000:
        return f"{number / 1000:.1f}k"
    return str(number)


def _check_video_limits(
    config: AstrBotConfig, duration_ms: int | None, size_bytes: int | None
) -> str | None:
    max_duration = _config_number(config, "videoMaxDuration", 0)
    if max_duration:
        if duration_ms is None:
            return "未获取到视频时长，无法核验时长限制，已跳过"
        # Compare milliseconds, not floored seconds (e.g. 60.9s exceeds 60s).
        if duration_ms > max_duration * 1000:
            return f"视频时长 {duration_ms / 1000:g}秒 超过限制 {max_duration}秒，已跳过"
    max_size = _config_number(config, "videoMaxSize", 0)
    if max_size:
        if size_bytes is None:
            return "未获取到视频大小，无法核验大小限制，已跳过"
        if size_bytes > max_size * 1024 * 1024:
            return f"视频大小 {size_bytes / 1024 / 1024:.1f}MB 超过限制 {max_size}MB，已跳过"
    return None


def _file_uri(path: str) -> str:
    """Use the OneBot backend's filesystem, including when it is in another container/OS."""
    if path.startswith("file://"):
        return path
    windows_path = PureWindowsPath(path)
    if windows_path.is_absolute():
        encoded = quote(windows_path.as_posix(), safe="/:")
        return f"file:{encoded}" if windows_path.drive.startswith("\\\\") else f"file:///{encoded}"
    posix_path = PurePosixPath(path)
    if posix_path.is_absolute():
        return "file://" + quote(str(posix_path), safe="/")
    raise ValueError("OneBot download_file did not return an absolute file path")


async def _download_file(bot, url: str, headers: list[str] | None = None) -> str:
    payload = {"url": url}
    if headers:
        payload["headers"] = headers
    response = await call_action(bot, "download_file", **payload)
    if isinstance(response, dict) and "file" not in response:
        response = response.get("data")
    path = response.get("file") if isinstance(response, dict) else None
    if not isinstance(path, str) or not path:
        raise MediaDownloadError("download_file did not return a file path")
    return path


class DouyinParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._dtk = DTKClient(
            base_url=config.get("url") or "",
            api_key=config.get("apiKey") or "",
            request_timeout=max(1, _config_number(config, "apiTimeout", 30)),
            task_timeout=max(1, _config_number(config, "taskTimeout", 120)),
            poll_interval=max(1, _config_number(config, "taskPollInterval", 2)),
        )
        self._downloads = asyncio.Semaphore(
            max(1, _config_number(config, "downloadConcurrency", 3))
        )

    async def initialize(self):
        logger.info("douyin_parser 插件已加载（DTK v5）")

    async def terminate(self):
        await self._dtk.aclose()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # Avoid consuming identity quota on platforms we cannot reply to.
        if not isinstance(event, AiocqhttpMessageEvent):
            return
        url = _extract_url(event.message_str or "")
        if not url:
            return
        logger.info("douyin_parser 检测到抖音/TikTok 作品链接")
        try:
            try:
                post = await self._dtk.parse_url(url)
            except DTKError as exc:
                logger.warning(
                    "DTK 解析失败: code=%s, request_id=%s, task_id=%s",
                    exc.code,
                    exc.request_id,
                    exc.task_id,
                )
                yield event.plain_result(f"解析失败：{exc.user_message()}")
                return

            video_attempted = False
            try:
                if post.kind == "image_album" and self.config.get("isSendVideo", False):
                    await self._send_album(event, post)
                else:
                    await self._send_info(event, post)
                    if post.kind == "video" and self.config.get("isSendVideo", False):
                        reason = _check_video_limits(
                            self.config,
                            post.duration_ms,
                            post.video.size_bytes if post.video else None,
                        )
                        if reason:
                            yield event.plain_result(reason)
                        elif not post.video:
                            yield event.plain_result("未获取到可下载的无水印视频地址")
                        else:
                            video_attempted = True
                            file = await self._download_asset(event, post.video, post.platform)
                            await self._send_message(
                                event, [{"type": "video", "data": {"file": file}}]
                            )
            except OneBotActionError as exc:
                logger.warning("douyin_parser 媒体处理失败: %s", exc.diagnostic())
                message = exc.user_message()
                if video_attempted and self.config.get("sendLinkOnFailure", True):
                    message += self._media_links(post, url)
                yield event.plain_result(message)
            except Exception as exc:
                logger.warning("douyin_parser 媒体处理异常: %s", type(exc).__name__)
                yield event.plain_result("媒体处理失败，请查看插件及 OneBot 后端日志")
        finally:
            event.stop_event()

    @staticmethod
    def _info_text(post: Post) -> str:
        lines = [f"标题：{post.description}", f"作者：{post.author}"]
        if post.kind == "video":
            lines.append(f"时长：{_format_duration(post.duration_ms)}")
        labels = (
            ("点赞", "digg_count"),
            ("评论", "comment_count"),
            ("收藏", "collect_count"),
            ("分享", "share_count"),
        )
        lines.append(
            "  ".join(f"{label}：{_shorten_number(post.stats.get(key))}" for label, key in labels)
        )
        return "\n".join(lines)

    @staticmethod
    def _target(event: AiocqhttpMessageEvent) -> tuple[str, dict]:
        group_id = event.get_group_id()
        if group_id:
            return "group", {"group_id": int(group_id)}
        return "private", {"user_id": int(event.get_sender_id())}

    async def _send_message(self, event: AiocqhttpMessageEvent, message: list[dict]):
        kind, target = self._target(event)
        await call_action(event.bot, f"send_{kind}_msg", **target, message=message)

    async def _download_asset(
        self, event: AiocqhttpMessageEvent, asset: MediaAsset, platform: str
    ) -> str:
        referer = "https://www.douyin.com/" if platform == "douyin" else "https://www.tiktok.com/"
        headers = [f"Referer={referer}", f"User-Agent={_USER_AGENT}"]

        async def download() -> str:
            last_error = None
            for url in asset.urls:
                try:
                    path = await _download_file(event.bot, url, headers)
                    return _file_uri(path)
                except OneBotActionError as exc:
                    last_error = exc
                    logger.debug("douyin_parser CDN 下载失败: %s", exc.diagnostic())
                    if exc.reason in (
                        "DOWNLOAD_UNSUPPORTED",
                        "ONEBOT_CONNECTION_ERROR",
                        "ONEBOT_TIMEOUT",
                    ):
                        break
                except ValueError:
                    last_error = MediaDownloadError("download_file returned an invalid file path")
            raise MediaDownloadError("all CDN mirrors failed", last_error=last_error)

        timeout = max(1, _config_number(self.config, "mediaDownloadTimeout", 120))
        # Waiting for a concurrency slot must not consume the download deadline.
        # The semaphore is shared across messages to avoid unbounded OneBot jobs.
        async with self._downloads:
            try:
                return await asyncio.wait_for(download(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise MediaDownloadError(
                    "OneBot download timed out", reason="DOWNLOAD_TIMEOUT"
                ) from exc

    async def _send_info(self, event: AiocqhttpMessageEvent, post: Post):
        segments = []
        if self.config.get("isThumbs", False) and (cover := post.cover(prefer_thumbnail=True)):
            try:
                file = await self._download_asset(event, cover, post.platform)
                segments.append({"type": "image", "data": {"file": file}})
            except MediaDownloadError as exc:
                logger.info("douyin_parser 封面下载失败，继续发送文字: %s", exc.diagnostic())
        segments.append({"type": "text", "data": {"text": self._info_text(post)}})
        try:
            await self._send_message(event, segments)
        except OneBotActionError as exc:
            if len(segments) == 1 or not exc.upload_failed:
                raise
            logger.warning("douyin_parser 封面上传失败，改发文字: %s", exc.diagnostic())
            await self._send_message(event, segments[-1:])

    @staticmethod
    def _media_links(post: Post, original_url: str) -> str:
        lines = [f"作品链接：{post.web_url or original_url}"]
        if post.video and post.video.urls:
            lines.append(f"媒体直链（可能过期或受平台访问限制）：{post.video.urls[0]}")
        return "\n" + "\n".join(lines)

    async def _album_segments(
        self,
        event: AiocqhttpMessageEvent,
        post: Post,
        item: AlbumItem,
        index: int,
        *,
        force_static: bool = False,
        note: str | None = None,
    ) -> list[dict]:
        if item.motion and not force_static:
            reason = _check_video_limits(
                self.config, item.motion.duration_ms, item.motion.size_bytes
            )
            if reason:
                note = f"第 {index} 张实况图：{reason}，改发静态图片"
            else:
                try:
                    file = await self._download_asset(event, item.motion, post.platform)
                    return [{"type": "video", "data": {"file": file}}]
                except MediaDownloadError as exc:
                    logger.info("douyin_parser 实况视频下载失败，改发静态图: %s", exc.diagnostic())
                    note = f"第 {index} 张实况视频下载失败，改发静态图片"
        try:
            file = await self._download_asset(event, item.image, post.platform)
            segments = [{"type": "image", "data": {"file": file}}]
        except MediaDownloadError as exc:
            logger.info("douyin_parser 图文图片下载失败: %s", exc.diagnostic())
            segments = []
            note = f"第 {index} 张图片下载失败"
        if note:
            segments.append({"type": "text", "data": {"text": note}})
        return segments

    async def _send_album(self, event: AiocqhttpMessageEvent, post: Post):
        if not post.images:
            await self._send_info(event, post)
            await self._send_message(
                event, [{"type": "text", "data": {"text": "未获取到图文作品的图片地址"}}]
            )
            return
        if len(post.images) == 1:
            segments = await self._album_segments(event, post, post.images[0], 1)
            segments.append({"type": "text", "data": {"text": self._info_text(post)}})
            try:
                await self._send_message(event, segments)
            except OneBotActionError as exc:
                if not exc.upload_failed or not any(s["type"] == "video" for s in segments):
                    raise
                logger.warning("douyin_parser 实况视频上传失败，改发静态图: %s", exc.diagnostic())
                fallback = await self._album_segments(
                    event,
                    post,
                    post.images[0],
                    1,
                    force_static=True,
                    note="实况视频 QQ 上传失败，改发静态图片",
                )
                fallback.append({"type": "text", "data": {"text": self._info_text(post)}})
                await self._send_message(event, fallback)
            return

        nickname = "抖音图文" if post.platform == "douyin" else "TikTok图文"

        def node(content: list[dict]) -> dict:
            return {
                "type": "node",
                "data": {
                    "user_id": event.get_self_id(),
                    "nickname": nickname,
                    "content": content,
                },
            }

        nodes = [node([{"type": "text", "data": {"text": self._info_text(post)}}])]
        results = await asyncio.gather(
            *(
                self._album_segments(event, post, item, index)
                for index, item in enumerate(post.images, start=1)
            )
        )
        nodes.extend(node(segments) for segments in results)
        kind, target = self._target(event)
        action = f"send_{kind}_forward_msg"
        try:
            await call_action(event.bot, action, **target, messages=nodes)
        except OneBotActionError as exc:
            if not exc.upload_failed or not any(
                segment["type"] == "video" for segments in results for segment in segments
            ):
                raise
            logger.warning("douyin_parser 实况转发上传失败，改发静态图: %s", exc.diagnostic())
            fallback = []
            for index, (item, segments) in enumerate(
                zip(post.images, results, strict=True), start=1
            ):
                if any(segment["type"] == "video" for segment in segments):
                    segments = await self._album_segments(
                        event,
                        post,
                        item,
                        index,
                        force_static=True,
                        note=f"第 {index} 张实况视频 QQ 上传失败，改发静态图片",
                    )
                fallback.append(segments)
            # Only one downgrade attempt, reuse prepared static images, and do
            # not re-download/re-upload the same failed video.
            fallback_nodes = [nodes[0], *(node(segments) for segments in fallback)]
            await call_action(event.bot, action, **target, messages=fallback_nodes)
