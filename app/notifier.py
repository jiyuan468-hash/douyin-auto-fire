"""Douyin notification adapters: abstract base, DingTalk, Feishu, generic webhook."""
from __future__ import annotations

import abc
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from app.models import TargetResult


NOTIFY_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_RESULTS_PER_SECTION = 15
MAX_MARKDOWN_BYTES = 18_000


class NotificationAdapter(abc.ABC):
    """Base class for notification channel adapters."""

    @abc.abstractmethod
    async def send(
        self,
        title: str,
        text: str,
        screenshots: list[Path] | None = None,
    ) -> None:
        ...

    @classmethod
    def _truncate_utf8(cls, text: str, max_bytes: int) -> str:
        if len(text.encode("utf-8")) <= max_bytes:
            return text
        suffix = "\n\n> 通知内容过长，部分内容已省略。"
        available = max_bytes - len(suffix.encode("utf-8"))
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if len(text[:middle].encode("utf-8")) <= available:
                low = middle
            else:
                high = middle - 1
        return f"{text[:low]}{suffix}"

    @classmethod
    def _markdown_text(cls, value: str, limit: int | None = None) -> str:
        text = " ".join(value.splitlines()).strip()
        if limit is not None and len(text) > limit:
            text = f"{text[:limit - 3]}..."
        for ch in ("\\\\", "`", "*", "_", "[", "]", "#", ">", "|"):
            text = text.replace(ch, f"\\{ch}")
        return text


# Module-level helpers for backward compat with tests
def _truncate_utf8(text, max_bytes):
    if len(text.encode('utf-8')) <= max_bytes:
        return text
    suffix = '\n\n> 通知内容过长，部分内容已省略。'
    available = max_bytes - len(suffix.encode('utf-8'))
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(text[:middle].encode('utf-8')) <= available:
            low = middle
        else:
            high = middle - 1
    return f'{text[:low]}{suffix}'


def _markdown_text(value, limit=None):
    text = ' '.join(value.splitlines()).strip()
    if limit is not None and len(text) > limit:
        text = f'{text[:limit - 3]}...'
    for ch in ('\\', '', '*', '_', '[', ']', '#', '>', '|'):
        text = text.replace(ch, f'\\{ch}')
    return text


class DingTalkAdapter(NotificationAdapter):
    def __init__(self, webhook: str, secret: str) -> None:
        self._webhook = webhook
        self._secret = secret

    async def send(self, title: str, text: str, screenshots: list[Path] | None = None) -> None:
        url = _signed_webhook_url(self._webhook, self._secret)
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": _truncate_utf8(text, MAX_MARKDOWN_BYTES)},
            "at": {"isAtAll": False},
        }
        await asyncio.to_thread(_post_json, url, payload)

    @classmethod
    def _signed_webhook_url(cls, webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
        timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
        signature = base64.b64encode(
            hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
        ).decode()
        parsed = urlsplit(webhook)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend((("timestamp", str(timestamp)), ("sign", signature)))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


class FeishuAdapter(NotificationAdapter):
    async def send(self, title: str, text: str, screenshots: list[Path] | None = None) -> None:
        url = os.getenv("FEISHU_WEBHOOK")
        if not url:
            raise ValueError("FEISHU_WEBHOOK 未配置")
        text = _truncate_utf8(text, MAX_MARKDOWN_BYTES)
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": text}},
                ],
            },
        }
        await asyncio.to_thread(_post_json, url, payload)


class GenericWebhookAdapter(NotificationAdapter):
    def __init__(self, url: str, method: str = "POST", headers: dict[str, str] | None = None) -> None:
        self._url = url
        self._method = method.upper()
        self._headers = headers or {}

    async def send(self, title: str, text: str, screenshots: list[Path] | None = None) -> None:
        payload = {
            "title": title,
            "text": text,
            "screenshots": [p.name for p in (screenshots or [])],
        }
        headers = {"Content-Type": "application/json; charset=utf-8", **self._headers}
        await asyncio.to_thread(_post_json, self._url, payload, headers)


def _post_json(url: str, payload: dict, extra_headers: dict[str, str] | None = None) -> None:
    headers = extra_headers or {}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers=headers, method="POST")
    with urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8")
    try:
        result = json.loads(body)
        if result.get("errcode") != 0:
            raise RuntimeError(f"通知接口返回错误: {result.get('errmsg', body)}")
    except (json.JSONDecodeError, KeyError):
        pass


