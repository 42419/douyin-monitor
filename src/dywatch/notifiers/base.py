"""投递骨架：渠道接口、失败隔离、重试上限、渠道之间的最小间隔。

三条纪律，都是"告警不能变成它正在报告的那个故障"的具体化：

* **单渠道失败隔离。** 一个渠道挂了，其它渠道照常收到——返回 `sent` / `failed` 两份名单，
  而不是抛异常让整轮通知中断。
* **重试最多 2 次、超时 8 秒。** 重试狠了，一次渠道故障就变成一次真实的中断。
* **渠道之间至少隔 `NOTIFY_GAP` 秒。** 一轮里连发十几条容易被渠道限流，
  而限流之后丢的是通知，不是请求。

另外，**每一次投递失败都会把去重窗口还回去**（见 `alerts.Deduplicator.release`）：
窗口是给"已送达的告警"用的，一条谁都没收到的消息不该占用它。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote

import httpx

from ..render import Message

#: 单次投递的超时。告警必须比它报告的事情快。
SEND_TIMEOUT_SECONDS = 8.0
#: 值得再试一次的状态码，其余都是对方在说"不"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 1.0

#: Bark 的 level 由严重级别决定：error 要能穿透免打扰
BARK_LEVELS: Mapping[str, str] = {"error": "timeSensitive", "warning": "active", "info": "passive"}


@dataclass(slots=True)
class Delivery:
    """Outcome of one `send`. Mirrors DTK's shape so the logs read the same."""

    sent: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.sent) and not self.failed

    @property
    def delivered(self) -> bool:
        return bool(self.sent)

    def as_dict(self) -> dict[str, Any]:
        return {"sent": list(self.sent), "failed": dict(self.failed)}


class Channel(Protocol):
    """A destination a message can be delivered to."""

    name: str

    def request(self, message: Message) -> tuple[str, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class HttpChannel:
    """Common POST-with-JSON behaviour. Subclasses only build the payload."""

    name: str

    def request(self, message: Message) -> tuple[str, dict[str, Any]]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def send(self, message: Message, client: httpx.AsyncClient) -> None:
        url, payload = self.request(message)
        response = await client.post(url, json=payload, timeout=SEND_TIMEOUT_SECONDS)
        if response.status_code in RETRYABLE_STATUS:
            raise RetryableDelivery(f"{self.name} returned {response.status_code}")
        if response.status_code >= 400:
            raise RuntimeError(f"{self.name} returned {response.status_code}")


class RetryableDelivery(RuntimeError):
    """A failure worth exactly one more attempt."""


def sign_dingtalk_url(url: str, secret: str, now: float) -> str:
    """Append DingTalk's `timestamp` and `sign` query parameters."""
    timestamp = str(int(now * 1000))
    digest = hmac.new(
        secret.encode("utf-8"), f"{timestamp}\n{secret}".encode(), hashlib.sha256
    ).digest()
    signature = quote(base64.b64encode(digest).decode("ascii"), safe="")
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}timestamp={timestamp}&sign={signature}"


class Notifier:
    """Broadcasts one message to every channel, isolating their failures."""

    def __init__(
        self,
        channels: Sequence[HttpChannel] = (),
        *,
        gap_seconds: float = 1.0,
        client: httpx.AsyncClient | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._channels = tuple(channels)
        self._gap = max(0.0, float(gap_seconds))
        self._owns = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._clock = clock
        self._last_sent_at: float | None = None
        self._gap_lock = asyncio.Lock()

    @property
    def channels(self) -> tuple[HttpChannel, ...]:
        return self._channels

    @property
    def names(self) -> list[str]:
        return [channel.name for channel in self._channels]

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def _respect_gap(self) -> float:
        """Wait so two consecutive messages are at least `gap` seconds apart."""
        if self._gap <= 0:
            return 0.0
        async with self._gap_lock:
            waited = 0.0
            if self._last_sent_at is not None:
                elapsed = self._clock() - self._last_sent_at
                if elapsed < self._gap:
                    waited = self._gap - elapsed
            if waited > 0:
                await asyncio.sleep(waited)
            self._last_sent_at = self._clock()
            return waited

    async def send(self, message: Message) -> Delivery:
        delivery = Delivery()
        if not self._channels:
            return delivery

        await self._respect_gap()
        for channel in self._channels:
            reason = await self._deliver_one(channel, message)
            if reason is None:
                delivery.sent.append(channel.name)
            else:
                delivery.failed[channel.name] = reason
        return delivery

    async def _deliver_one(self, channel: HttpChannel, message: Message) -> str | None:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await channel.send(message, self._client)
            except (RetryableDelivery, httpx.HTTPError, OSError) as exc:
                if attempt >= MAX_ATTEMPTS:
                    return _reason(channel, exc)
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
            except Exception as exc:  # noqa: BLE001 - 一个渠道的意外不能带走其它渠道
                return _reason(channel, exc)
            else:
                return None
        return "exhausted attempts"  # pragma: no cover

    async def send_test(self) -> Delivery:
        """A deliberately un-deduplicated message, to prove a channel is reachable."""
        from ..models import EventKind
        from ..render import Message as M

        probe = M(
            event=EventKind.INITIALIZED,
            severity="info",
            subject="【测试】dywatch 通知渠道自检",
            markdown="### dywatch 通知渠道自检\n\n如果你看到这条消息，说明该渠道可用。",
            text="dywatch 通知渠道自检：如果你看到这条消息，说明该渠道可用。",
        )
        return await self.send(probe)


_URL_RE = re.compile(r"https?://\S+")


def _reason(channel: HttpChannel, exc: BaseException) -> str:
    """Describe a failure without echoing the channel's credentials.

    渠道 URL 里通常就带着 token（Telegram 的 bot token 在路径里、钉钉的 sign 在 query 里），
    而 httpx 的异常文本经常把整个 URL 带出来。所以这里**无条件**把所有 URL 换成占位符：
    依赖"某个渠道没有凭据在 URL 里"是一条会随时间失效的假设，正则替换不会。
    """
    text = f"{type(exc).__name__}: {exc}"
    text = _URL_RE.sub(f"<{channel.name} target>", text)
    return text[:300]


__all__ = [
    "BARK_LEVELS",
    "Channel",
    "Delivery",
    "HttpChannel",
    "MAX_ATTEMPTS",
    "Notifier",
    "RETRYABLE_STATUS",
    "RetryableDelivery",
    "SEND_TIMEOUT_SECONDS",
    "sign_dingtalk_url",
]
