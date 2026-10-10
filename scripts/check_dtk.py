#!/usr/bin/env python3
"""Check the actual v5 REST client without importing AstrBot or sending to QQ."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dtk_client import DTKClient, DTKError  # noqa: E402


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


async def check(args: argparse.Namespace) -> int:
    client = DTKClient(
        args.base_url,
        os.environ.get("DTK_API_KEY", ""),
        request_timeout=args.api_timeout,
        task_timeout=args.task_timeout,
        poll_interval=args.poll_interval,
    )
    try:
        post = await client.parse_url(args.url)
        print(
            json.dumps(
                {
                    "platform": post.platform,
                    "content_id": post.content_id,
                    "kind": post.kind,
                    "author": post.author,
                    "description": post.description,
                    "duration_ms": post.duration_ms,
                    "stats": post.stats,
                    "has_cover": post.cover() is not None,
                    "has_thumbnail": post.thumbnail is not None,
                    "has_video": post.video is not None,
                    "video_size_bytes": post.video.size_bytes if post.video else None,
                    "image_count": len(post.images),
                    "live_photo_count": sum(item.motion is not None for item in post.images),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except DTKError as exc:
        print(
            json.dumps(
                {
                    "error": exc.code,
                    "message": exc.user_message(),
                    "request_id": exc.request_id,
                    "task_id": exc.task_id,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="作品链接或包含链接的完整分享文案")
    parser.add_argument(
        "--base-url", default=os.environ.get("DTK_BASE_URL"), help="DTK 地址，也可设置 DTK_BASE_URL"
    )
    parser.add_argument("--api-timeout", type=positive_int, default=30)
    parser.add_argument("--task-timeout", type=positive_int, default=120)
    parser.add_argument("--poll-interval", type=positive_int, default=2)
    args = parser.parse_args()
    if not args.base_url:
        parser.error("请通过 --base-url 或 DTK_BASE_URL 指定 DTK 地址")
    return asyncio.run(check(args))


if __name__ == "__main__":
    raise SystemExit(main())
