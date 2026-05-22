import asyncio
import re

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


async def _download_file(bot, url: str, headers: list[str] | None = None) -> str:
    """调用 NapCat /download_file 下载文件，返回本地路径"""
    payload = {"url": url}
    if headers:
        payload["headers"] = headers
    resp = await bot.call_action("download_file", **payload)
    return resp["file"]


def _format_duration(ms: int) -> str:
    """将毫秒转换为可读时长"""
    seconds = ms // 1000
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _shorten_number(n: int) -> str:
    """将大数字简写为 k/m 格式"""
    if n >= 1000000:
        return f"{n / 1000000:.1f}m"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _get_cover_url(video_info: dict, is_thumbs: bool) -> str | None:
    """从 video 数据中提取封面图 URL"""
    if is_thumbs:
        big_thumbs = video_info.get("big_thumbs", [])
        if big_thumbs and big_thumbs[0].get("img_url"):
            return big_thumbs[0]["img_url"]
    cover_list = video_info.get("cover", {}).get("url_list", [])
    return cover_list[0] if cover_list else None


def _check_video_limits(config: AstrBotConfig, duration_ms: int, data_size: int | None) -> str | None:
    """检查视频时长和大小限制，返回 None 表示通过，返回字符串表示被拦截的原因"""
    max_duration = config.get("videoMaxDuration", 0)
    if max_duration > 0:
        duration_sec = duration_ms // 1000
        if duration_sec > max_duration:
            return f"视频时长 {duration_sec}秒 超过限制 {max_duration}秒，已跳过"

    max_size = config.get("videoMaxSize", 0)
    if max_size > 0 and data_size:
        size_mb = data_size / 1024 / 1024
        if size_mb > max_size:
            return f"视频大小 {size_mb:.1f}MB 超过限制 {max_size}MB，已跳过"

    return None