def build_notification_text(
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    screenshots: list[Path],
    finished_at: datetime | None = None,
) -> tuple[str, str]:
    successes = [r for r in results if r.status == "success"]
    failures = [r for r in results if r.status == "failed"]
    status = "全部成功" if not failures else "存在失败"
    mode = "检查模式（未发送消息）" if dry_run else "正式发送"
    finished = (finished_at.astimezone(NOTIFY_TIMEZONE) if finished_at and finished_at.tzinfo else datetime.now(NOTIFY_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %z")
    title = f"抖音自动发送：{status}"
    lines = [
        f"### {title}",
        "",
        f"> **任务**：{_markdown_text(task_id, limit=100)}  ",
        f"> **模式**：{mode}  ",
        f"> **完成时间**：{finished}  ",
        f"> **结果**：成功 {len(successes)} 人，失败 {len(failures)} 人",
        "",
        f"#### 成功名单（{len(successes)}）",
    ]
    if successes:
        for i, r in enumerate(successes[:MAX_RESULTS_PER_SECTION], 1):
            detail = "验证通过" if dry_run else f"已发送 {r.sent} 条"
            lines.append(f"{i}. **{_markdown_text(r.target, limit=100)}** - {detail}")
        if len(successes) > MAX_RESULTS_PER_SECTION:
            lines.append(f"- 其余 {len(successes) - MAX_RESULTS_PER_SECTION} 人已省略")
    else:
        lines.append("无")
    lines.extend(["", f"#### 失败名单（{len(failures)}）"])
    if failures:
        for i, r in enumerate(failures[:MAX_RESULTS_PER_SECTION], 1):
            error = _markdown_text(r.error or "未知错误", limit=300)
            sent = f"，已发送 {r.sent} 条" if r.sent else ""
            lines.append(f"{i}. **{_markdown_text(r.target, limit=100)}**{sent}")
            lines.append(f"   - 原因：{error}")
        if len(failures) > MAX_RESULTS_PER_SECTION:
            lines.append(f"- 其余 {len(failures) - MAX_RESULTS_PER_SECTION} 人已省略")
    else:
        lines.append("无")
    if screenshots:
        lines.extend(["", "#### 失败截图"])
        lines.extend(f"- `{_markdown_text(p.name, limit=100)}`" for p in screenshots[:MAX_RESULTS_PER_SECTION])
        run_url = _github_run_url()
        if run_url:
            lines.extend(["", f"[打开本次 GitHub Actions 运行并下载截图]({run_url})", "", "> 截图将在任务结束后出现在该次运行底部的 Artifacts 中。"])
    return title, _truncate_utf8("\n".join(lines), MAX_MARKDOWN_BYTES)


def _markdown_text(value: str, limit: int | None = None) -> str:
    text = " ".join(value.splitlines()).strip()
    if limit is not None and len(text) > limit:
        text = f"{text[:limit - 3]}..."
    for ch in ("\\\\", "`", "*", "_", "[", "]", "#", ">", "|"):
        text = text.replace(ch, f"\\{ch}")
    return text


def _github_run_url() -> str | None:
    server = os.getenv("GITHUB_SERVER_URL")
    repository = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if not server or not repository or not run_id:
        return None
    return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"


async def notify(
    settings,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    screenshots: list[Path],
    logger,
) -> None:
    title, text = build_notification_text(task_id, dry_run, results, screenshots)
    adapters = []
    if "dingtalk" in settings.notification_channels and settings.dingtalk_webhook and settings.dingtalk_secret:
        adapters.append(DingTalkAdapter(settings.dingtalk_webhook, settings.dingtalk_secret))
    if "feishu" in settings.notification_channels and settings.feishu_webhook:
        adapters.append(FeishuAdapter())
    if "webhook" in settings.notification_channels and settings.webhook_url:
        adapters.append(GenericWebhookAdapter(
            settings.webhook_url,
            method=settings.webhook_method,
            headers=settings.webhook_headers,
        ))
    if not adapters:
        return
    tasks = [a.send(title, text, screenshots) for a in adapters]
    await asyncio.gather(*tasks, return_exceptions=True)
    for adapter in adapters:
        cls_name = adapter.__class__.__name__
        logger.info(f"{cls_name} 通知发送完成")


# 保留旧的入口函数供测试兼容
# 向后兼容导出
_signed_webhook_url = DingTalkAdapter._signed_webhook_url
build_dingtalk_markdown = build_notification_text

async def send_dingtalk_notification(webhook, secret, task_id, dry_run, results, screenshots):
    title, text = build_notification_text(task_id, dry_run, results, screenshots)
    await DingTalkAdapter(webhook, secret).send(title, text, screenshots)
