import asyncio
import unittest
from unittest.mock import AsyncMock

import httpx

from dtk_client import DTKError, MediaAsset, parse_post
from tests.support import ActionFailed, FakeBot, FakeEvent, fixture, load_plugin
from tests.test_dtk_client import TASK_ID, URL, response

main = load_plugin()


class HelperTests(unittest.TestCase):
    def test_links_share_text_punctuation_case_and_domains(self):
        for text, expected in (
            (f"2.84 标题 {URL} 复制打开抖音", URL),
            ("https://v.douyin.com/AbCd/。复制打开抖音", "https://v.douyin.com/AbCd/"),
            ("(https://vm.tiktok.com/AbCd/)", "https://vm.tiktok.com/AbCd/"),
            ("HTTPS://WWW.TIKTOK.COM/@name/photo/123", "HTTPS://WWW.TIKTOK.COM/@name/photo/123"),
            ("https://douyin.com.evil.example/video/1", None),
            ("https://evildouyin.com/video/1", None),
            ("https://douyin.com@evil.example/video/1", None),
            ("https://user@www.douyin.com/video/1", None),
            ("今天没有链接", None),
            (f"https://other.example/ {URL}", URL),
        ):
            with self.subTest(text=text):
                self.assertEqual(main._extract_url(text), expected)

    def test_null_and_zero_formatting(self):
        self.assertEqual(main._shorten_number(None), "未知")
        self.assertEqual(main._shorten_number(0), "0")
        self.assertEqual(main._shorten_number(1500), "1.5k")
        self.assertEqual(main._shorten_number(1500000), "1.5m")
        self.assertEqual(main._format_duration(None), "未知")
        self.assertEqual(main._format_duration(0), "0秒")
        self.assertEqual(main._format_duration(62034), "1分2秒")
        self.assertEqual(main._format_duration(3661000), "1时1分1秒")

    def test_limits_at_boundaries_and_unknown_measurements(self):
        config = {"videoMaxDuration": 60, "videoMaxSize": 1}
        self.assertIsNone(main._check_video_limits(config, 60000, 1048576))
        self.assertIn("时长", main._check_video_limits(config, 60001, 1))
        self.assertIn("大小", main._check_video_limits(config, 1000, 1048577))
        self.assertIn("未获取", main._check_video_limits(config, None, 1))
        self.assertIn("未获取", main._check_video_limits(config, 1000, None))
        self.assertIsNone(main._check_video_limits({}, None, None))
        self.assertIsNone(main._check_video_limits(config, 0, 0))

    def test_config_number_handles_invalid_and_negative_values(self):
        self.assertEqual(main._config_number({"x": -1}, "x", 30), 0)
        self.assertEqual(main._config_number({"x": None}, "x", 30), 30)
        self.assertEqual(main._config_number({"x": "5"}, "x", 30), 5)

    def test_napcat_file_uri_linux_windows_and_unc(self):
        self.assertEqual(main._file_uri("/app/tmp/a b.mp4"), "file:///app/tmp/a%20b.mp4")
        self.assertEqual(main._file_uri(r"C:\NapCat\a b.mp4"), "file:///C:/NapCat/a%20b.mp4")
        self.assertEqual(
            main._file_uri(r"\\server\share\video.mp4"), "file://server/share/video.mp4"
        )
        self.assertEqual(main._file_uri("file:///tmp/video.mp4"), "file:///tmp/video.mp4")
        with self.assertRaises(ValueError):
            main._file_uri("relative/video.mp4")


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = {
            "url": "http://dtk:8000",
            "apiKey": "dtk_test_secret",
            "isSendVideo": True,
            "isSendCover": False,
            "isThumbs": False,
        }
        self.plugin = main.DouyinParserPlugin(None, self.config)
        self.addAsyncCleanup(self.plugin.terminate)
        self.plugin._dtk.parse_url = AsyncMock(return_value=parse_post(fixture("video")))
        self.event = FakeEvent(URL)

    async def consume(self, event=None):
        return [result async for result in self.plugin.on_message(event or self.event)]

    def sent(self, action="send_group_msg"):
        return [payload for name, payload in self.event.bot.calls if name == action]

    def downloads(self):
        return [payload for name, payload in self.event.bot.calls if name == "download_file"]

    async def test_video_info_then_napcat_download_and_send(self):
        self.assertEqual(await self.consume(), [])
        messages = self.sent()
        self.assertEqual(len(messages), 2)
        info = messages[0]["message"][0]["data"]["text"]
        self.assertIn("收藏：未知", info)
        self.assertIn("评论：0", info)
        self.assertEqual(
            messages[1]["message"],
            [
                {
                    "type": "video",
                    "data": {"file": "file:///napcat/temp/video.mp4"},
                }
            ],
        )
        self.assertEqual(self.downloads()[0]["url"], "https://cdn.example/video.mp4")
        self.assertIn("Referer=https://www.douyin.com/", self.downloads()[0]["headers"])
        self.assertNotIn("dtk_test_secret", str(self.event.bot.calls))
        self.assertTrue(self.event.stopped)

    async def test_metadata_only_when_media_disabled(self):
        self.config["isSendVideo"] = False
        await self.consume()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.downloads(), [])

    async def test_cover_switch_sends_regular_cover_not_thumbnail(self):
        self.config.update(isSendCover=True, isSendVideo=False)
        await self.consume()
        self.assertEqual(
            [payload["url"] for payload in self.downloads()], ["https://cdn.example/cover.jpg"]
        )
        self.assertEqual(self.sent()[0]["message"][0]["type"], "image")

    async def test_thumbnail_switch_does_not_send_regular_cover(self):
        self.config.update(isThumbs=True, isSendVideo=False)
        await self.consume()
        self.assertEqual(
            [payload["url"] for payload in self.downloads()], ["https://cdn.example/thumb.jpg"]
        )
        self.assertEqual(self.sent()[0]["message"][0]["type"], "image")

    async def test_cover_failure_does_not_prevent_text_reply(self):
        self.config.update(isSendCover=True, isSendVideo=False)
        self.event.bot = FakeBot(
            failures=("https://cdn.example/cover.jpg", "https://mirror.example/cover.jpg")
        )
        await self.consume()
        self.assertEqual(self.sent()[0]["message"][0]["type"], "text")
        self.assertNotIn("https://cdn.example/thumb.jpg", [p["url"] for p in self.downloads()])

    async def test_thumbnail_failure_does_not_prevent_text_reply(self):
        self.config.update(isThumbs=True, isSendVideo=False)
        self.event.bot = FakeBot(failures=("https://cdn.example/thumb.jpg",))
        await self.consume()
        self.assertEqual(self.sent()[0]["message"][0]["type"], "text")
        self.assertNotIn("https://cdn.example/cover.jpg", [p["url"] for p in self.downloads()])

    async def test_preview_switch_combinations_and_order(self):
        for cover_enabled, thumb_enabled, expected in (
            (False, False, []),
            (True, False, ["https://cdn.example/cover.jpg"]),
            (False, True, ["https://cdn.example/thumb.jpg"]),
            (True, True, ["https://cdn.example/cover.jpg", "https://cdn.example/thumb.jpg"]),
        ):
            with self.subTest(cover=cover_enabled, thumbnail=thumb_enabled):
                self.event = FakeEvent(URL)
                self.config.update(
                    isSendCover=cover_enabled, isThumbs=thumb_enabled, isSendVideo=False
                )
                self.assertEqual(await self.consume(), [])
                self.assertEqual([p["url"] for p in self.downloads()], expected)
                message = self.sent()[0]["message"]
                self.assertEqual([s["type"] for s in message], ["image"] * len(expected) + ["text"])

    async def test_missing_thumbnail_does_not_fall_back_to_cover(self):
        data = fixture("video")
        data["raw"] = None
        self.plugin._dtk.parse_url.return_value = parse_post(data)
        self.config.update(isThumbs=True, isSendVideo=False)
        await self.consume()
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.sent()[0]["message"][0]["type"], "text")

    async def test_missing_cover_does_not_fall_back_to_thumbnail(self):
        data = fixture("video")
        data["media"]["covers"] = []
        self.plugin._dtk.parse_url.return_value = parse_post(data)
        self.config.update(isSendCover=True, isSendVideo=False)
        await self.consume()
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.sent()[0]["message"][0]["type"], "text")

    async def test_cover_download_failure_keeps_enabled_thumbnail(self):
        self.config.update(isSendCover=True, isThumbs=True, isSendVideo=False)
        self.event.bot = FakeBot(
            failures=("https://cdn.example/cover.jpg", "https://mirror.example/cover.jpg")
        )
        await self.consume()
        message = self.sent()[0]["message"]
        self.assertEqual(message[0]["data"]["file"], "file:///napcat/temp/thumb.jpg")
        self.assertEqual([s["type"] for s in message], ["image", "text"])

    async def test_thumbnail_download_failure_keeps_enabled_cover(self):
        self.config.update(isSendCover=True, isThumbs=True, isSendVideo=False)
        self.event.bot = FakeBot(failures=("https://cdn.example/thumb.jpg",))
        await self.consume()
        message = self.sent()[0]["message"]
        self.assertEqual(message[0]["data"]["file"], "file:///napcat/temp/cover.jpg")
        self.assertEqual([s["type"] for s in message], ["image", "text"])

    async def test_preview_switches_do_not_control_video_sending(self):
        self.config.update(isSendCover=True, isThumbs=True)
        await self.consume()
        self.assertEqual(
            [p["url"] for p in self.downloads()],
            [
                "https://cdn.example/cover.jpg",
                "https://cdn.example/thumb.jpg",
                "https://cdn.example/video.mp4",
            ],
        )
        self.assertEqual(self.sent()[1]["message"][0]["type"], "video")

    async def test_preview_switches_do_not_add_album_metadata_images(self):
        self.config.update(isSendCover=True, isThumbs=True, isSendVideo=False)
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))
        await self.consume()
        self.assertEqual(self.downloads(), [])
        self.assertEqual([s["type"] for s in self.sent()[0]["message"]], ["text"])

    async def test_preview_switches_do_not_change_album_media_sending(self):
        self.config.update(isSendCover=True, isThumbs=True)
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))
        await self.consume()
        self.assertEqual(len(self.downloads()), 2)
        self.assertEqual(self.sent(), [])
        self.assertEqual(len(self.sent("send_group_forward_msg")[0]["messages"]), 3)

    async def test_enabled_previews_skip_only_the_missing_asset(self):
        self.config.update(isSendCover=True, isThumbs=True, isSendVideo=False)
        for missing, expected in (
            ("thumbnail", "https://cdn.example/cover.jpg"),
            ("cover", "https://cdn.example/thumb.jpg"),
        ):
            with self.subTest(missing=missing):
                data = fixture("video")
                if missing == "thumbnail":
                    data["raw"] = None
                else:
                    data["media"]["covers"] = []
                self.plugin._dtk.parse_url.return_value = parse_post(data)
                self.event = FakeEvent(URL)
                await self.consume()
                self.assertEqual([p["url"] for p in self.downloads()], [expected])

    async def test_preview_upload_failure_keeps_text_and_video_sending(self):
        self.config.update(isSendCover=True, isThumbs=True)

        class PreviewUploadBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "send_group_msg" and payload["message"][0]["type"] == "image":
                    bot.calls.append((action, payload))
                    raise ActionFailed("highway upload transport failed: TCP connect timeout")
                return await super().call_action(action, **payload)

        self.event.bot = PreviewUploadBot()
        self.assertEqual(await self.consume(), [])
        self.assertEqual(len(self.sent()), 3)
        self.assertEqual([s["type"] for s in self.sent()[0]["message"]], ["image", "image", "text"])
        self.assertEqual([s["type"] for s in self.sent()[1]["message"]], ["text"])
        self.assertEqual(self.sent()[2]["message"][0]["type"], "video")
        self.assertEqual(len(self.downloads()), 3)

    async def test_video_limits_prevent_download(self):
        self.config["videoMaxDuration"] = 60
        results = await self.consume()
        self.assertIn("超过限制", results[0])
        self.assertNotIn("媒体直链", results[0])
        self.assertEqual(self.downloads(), [])
        self.assertEqual(len(self.sent()), 1)

    async def test_unknown_size_with_limit_is_skipped(self):
        data = fixture("video")
        data["media"]["video"]["size_bytes"] = None
        self.plugin._dtk.parse_url.return_value = parse_post(data)
        self.config["videoMaxSize"] = 50
        results = await self.consume()
        self.assertIn("未获取到视频大小", results[0])
        self.assertEqual(self.downloads(), [])

    async def test_missing_video_url_is_reported(self):
        data = fixture("video")
        data["media"]["video"] = None
        self.plugin._dtk.parse_url.return_value = parse_post(data)
        results = await self.consume()
        self.assertIn("视频地址", results[0])

    async def test_single_static_image_is_sent(self):
        data = fixture("album")
        data["media"]["images"] = data["media"]["images"][:1]
        self.plugin._dtk.parse_url.return_value = parse_post(data)
        await self.consume()
        messages = self.sent()
        self.assertEqual(len(messages), 1)
        self.assertEqual([s["type"] for s in messages[0]["message"]], ["image", "text"])

    async def test_album_metadata_is_not_silently_dropped_when_disabled(self):
        self.config["isSendVideo"] = False
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))
        await self.consume()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.sent()[0]["message"][0]["type"], "text")
        self.assertEqual(self.downloads(), [])

    async def test_multi_image_album_order_and_live_photo(self):
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))
        await self.consume()
        nodes = self.sent("send_group_forward_msg")[0]["messages"]
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0]["data"]["content"][0]["type"], "text")
        self.assertEqual(nodes[1]["data"]["content"][0]["type"], "image")
        self.assertEqual(nodes[2]["data"]["content"][0]["type"], "video")
        self.assertEqual(
            nodes[2]["data"]["content"][0]["data"]["file"], "file:///napcat/temp/live.mp4"
        )
        self.assertEqual(self.sent(), [])

    async def test_live_photo_limit_falls_back_to_static_image(self):
        self.config["videoMaxDuration"] = 1
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))
        await self.consume()
        node = self.sent("send_group_forward_msg")[0]["messages"][2]
        self.assertEqual(node["data"]["content"][0]["type"], "image")
        self.assertIn("超过限制", node["data"]["content"][1]["data"]["text"])
        self.assertNotIn("https://cdn.example/live.mp4", [p["url"] for p in self.downloads()])

    async def test_failed_live_photo_falls_back_to_static_image(self):
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))
        self.event.bot = FakeBot(failures=("https://cdn.example/live.mp4",))
        await self.consume()
        node = self.sent("send_group_forward_msg")[0]["messages"][2]
        self.assertEqual(node["data"]["content"][0]["type"], "image")
        self.assertIn("改发静态", node["data"]["content"][1]["data"]["text"])

    async def test_album_partial_download_failure_keeps_other_images(self):
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))
        self.event.bot = FakeBot(
            failures=("https://cdn.example/1.jpg", "https://mirror.example/1.jpg")
        )
        await self.consume()
        nodes = self.sent("send_group_forward_msg")[0]["messages"]
        self.assertIn("图片下载失败", nodes[1]["data"]["content"][0]["data"]["text"])
        self.assertEqual(nodes[2]["data"]["content"][0]["type"], "video")

    async def test_cdn_mirror_fallback(self):
        self.event.bot = FakeBot(failures=("https://cdn.example/video.mp4",), nested=True)
        await self.consume()
        self.assertEqual(
            [p["url"] for p in self.downloads()],
            [
                "https://cdn.example/video.mp4",
                "https://mirror.example/video.mp4",
            ],
        )
        self.assertEqual(len(self.sent()), 2)

    async def test_download_failure_produces_safe_reply(self):
        self.event.bot = FakeBot(
            failures=("https://cdn.example/video.mp4", "https://mirror.example/video.mp4")
        )
        results = await self.consume()
        self.assertIn("媒体下载失败", results[0])
        self.assertNotIn("secret", results[0])
        self.assertTrue(self.event.stopped)

    async def test_api_error_produces_safe_reply_and_stops_event(self):
        self.plugin._dtk.parse_url.side_effect = DTKError("UNAUTHENTICATED")
        results = await self.consume()
        self.assertIn("API Key", results[0])
        self.assertEqual(self.event.bot.calls, [])
        self.assertTrue(self.event.stopped)

    async def test_group_and_private_video_sending(self):
        self.event.group_id = ""
        await self.consume()
        messages = self.sent("send_private_msg")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["user_id"], 456)

    async def test_tiktok_private_album(self):
        data = fixture("album")
        data["platform"] = "tiktok"
        self.plugin._dtk.parse_url.return_value = parse_post(data)
        self.event.group_id = ""
        await self.consume()
        forward = self.sent("send_private_forward_msg")[0]
        self.assertEqual(forward["user_id"], 456)
        self.assertEqual(forward["messages"][0]["data"]["nickname"], "TikTok图文")
        self.assertTrue(
            all(p["headers"][0] == "Referer=https://www.tiktok.com/" for p in self.downloads())
        )

    async def test_missing_album_images_reports_reason(self):
        data = fixture("album")
        data["media"]["images"] = []
        self.plugin._dtk.parse_url.return_value = parse_post(data)
        await self.consume()
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("未获取到图文", self.sent()[1]["message"][0]["data"]["text"])

    async def test_non_links_and_unsupported_platform_do_not_call_dtk(self):
        self.assertEqual(await self.consume(FakeEvent("hello")), [])
        unsupported = type("OtherPlatformEvent", (), {"message_str": URL})()
        self.assertEqual(await self.consume(unsupported), [])
        self.plugin._dtk.parse_url.assert_not_awaited()

    async def test_download_concurrency_is_shared(self):
        self.plugin._downloads = asyncio.Semaphore(2)
        active = 0
        peak = 0

        class SlowBot(FakeBot):
            async def call_action(bot, action, **payload):
                nonlocal active, peak
                if action != "download_file":
                    return await super().call_action(action, **payload)
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.01)
                    return await super().call_action(action, **payload)
                finally:
                    active -= 1

        self.event.bot = SlowBot()
        assets = [MediaAsset((f"https://cdn.example/{index}.jpg",)) for index in range(8)]
        await asyncio.gather(
            *(self.plugin._download_asset(self.event, asset, "douyin") for asset in assets)
        )
        self.assertEqual(peak, 2)

    async def test_media_timeout_is_bounded(self):
        class HangingBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "download_file":
                    await asyncio.sleep(10)
                return await super().call_action(action, **payload)

        self.event.bot = HangingBot()
        self.config["mediaDownloadTimeout"] = 1
        with self.assertRaises(main.MediaDownloadError):
            await self.plugin._download_asset(
                self.event, MediaAsset(("https://cdn.example/video.mp4",)), "douyin"
            )

    async def test_semaphore_wait_does_not_consume_media_timeout(self):
        self.plugin._downloads = asyncio.Semaphore(1)
        self.config["mediaDownloadTimeout"] = 1
        await self.plugin._downloads.acquire()
        task = asyncio.create_task(
            self.plugin._download_asset(
                self.event, MediaAsset(("https://cdn.example/video.mp4",)), "douyin"
            )
        )
        try:
            await asyncio.sleep(1.05)
            self.assertFalse(task.done())
        finally:
            self.plugin._downloads.release()
        self.assertEqual(await task, "file:///napcat/temp/video.mp4")

    async def test_qq_send_failure_is_reported_without_diagnostic_leak(self):
        class FailingBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "send_group_msg":
                    raise RuntimeError("secret path or key")
                return await super().call_action(action, **payload)

        self.event.bot = FailingBot()
        results = await self.consume()
        self.assertIn("媒体发送失败", results[0])
        self.assertNotIn("secret", results[0])
        self.assertTrue(self.event.stopped)

    async def test_snowluma_cdn_404_tries_next_mirror(self):
        class Cdn404Bot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "download_file" and payload["url"] == "https://cdn.example/video.mp4":
                    bot.calls.append((action, payload))
                    raise ActionFailed("HTTP 404 Not Found", 1200)
                return await super().call_action(action, **payload)

        self.event.bot = Cdn404Bot()
        self.assertEqual(await self.consume(), [])
        self.assertEqual(len(self.downloads()), 2)
        self.assertEqual(self.downloads()[1]["url"], "https://mirror.example/video.mp4")

    async def test_download_api_timeout_does_not_enqueue_other_mirrors(self):
        class TimingError(Exception):
            pass

        class DownloadTimeoutBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "download_file":
                    bot.calls.append((action, payload))
                    raise TimingError("API timed out")
                return await super().call_action(action, **payload)

        self.event.bot = DownloadTimeoutBot()
        results = await self.consume()
        self.assertIn("下载响应超时", results[0])
        self.assertEqual(len(self.downloads()), 1)
        self.assertEqual(len(self.sent()), 1)

    async def test_snowluma_download_file_contract(self):
        class SnowLumaBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "download_file":
                    bot.calls.append((action, payload))
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"file": "/app/data/downloads/snow video.mp4"},
                    }
                return await super().call_action(action, **payload)

        self.event.bot = SnowLumaBot()
        await self.consume()
        self.assertEqual(
            self.sent()[1]["message"][0]["data"]["file"],
            "file:///app/data/downloads/snow%20video.mp4",
        )
        self.assertIn("Referer=https://www.douyin.com/", self.downloads()[0]["headers"])

    async def test_highway_failure_has_stage_diagnostic_and_link_without_video_retry(self):
        class HighwayBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "send_group_msg" and payload["message"][0]["type"] == "video":
                    bot.calls.append((action, payload))
                    raise ActionFailed(
                        "highway upload transport failed after 3 attempts "
                        "(cmdId=1005 chunk=1048576/39116665 offset=0): Error: TCP connect timeout"
                    )
                return await super().call_action(action, **payload)

        self.event.bot = HighwayBot()
        with self.assertLogs("douyin-parser-tests", level="WARNING") as logs:
            results = await self.consume()
        self.assertIn("已下载", results[0])
        self.assertIn("QQ Highway", results[0])
        self.assertIn("https://cdn.example/video.mp4", results[0])
        self.assertIn("作品链接", results[0])
        self.assertNotIn("NapCat", results[0])
        self.assertIn("action=send_group_msg", logs.output[0])
        self.assertIn("reason=HIGHWAY_CONNECT_TIMEOUT", logs.output[0])
        self.assertIn("retcode=1200", logs.output[0])
        self.assertEqual(len(self.downloads()), 1)
        self.assertEqual(len(self.sent()), 2)
        self.assertTrue(self.event.stopped)

    async def test_failure_links_can_be_disabled(self):
        self.config["sendLinkOnFailure"] = False
        self.event.bot = FakeBot(
            failures=("https://cdn.example/video.mp4", "https://mirror.example/video.mp4")
        )
        results = await self.consume()
        self.assertIn("媒体下载失败", results[0])
        self.assertNotIn("https://", results[0])

    async def test_snowluma_download_failure_detail_is_sanitized_in_logs(self):
        class BrokenDownloadBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "download_file":
                    raise ActionFailed(
                        "CDN 403 https://cdn.example/video.mp4?sign=private Cookie=private-cookie"
                    )
                return await super().call_action(action, **payload)

        self.event.bot = BrokenDownloadBot()
        with self.assertLogs("douyin-parser-tests", level="WARNING") as logs:
            results = await self.consume()
        self.assertIn("媒体下载失败", results[0])
        self.assertIn("CDN 403", logs.output[0])
        self.assertNotIn("private", logs.output[0])
        self.assertNotIn("cdn.example", logs.output[0])

    async def test_cover_highway_failure_falls_back_to_text(self):
        self.config.update(isSendCover=True, isSendVideo=False)

        class ImageUploadBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "send_group_msg" and payload["message"][0]["type"] == "image":
                    bot.calls.append((action, payload))
                    raise ActionFailed("highway upload transport failed: TCP connect timeout")
                return await super().call_action(action, **payload)

        self.event.bot = ImageUploadBot()
        await self.consume()
        self.assertEqual([segment["type"] for segment in self.sent()[1]["message"]], ["text"])
        self.assertEqual(len(self.downloads()), 1)

    async def test_single_live_photo_upload_failure_falls_back_once(self):
        data = fixture("album")
        data["media"]["images"] = data["media"]["images"][1:]
        data["raw"]["images"] = data["raw"]["images"][1:]
        self.plugin._dtk.parse_url.return_value = parse_post(data)

        class LiveUploadBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "send_group_msg" and payload["message"][0]["type"] == "video":
                    bot.calls.append((action, payload))
                    raise ActionFailed("highway upload transport failed: TCP connect timeout")
                return await super().call_action(action, **payload)

        self.event.bot = LiveUploadBot()
        self.assertEqual(await self.consume(), [])
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.sent()[1]["message"][0]["type"], "image")
        self.assertIn("QQ 上传失败", self.sent()[1]["message"][1]["data"]["text"])
        self.assertEqual(
            [p["url"] for p in self.downloads()],
            ["https://cdn.example/live.mp4", "https://cdn.example/2.jpg"],
        )

    async def test_forward_live_video_upload_failure_reuses_static_nodes(self):
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))

        class ForwardUploadBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "send_group_forward_msg":
                    bot.calls.append((action, payload))
                    if any(
                        segment["type"] == "video"
                        for node in payload["messages"]
                        for segment in node["data"]["content"]
                    ):
                        raise ActionFailed("highway upload transport failed: TCP connect timeout")
                    return {}
                return await super().call_action(action, **payload)

        self.event.bot = ForwardUploadBot()
        self.assertEqual(await self.consume(), [])
        forwards = self.sent("send_group_forward_msg")
        self.assertEqual(len(forwards), 2)
        self.assertEqual(forwards[0]["messages"][1], forwards[1]["messages"][1])
        self.assertEqual(forwards[1]["messages"][2]["data"]["content"][0]["type"], "image")
        self.assertEqual(
            [p["url"] for p in self.downloads()].count("https://cdn.example/live.mp4"), 1
        )

    async def test_forward_timeout_does_not_trigger_duplicate_album(self):
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))

        class TimingError(Exception):
            pass

        class TimeoutBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "send_group_forward_msg":
                    bot.calls.append((action, payload))
                    raise TimingError("awaiting API result timed out")
                return await super().call_action(action, **payload)

        self.event.bot = TimeoutBot()
        results = await self.consume()
        self.assertIn("尚不确定", results[0])
        self.assertEqual(len(self.sent("send_group_forward_msg")), 1)
        self.assertNotIn("https://cdn.example/2.jpg", [p["url"] for p in self.downloads()])

    async def test_failed_static_forward_is_not_retried_again(self):
        self.plugin._dtk.parse_url.return_value = parse_post(fixture("album"))

        class AllUploadsFailBot(FakeBot):
            async def call_action(bot, action, **payload):
                if action == "send_group_forward_msg":
                    bot.calls.append((action, payload))
                    raise ActionFailed("highway upload transport failed: TCP connect timeout")
                return await super().call_action(action, **payload)

        self.event.bot = AllUploadsFailBot()
        results = await self.consume()
        self.assertIn("QQ Highway", results[0])
        self.assertEqual(len(self.sent("send_group_forward_msg")), 2)

    async def test_full_mocked_rest_to_qq_action_pipeline(self):
        async def handler(request):
            if request.method == "POST":
                return response({"task_id": TASK_ID, "state": "queued"}, 202)
            return response({"state": "done", "data": fixture("video")})

        await self.plugin._dtk.aclose()
        self.plugin._dtk = main.DTKClient(
            "http://dtk",
            "dtk_test_secret",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(self.plugin._dtk._http.aclose)
        await self.consume()
        self.assertEqual(len(self.sent()), 2)
