"""推送通道：ntfy（主）与邮件（可选兜底）。

ntfy 协议简单：POST https://ntfy.sh/ 携带 JSON 即可，支持中文、
click（点击通知跳转）、tags（图标）、priority。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class Ntfy:
    def __init__(self, topic: str, server: str = "https://ntfy.sh") -> None:
        self.topic = topic
        self.server = server.rstrip("/")

    def send(
        self,
        title: str,
        message: str,
        click: str | None = None,
        tags: list[str] | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "topic": self.topic,
            "title": title,
            "message": message,
        }
        if click:
            payload["click"] = click
        if tags:
            payload["tags"] = tags
        if priority:
            payload["priority"] = priority

        req = urllib.request.Request(
            f"{self.server}/",
            # 必须显式 UTF-8，否则中文会乱码
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"ntfy 推送失败 HTTP {e.code}: {e.read()[:200]!r}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"ntfy 网络不可达: {e.reason}") from e


def from_env() -> Ntfy | None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return None
    return Ntfy(topic, os.environ.get("NTFY_SERVER", "https://ntfy.sh"))
