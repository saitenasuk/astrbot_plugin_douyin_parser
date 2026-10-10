import asyncio
import unittest
from unittest.mock import AsyncMock

from onebot_media import MediaDownloadError, OneBotActionError, call_action, sanitize_detail
from tests.support import ActionFailed

HIGHWAY_FAILURE = (
    "highway upload transport failed after 3 attempts "
    "(cmdId=1005 chunk=1048576/39116665 offset=0): Error: TCP connect timeout"
)


class DiagnosticTests(unittest.TestCase):
    def test_aiocqhttp_action_failed_wording_and_retcode(self):
        exc = ActionFailed(HIGHWAY_FAILURE)
        error = OneBotActionError.from_failure("send_group_msg", exc.result, exc)
        self.assertEqual(error.reason, "HIGHWAY_CONNECT_TIMEOUT")
        self.assertEqual(error.retcode, 1200)
        self.assertTrue(error.upload_failed)
        self.assertIn("TCP connect timeout", error.detail)
        self.assertIn("cmdId=1005 chunk=1048576/39116665 offset=0", error.diagnostic())
        self.assertIn("已下载", error.user_message())
        self.assertIn("QQ Highway", error.user_message())
        self.assertNotIn("NapCat", error.user_message())

    def test_other_highway_failures_are_still_upload_errors(self):
        exc = ActionFailed("highway upload error_code=102902 cmdId=1005")
        error = OneBotActionError.from_failure("send_group_msg", exc.result, exc)
        self.assertEqual(error.reason, "HIGHWAY_UPLOAD_FAILED")
        self.assertTrue(error.upload_failed)

    def test_download_timeout_is_not_misclassified_as_upload(self):
        exc = ActionFailed("TCP connect timeout")
        error = OneBotActionError.from_failure("download_file", exc.result, exc)
        self.assertEqual(error.reason, "DOWNLOAD_ERROR")
        self.assertFalse(error.upload_failed)
        self.assertIn("媒体下载失败", error.user_message())

    def test_bridge_timeout_is_not_confirmed_failure(self):
        class TimingError(Exception):
            pass

        exc = TimingError("awaiting API result timed out")
        error = OneBotActionError.from_failure("send_group_msg", exception=exc)
        self.assertEqual(error.reason, "ONEBOT_TIMEOUT")
        self.assertFalse(error.upload_failed)
        self.assertIn("尚不确定", error.user_message())

    def test_cdn_not_found_is_not_unknown_onebot_action(self):
        exc = ActionFailed("HTTP 404 Not Found", 1200)
        error = OneBotActionError.from_failure("download_file", exc.result, exc)
        self.assertEqual(error.reason, "DOWNLOAD_ERROR")
        error = OneBotActionError.from_failure(
            "download_file",
            {
                "status": "failed",
                "retcode": 1404,
                "wording": "",
            },
        )
        self.assertEqual(error.reason, "DOWNLOAD_UNSUPPORTED")

    def test_download_response_timeout_does_not_claim_media_was_sent(self):
        class TimingError(Exception):
            pass

        error = OneBotActionError.from_failure("download_file", exception=TimingError())
        self.assertIn("下载响应超时", error.user_message())
        self.assertNotIn("聊天", error.user_message())

    def test_diagnostic_masks_urls_keys_headers_and_paths(self):
        samples = (
            ("upload https://cdn.example/video.mp4?sign=secret", "cdn.example"),
            ("upload file:///app/data/downloads/private.mp4", "/app/data"),
            ("dtk_0123456789ab_secretkey", "secretkey"),
            ("Authorization: Bearer dtk_0123456789ab_token", "token"),
            ("Cookie=private-cookie; SESSION=private-session", "private-cookie"),
            (r"C:\Users\private-user\video.mp4 failed", "private-user"),
            ("file /app/private-user/video.mp4 failed", "private-user"),
            ("sig_session=secret-session", "secret-session"),
            ("failed at '/app/private user/video file.mp4'", "private user"),
            (r'failed at "C:\Users\private user\video.mp4"', "private user"),
        )
        for text, secret in samples:
            with self.subTest(text=text):
                self.assertNotIn(secret, sanitize_detail(text))
        self.assertEqual(sanitize_detail("first line\npretend log entry"), "first line")
        self.assertLessEqual(len(sanitize_detail("x" * 2000)), 600)

    def test_raw_server_prose_never_appears_in_qq_error(self):
        error = OneBotActionError.from_failure(
            "send_group_msg",
            {
                "status": "failed",
                "retcode": 100,
                "wording": "private account information",
            },
        )
        self.assertNotIn("private account", error.user_message())
        self.assertIn("retcode=100", error.user_message())

    def test_response_without_wording_does_not_dump_data_fields(self):
        exc = ActionFailed("")
        exc.result["data"] = {"unrecognized_sensitive_field": "private-value"}
        error = OneBotActionError.from_failure("send_group_msg", exc.result, exc)
        self.assertNotIn("private-value", error.diagnostic())
        self.assertIn("no wording", error.detail)

    def test_download_error_preserves_last_mirror_diagnostic(self):
        last = OneBotActionError("download_file", "DOWNLOAD_ERROR", retcode=1200, detail="CDN 403")
        error = MediaDownloadError(last_error=last)
        self.assertEqual(error.retcode, 1200)
        self.assertEqual(error.detail, "CDN 403")
        self.assertIn("CDN", error.user_message())


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wraps_action_failed_using_result_not_repr(self):
        bot = type("Bot", (), {})()
        bot.call_action = AsyncMock(side_effect=ActionFailed(HIGHWAY_FAILURE))
        with self.assertRaises(OneBotActionError) as raised:
            await call_action(bot, "send_group_msg", message=[])
        self.assertEqual(raised.exception.reason, "HIGHWAY_CONNECT_TIMEOUT")
        bot.call_action.assert_awaited_once_with("send_group_msg", message=[])

    async def test_failed_response_returned_instead_of_raised(self):
        bot = type("Bot", (), {})()
        bot.call_action = AsyncMock(
            return_value={
                "status": "failed",
                "retcode": 1200,
                "wording": HIGHWAY_FAILURE,
                "data": None,
            }
        )
        with self.assertRaises(OneBotActionError) as raised:
            await call_action(bot, "send_private_msg", message=[])
        self.assertTrue(raised.exception.upload_failed)

    async def test_success_data_and_response_envelopes_unchanged(self):
        for result in (
            {"file": "/app/data/downloads/video.mp4"},
            {"status": "ok", "retcode": 0, "data": {"message_id": 1}},
        ):
            bot = type("Bot", (), {})()
            bot.call_action = AsyncMock(return_value=result)
            self.assertIs(await call_action(bot, "download_file"), result)

    async def test_cancel_is_not_converted_to_retryable_download_error(self):
        bot = type("Bot", (), {})()
        bot.call_action = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await call_action(bot, "download_file")

    async def test_unknown_download_action_is_reported(self):
        bot = type("Bot", (), {})()
        bot.call_action = AsyncMock(side_effect=ActionFailed("unknown action download_file", 1404))
        with self.assertRaises(OneBotActionError) as raised:
            await call_action(bot, "download_file")
        self.assertEqual(raised.exception.reason, "DOWNLOAD_UNSUPPORTED")

    async def test_alternate_adapter_wording_attributes(self):
        class OtherAdapterError(Exception):
            wording = HIGHWAY_FAILURE
            retcode = 1200

        bot = type("Bot", (), {})()
        bot.call_action = AsyncMock(side_effect=OtherAdapterError())
        with self.assertRaises(OneBotActionError) as raised:
            await call_action(bot, "send_group_msg")
        self.assertEqual(raised.exception.reason, "HIGHWAY_CONNECT_TIMEOUT")
        self.assertEqual(raised.exception.retcode, 1200)