class DouyinParserPlugin(Star):
    _DOUYIN_PATTERN = re.compile(
        r"(https?://(?:[a-z0-9-]*\.)?(?:douyin\.com|tiktok\.com)[^\s]*)"
    )
    _FORWARD_NICKNAME = "抖音图文"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client = httpx.AsyncClient(timeout=30)

    async def initialize(self):
        logger.info("douyin_parser 插件已加载")

    async def terminate(self):
        await self._client.aclose()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        if not text:
            return

        match = self._DOUYIN_PATTERN.search(text)
        if not match:
            return

        url = match.group(1)
        is_douyin = "douyin.com" in url
        logger.info(f"检测到{'抖音' if is_douyin else 'TikTok'}链接: {url}")

        # 调 API 获取视频数据
        api_base = self.config.get("url", "")
        if not api_base:
            yield event.plain_result("错误：未配置 API 地址")
            return

        api_url = f"{api_base}/api/hybrid/video_data"
        try:
            resp = await self._client.get(
                api_url, params={"url": url, "minimal": False}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"请求 API 失败: {e}")
            yield event.plain_result(f"请求解析失败: {e}")
            return

        if data.get("code") != 200 or not data.get("data"):
            logger.error(f"API 返回异常: {data}")
            yield event.plain_result("解析失败：API 返回异常")
            return

        video_data = data["data"]

        # 构建视频信息文本
        desc = video_data.get("desc", "")
        author = video_data.get("author", {}).get("nickname", "未知")
        duration_ms = video_data.get("duration", 0)
        stats = video_data.get("statistics", {})

        info_lines = [
            f"标题：{desc}",
            f"作者：{author}",
        ]

        if self.config.get("isSendVideo", False):
            info_lines.append(f"时长：{_format_duration(duration_ms)}")

        info_lines.append(
            f"点赞：{_shorten_number(stats.get('digg_count', 0))}  "
            f"评论：{_shorten_number(stats.get('comment_count', 0))}  "
            f"收藏：{_shorten_number(stats.get('collect_count', 0))}  "
            f"分享：{_shorten_number(stats.get('share_count', 0))}"
        )
        info_text = "\n".join(info_lines)

        # 构建消息链：封面图在前，文字在后
        chain_segments = []
        cover_url = _get_cover_url(
            video_data.get("video", {}), self.config.get("isThumbs", False)
        )
        if cover_url:
            chain_segments.append({"type": "image", "data": {"file": cover_url}})
        chain_segments.append({"type": "text", "data": {"text": info_text}})

        # 只处理群消息
        if isinstance(event, AiocqhttpMessageEvent) and event.get_group_id():
            group_id = int(event.get_group_id())

            media_type = video_data.get("media_type", 0)
            is_phototext = is_douyin and media_type in (2, 42)

            if is_phototext:
                # 图文：只发合并转发，不发封面+文字
                if self.config.get("isSendVideo", False):
                    await self._send_phototext(event, group_id, video_data, url)
            else:
                # 视频/其他：先发封面+文字
                await event.bot.call_action(
                    "send_group_msg",
                    group_id=group_id,
                    message=chain_segments,
                )

                # 如果配置了发送视频
                if self.config.get("isSendVideo", False) and media_type == 4:
                    reason = _check_video_limits(
                        self.config,
                        duration_ms,
                        video_data.get("video", {}).get("download_addr", {}).get("data_size"),
                    )
                    if reason:
                        logger.info(reason)
                        yield event.plain_result(reason)
                    else:
                        video_url = f"{api_base}/api/download?url={url}&prefix=true&with_watermark=false"
                        yield event.chain_result([Video.fromURL(video_url)])

            event.stop_event()

    async def _send_phototext(
        self, event: AiocqhttpMessageEvent, group_id: int, video_data: dict, url: str
    ):
        images = video_data.get("images") or []
        if not images:
            return

        if len(images) > 1:
            await self._send_phototext_forward(event, group_id, video_data, images)
        else:
            # 单条图文，如果有视频则额外发送
            img = images[0]
            video_url = (
                img.get("video", {})
                .get("download_addr", {})
                .get("url_list", [None])[0]
            )
            if video_url:
                local_path = await _download_file(event.bot, video_url, ["Referer=https://www.douyin.com/"])
                await event.bot.call_action(
                    "send_group_msg",
                    group_id=group_id,
                    message=[{"type": "video", "data": {"file": f"file://{local_path}"}}],
                )

    async def _send_phototext_forward(
        self, event: AiocqhttpMessageEvent, group_id: int, video_data: dict, images: list
    ):
        """用合并转发发送多条图文"""
        info_text = (
            f"标题：{video_data.get('desc', '')}\n"
            f"作者：{video_data.get('author', {}).get('nickname', '未知')}"
        )
        nodes: list[dict] = [
            {
                "type": "node",
                "data": {
                    "user_id": event.get_self_id(),
                    "nickname": self._FORWARD_NICKNAME,
                    "content": [{"type": "text", "data": {"text": info_text}}],
                },
            }
        ]

        async def _make_node(img: dict) -> dict | None:
            video_cdn_url = (
                img.get("video", {})
                .get("download_addr", {})
                .get("url_list", [None])[0]
            )
            img_url = img.get("download_url_list", [None])[0]

            if video_cdn_url:
                local_path = await _download_file(
                    event.bot, video_cdn_url, ["Referer=https://www.douyin.com/"]
                )
                return {
                    "type": "node",
                    "data": {
                        "user_id": event.get_self_id(),
                        "nickname": self._FORWARD_NICKNAME,
                        "content": [
                            {"type": "video", "data": {"file": f"file://{local_path}"}}
                        ],
                    },
                }
            elif img_url:
                return {
                    "type": "node",
                    "data": {
                        "user_id": event.get_self_id(),
                        "nickname": self._FORWARD_NICKNAME,
                        "content": [{"type": "image", "data": {"file": img_url}}],
                    },
                }
            return None

        results = await asyncio.gather(*[_make_node(img) for img in images])
        nodes.extend(n for n in results if n is not None)

        await event.bot.call_action("send_group_forward_msg", group_id=group_id, messages=nodes)
