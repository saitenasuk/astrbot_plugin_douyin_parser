import asyncio
import json
import unittest

import httpx

from dtk_client import DTKClient, DTKError, normalize_base_url, parse_post
from tests.support import fixture

TASK_ID = "c0eb0fc8-522e-40f2-b22f-8a662accc5f7"
URL = "https://www.douyin.com/video/7660875690212492466"


def response(data, status=200, *, success=True, error=None, request_id="request-1"):
    return httpx.Response(
        status,
        json={
            "success": success,
            "data": data,
            "error": error,
            "meta": {"request_id": request_id},
        },
    )


class ModelTests(unittest.TestCase):
    def test_normalized_video(self):
        post = parse_post(fixture("video"))
        self.assertEqual(post.content_id, "7660875690212492466")
        self.assertEqual(post.duration_ms, 62034)
        self.assertEqual(post.video.size_bytes, 14784469)
        self.assertEqual(
            post.video.urls,
            (
                "https://cdn.example/video.mp4",
                "https://mirror.example/video.mp4",
            ),
        )
        self.assertIsNone(post.stats["collect_count"])
        self.assertEqual(post.stats["comment_count"], 0)
        self.assertEqual(post.cover().urls[0], "https://cdn.example/cover.jpg")
        self.assertEqual(post.thumbnail.urls[0], "https://cdn.example/thumb.jpg")

    def test_cover_and_thumbnail_are_independent_assets(self):
        data = fixture("video")
        data["media"]["covers"] = []
        post = parse_post(data)
        self.assertIsNone(post.cover())
        self.assertEqual(post.thumbnail.urls[0], "https://cdn.example/thumb.jpg")

        data = fixture("video")
        data["raw"] = None
        post = parse_post(data)
        self.assertEqual(post.cover().urls[0], "https://cdn.example/cover.jpg")
        self.assertIsNone(post.thumbnail)

    def test_normalized_tiktok_uses_same_model(self):
        data = fixture("video")
        data["platform"] = "tiktok"
        data["stats"] = {"digg_count": 0}
        data["raw"] = None
        post = parse_post(data)
        self.assertEqual(post.platform, "tiktok")
        self.assertEqual(post.stats["digg_count"], 0)
        self.assertIsNone(post.stats["share_count"])

    def test_album_live_photo_supplement(self):
        post = parse_post(fixture("album"))
        self.assertEqual(post.kind, "image_album")
        self.assertEqual(len(post.images), 2)
        self.assertIsNone(post.images[0].motion)
        self.assertEqual(post.images[1].motion.urls, ("https://cdn.example/live.mp4",))
        self.assertEqual(post.images[1].motion.duration_ms, 3000)
        self.assertEqual(post.images[1].motion.size_bytes, 1024)
        self.assertEqual(post.cover().urls[0], "https://cdn.example/1.jpg")

    def test_live_photo_download_address_fallback(self):
        data = fixture("album")
        video = data["raw"]["images"][1]["video"]
        video.pop("play_addr")
        self.assertEqual(
            parse_post(data).images[1].motion.urls,
            ("https://cdn.example/live-watermark.mp4",),
        )

    def test_missing_raw_does_not_break_album(self):
        for raw in (None, {}, {"images": "malformed", "video": {"big_thumbs": 1}}):
            with self.subTest(raw=raw):
                data = fixture("album")
                data["raw"] = raw
                post = parse_post(data)
                self.assertEqual(len(post.images), 2)
                self.assertIsNone(post.images[1].motion)

    def test_only_valid_http_media_urls_are_used(self):
        data = fixture("video")
        data["media"]["video"].update(
            {
                "url": "file:///etc/passwd",
                "urls": ["javascript:alert(1)", "https://cdn.example/good.mp4", None],
            }
        )
        self.assertEqual(parse_post(data).video.urls, ("https://cdn.example/good.mp4",))

    def test_never_choose_watermarked_video_fallback(self):
        data = fixture("video")
        data["media"]["video"] = None
        self.assertIsNone(parse_post(data).video)
        data["media"]["streams"].append({"url": "https://cdn.example/clean.mp4"})
        self.assertEqual(parse_post(data).video.urls, ("https://cdn.example/clean.mp4",))

    def test_null_optional_arrays(self):
        data = fixture("video")
        data["media"].update(covers=None, streams=None, images=None)
        self.assertEqual(parse_post(data).covers, ())

    def test_malformed_media_arrays(self):
        for key in ("covers", "images", "streams"):
            with self.subTest(key=key):
                data = fixture("video")
                data["media"][key] = 1
                with self.assertRaises(DTKError) as raised:
                    parse_post(data)
                self.assertEqual(raised.exception.code, "INVALID_RESPONSE")

    def test_metrics_missing_zero_invalid_and_numeric_string(self):
        data = fixture("video")
        data["stats"] = {
            "digg_count": "1234",
            "comment_count": 0,
            "collect_count": -1,
            "share_count": True,
        }
        post = parse_post(data)
        self.assertEqual(
            post.stats,
            {
                "digg_count": 1234,
                "comment_count": 0,
                "collect_count": None,
                "share_count": None,
            },
        )

    def test_private_deleted_live_author_and_invalid_ids(self):
        for updates, code in (
            ({"is_private": True}, "CONTENT_PRIVATE"),
            ({"is_deleted": True}, "NOT_FOUND"),
            ({"kind": "live"}, "UNSUPPORTED_CONTENT"),
            ({"content_id": 7660875690212492466}, "INVALID_RESPONSE"),
            ({"media": None}, "INVALID_RESPONSE"),
        ):
            with self.subTest(updates=updates):
                data = fixture("video")
                data.update(updates)
                with self.assertRaises(DTKError) as raised:
                    parse_post(data)
                self.assertEqual(raised.exception.code, code)
        with self.assertRaises(DTKError) as raised:
            parse_post({"uid": "author", "nickname": "test"})
        self.assertEqual(raised.exception.code, "UNSUPPORTED_CONTENT")

    def test_normalize_base_url_with_proxy_prefix(self):
        self.assertEqual(normalize_base_url(" http://dtk:8000/ "), "http://dtk:8000")
        self.assertEqual(normalize_base_url("https://host/dtk/api/v1/"), "https://host/dtk")

    def test_invalid_base_urls(self):
        for value in (
            "",
            "dtk:8000",
            "file:///tmp/x",
            "http://host:invalid",
            "http://host:70000",
            "http://key@host",
            "https://host/?token=secret",
            "https://host/#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(DTKError):
                normalize_base_url(value)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, handler, **kwargs):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return DTKClient(
            kwargs.pop("base_url", "http://dtk:8000"),
            "dtk_test_secret",
            http_client=http,
            poll_interval=0.001,
            **kwargs,
        )

    async def test_submit_once_and_poll_done(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "POST":
                return response({"task_id": TASK_ID, "state": "queued"}, 202)
            if len(requests) == 2:
                return response({"task_id": TASK_ID, "state": "running"})
            return response({"task_id": TASK_ID, "state": "done", "data": fixture("video")})

        client = await self.make_client(handler, base_url="https://dtk/proxy/api/v1/")
        post = await client.parse_url(URL)
        self.assertEqual(post.content_id, "7660875690212492466")
        self.assertEqual([r.method for r in requests], ["POST", "GET", "GET"])
        self.assertEqual(requests[0].url.path, "/proxy/api/v1/parse")
        self.assertEqual(requests[1].url.path, f"/proxy/api/v1/tasks/{TASK_ID}")
        self.assertEqual(json.loads(requests[0].content), {"url": URL, "include_raw": True})
        self.assertNotIn("wait", requests[0].url.params)
        for request in requests:
            self.assertEqual(request.headers["Authorization"], "Bearer dtk_test_secret")
            self.assertEqual(request.url.params["lang"], "zh")
            self.assertNotIn("dtk_test_secret", str(request.url))

    async def test_direct_synchronous_result(self):
        client = await self.make_client(lambda _: response(fixture("video")))
        self.assertEqual((await client.parse_url(URL)).kind, "video")

    async def test_failed_task_inside_successful_http_envelope(self):
        def handler(request):
            if request.method == "POST":
                return response({"task_id": TASK_ID, "state": "queued"}, 202)
            return response(
                {
                    "state": "failed",
                    "error": {
                        "code": "IDENTITY_POOL_EXHAUSTED",
                        "message": "arbitrary private prose",
                        "retry_after": 30,
                    },
                }
            )

        client = await self.make_client(handler)
        with self.assertRaises(DTKError) as raised:
            await client.parse_url(URL)
        error = raised.exception
        self.assertEqual(error.code, "IDENTITY_POOL_EXHAUSTED")
        self.assertEqual(error.task_id, TASK_ID)
        self.assertEqual(error.request_id, "request-1")
        self.assertIn("30", error.user_message())
        self.assertNotIn("arbitrary", error.user_message())

    async def test_http_failure_envelopes(self):
        for code, status in (
            ("UNAUTHENTICATED", 401),
            ("FORBIDDEN_SCOPE", 403),
            ("RATE_LIMITED", 429),
            ("NOT_FOUND", 404),
        ):
            with self.subTest(code=code):
                client = await self.make_client(
                    lambda _, code=code, status=status: response(
                        None,
                        status,
                        success=False,
                        error={"code": code, "message": "secret"},
                    )
                )
                with self.assertRaises(DTKError) as raised:
                    await client.parse_url(URL)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("secret", raised.exception.user_message())

    async def test_wrong_version_html_and_malformed_response(self):
        bodies = (
            httpx.Response(200, text="<html>console</html>"),
            httpx.Response(200, json={"code": 200, "data": fixture("video")}),
            httpx.Response(200, json=[]),
            response(None),
            response({"task_id": "../../admin", "state": "queued"}, 202),
        )
        for body in bodies:
            with self.subTest(body=body):
                client = await self.make_client(lambda _, body=body: body)
                with self.assertRaises(DTKError) as raised:
                    await client.parse_url(URL)
                self.assertEqual(raised.exception.code, "INVALID_RESPONSE")

    async def test_redirect_is_not_followed(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(307, headers={"Location": "https://untrusted.example/"})

        client = await self.make_client(handler)
        with self.assertRaises(DTKError):
            await client.parse_url(URL)
        self.assertEqual(len(calls), 1)

    async def test_task_timeout_does_not_resubmit(self):
        calls = []

        def handler(request):
            calls.append(request)
            return response(
                {"task_id": TASK_ID, "state": "queued"}, 202 if request.method == "POST" else 200
            )

        client = await self.make_client(handler, task_timeout=0.015)
        with self.assertRaises(DTKError) as raised:
            await client.parse_url(URL)
        self.assertEqual(raised.exception.code, "TASK_TIMEOUT")
        self.assertEqual(raised.exception.task_id, TASK_ID)
        self.assertEqual(sum(r.method == "POST" for r in calls), 1)

    async def test_deadline_includes_slow_submission(self):
        async def handler(request):
            await asyncio.sleep(10)
            return response({"task_id": TASK_ID})

        client = await self.make_client(handler, task_timeout=0.01)
        with self.assertRaises(DTKError) as raised:
            await client.parse_url(URL)
        self.assertEqual(raised.exception.code, "TASK_TIMEOUT")
        self.assertIsNone(raised.exception.task_id)

    async def test_deadline_includes_slow_poll(self):
        async def handler(request):
            if request.method == "POST":
                return response({"task_id": TASK_ID}, 202)
            await asyncio.sleep(10)

        client = await self.make_client(handler, task_timeout=0.01)
        with self.assertRaises(DTKError) as raised:
            await client.parse_url(URL)
        self.assertEqual(raised.exception.code, "TASK_TIMEOUT")
        self.assertEqual(raised.exception.task_id, TASK_ID)

    async def test_httpx_timeout_and_connection_failure(self):
        for exception, expected in (
            (httpx.ReadTimeout, "HTTP_TIMEOUT"),
            (httpx.ConnectError, "CONNECTION_ERROR"),
        ):

            def handler(request, exception=exception):
                raise exception("private diagnostic", request=request)

            client = await self.make_client(handler)
            with self.assertRaises(DTKError) as raised:
                await client.parse_url(URL)
            self.assertEqual(raised.exception.code, expected)

    async def test_poll_failure_retains_task_id(self):
        def handler(request):
            if request.method == "POST":
                return response({"task_id": TASK_ID}, 202)
            return response(
                None, 429, success=False, error={"code": "RATE_LIMITED", "retry_after": 5}
            )

        client = await self.make_client(handler)
        with self.assertRaises(DTKError) as raised:
            await client.parse_url(URL)
        self.assertEqual(raised.exception.task_id, TASK_ID)
        self.assertEqual(raised.exception.retry_after, 5)

    async def test_unknown_task_state_fails_fast(self):
        def handler(request):
            return response({"task_id": TASK_ID, "state": "unknown"})

        client = await self.make_client(handler)
        with self.assertRaises(DTKError) as raised:
            await client.parse_url(URL)
        self.assertEqual(raised.exception.code, "INVALID_RESPONSE")

    async def test_anonymous_public_api_omits_auth_header(self):
        def handler(request):
            self.assertNotIn("Authorization", request.headers)
            return response(fixture("video"))

        client = await self.make_client(handler)
        client.api_key = ""
        await client.parse_url(URL)

    async def test_external_cancellation_propagates(self):
        started = asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.sleep(10)

        client = await self.make_client(handler)
        task = asyncio.create_task(client.parse_url(URL))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_close_only_owned_client(self):
        client = DTKClient("http://dtk")
        await client.aclose()
        self.assertTrue(client._http.is_closed)
        borrowed = await self.make_client(lambda _: response(fixture("video")))
        await borrowed.aclose()
        self.assertFalse(borrowed._http.is_closed)
