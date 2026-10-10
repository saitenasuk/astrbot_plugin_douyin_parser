"""Minimal AstrBot doubles: these tests do not exercise a running bot/QQ stack."""

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import dtk_client
import onebot_media

ROOT = Path(__file__).resolve().parents[1]


def fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / f"{name}.json").read_text())


class FakeEvent:
    def __init__(self, text="", *, bot=None, group_id="123", sender_id="456"):
        self.message_str = text
        self.bot = bot or FakeBot()
        self.group_id = group_id
        self.sender_id = sender_id
        self.stopped = False

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id

    def get_self_id(self):
        return "789"

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class FakeBot:
    def __init__(self, *, failures=(), nested=False):
        self.calls = []
        self.failures = failures
        self.nested = nested

    async def call_action(self, action, **payload):
        self.calls.append((action, payload))
        if action == "download_file":
            if payload["url"] in self.failures:
                raise RuntimeError("secret URL/path must not be echoed")
            filename = payload["url"].rsplit("/", 1)[-1]
            result = {"file": f"/napcat/temp/{filename}"}
            return {"data": result} if self.nested else result
        return {}


class FakeStar:
    def __init__(self, context):
        self.context = context


def load_plugin():
    package_name = "_test_douyin_parser"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    sys.modules[f"{package_name}.dtk_client"] = dtk_client
    sys.modules[f"{package_name}.onebot_media"] = onebot_media
    stubs = {}
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.platform",
        "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.aiocqhttp",
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
    ):
        stubs[name] = ModuleType(name)
    stubs["astrbot.api"].AstrBotConfig = dict
    stubs["astrbot.api"].logger = logging.getLogger("douyin-parser-tests")
    stubs["astrbot.api.event"].AstrMessageEvent = FakeEvent
    stubs["astrbot.api.event"].filter = SimpleNamespace(
        EventMessageType=SimpleNamespace(ALL="all"),
        event_message_type=lambda _: lambda function: function,
    )
    stubs["astrbot.api.star"].Context = object
    stubs["astrbot.api.star"].Star = FakeStar
    stubs[
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    ].AiocqhttpMessageEvent = FakeEvent
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        name = f"{package_name}.main"
        spec = importlib.util.spec_from_file_location(name, ROOT / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class ActionFailed(Exception):
    """Matches aiocqhttp.ActionFailed.result, including SnowLuma wording."""

    def __init__(self, wording, retcode=1200):
        self.result = {"status": "failed", "retcode": retcode, "data": None, "wording": wording}
        super().__init__(self.result)
