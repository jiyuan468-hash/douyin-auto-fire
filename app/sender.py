from __future__ import annotations

import asyncio
import random
import secrets
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page

from app.douyin import DouyinChat, PageOperationError, first_visible
from app.models import Message, Sticker
from app.selectors import IMAGE_INPUTS, MESSAGE_INPUTS, STICKER_BUTTONS, STICKER_PANELS
from app.message_library import select_messages


def _monotonic() -> float:
    return asyncio.get_running_loop().time()


SEND_BUTTONS = (
    "[class*=messageMsgInputpublishBtn]",
    ".e2e-send-msg-bt",
    'button[aria-label*="发送"]',
    '[role="button"][aria-label*="发送"]',
)


async def _trigger_send(page: Page) -> None:
    button = None
    for selector in SEND_BUTTONS:
        candidate = page.locator(selector).first
        try:
            if await candidate.count() and await candidate.is_visible():
                button = candidate
                break
        except Exception:
            continue
    if button is not None:
        await button.click()
    else:
        await page.keyboard.press("Enter")


async def _publish_ready(page: Page) -> bool:
    for selector in SEND_BUTTONS:
        candidate = page.locator(selector).first
        try:
            if await candidate.count() and await candidate.is_visible():
                return True
        except Exception:
            continue
    return False


LATEST_OUTGOING_MESSAGE = (
    '.messageMessageListlist [data-index="0"] '
    '.messageMessageBoxmessageBox:has(.messageMessageBoxcontentBox.messageMessageBoxisFromMe)'
)
MESSAGE_CONFIRM_ANCHOR = "data-douyin-sender-anchor"

SEND_FAILURE_MARKERS = (
    "text=发送失败",
    '[aria-label*="重试"]',
    '[title*="重试"]',
    '[class*="sendFailed"]',
    '[class*="SendFailed"]',
    '[class*="ContentSideSendStatusretry"]',
    '[class*="SendStatusretry"]',
)
SEND_PENDING_MARKERS = (
    ".semi-spin",
    '[class*="im-saas-message-spin"]',
    '[data-icon="spin"]',
)

SEND_CONFIRM_TIMEOUT_MS = 15_000
SEND_POLL_INTERVAL_MS = 300
SEND_STABLE_INTERVAL_MS = 500
SEND_INITIAL_CLEAN_GRACE_MS = 2_000
# 打字模拟参数
_TYPING_CHAR_DELAY_MIN_MS = 40
_TYPING_CHAR_DELAY_MAX_MS = 120


async def send_message(page: Page, chat: DouyinChat, message: Message, stickers: dict[str, Sticker]) -> None:
    if message.type == "random":
        await send_message(page, chat, random.choice(message.choices), stickers)
        return
    if message.type == "text":
        await send_text(chat, message.content or "")
        return
    if message.type == "image":
        if message.path is None:
            raise PageOperationError("图片消息缺少文件路径")
        await send_image(page, message.path.as_posix())
        return
    if message.type == "douyin_sticker":
        sticker = stickers.get(message.sticker or "")
        if sticker is None:
            raise PageOperationError(f"没有原生表情映射: {message.sticker}")
        await send_douyin_sticker(page, sticker)
        return
    raise PageOperationError(f"不支持的消息类型: {message.type!r}")


async def send_text(chat: DouyinChat, text: str) -> None:
    if not text:
        return
    input_el = await first_visible(chat.page, MESSAGE_INPUTS, chat.timeout_ms)
    old_scroll = await _get_scroll_position(chat.page)
    await input_el.focus()
    await chat.page.wait_for_timeout(random.uniform(200, 500))
    # 模拟真人逐字输入
    for ch in text:
        await chat.page.keyboard.type(ch, delay=random.uniform(_TYPING_CHAR_DELAY_MIN_MS, _TYPING_CHAR_DELAY_MAX_MS))
        await chat.page.wait_for_timeout(random.uniform(20, 50))
    await _restore_scroll_position(chat.page, old_scroll)
    await _trigger_send(chat.page)
    await _confirm_outgoing_message(chat.page, text=text)


async def send_image(page: Page, image_path: str) -> None:
    file_input = await first_visible(page, IMAGE_INPUTS, timeout_ms=10_000)
    old_scroll = await _get_scroll_position(page)
    await file_input.set_input_files(image_path)
    await page.wait_for_timeout(1500)
    await _restore_scroll_position(page, old_scroll)
    await _trigger_send(page)
    anchor = f"anchor-img-{secrets.token_hex(4)}"
    await page.locator(LATEST_OUTGOING_MESSAGE).evaluate(
        f"el => el.setAttribute('{MESSAGE_CONFIRM_ANCHOR}', '{anchor}')"
    )
    await _confirm_outgoing_message(page, (anchor, ""), "图片", resource_key=image_path.split("/")[-1])


async def send_douyin_sticker(page: Page, sticker: Sticker) -> None:
    await _click_and_open_sticker_panel(page, sticker)
    await _confirm_sticker_sent(page, sticker)


async def _await_send_terminal_state(page: Page, scope: Locator, label: str, timeout_ms: int = SEND_CONFIRM_TIMEOUT_MS) -> None:
    deadline = _monotonic() + timeout_ms / 1000

    grace_deadline = _monotonic() + SEND_INITIAL_CLEAN_GRACE_MS / 1000
    while _monotonic() < grace_deadline:
        if _monotonic() >= deadline:
            raise PageOperationError(
                f"{label}发送状态未能确认（发送超时或状态不确定），为避免重复不会自动重试"
            )
        if await _marker_visible(scope, SEND_FAILURE_MARKERS):
            raise PageOperationError(f"{label}发送失败，页面提示可以重试")
        if await _marker_visible(scope, SEND_PENDING_MARKERS):
            break
        await page.wait_for_timeout(SEND_POLL_INTERVAL_MS)
    else:
        return

    while True:
        if _monotonic() >= deadline:
            raise PageOperationError(
                f"{label}发送状态未能确认（发送超时或状态不确定），为避免重复不会自动重试"
            )
        if await _marker_visible(scope, SEND_FAILURE_MARKERS):
            raise PageOperationError(f"{label}发送失败，页面提示可以重试")
        if not await _marker_visible(scope, SEND_PENDING_MARKERS):
            await page.wait_for_timeout(SEND_STABLE_INTERVAL_MS)
            if await _marker_visible(scope, SEND_FAILURE_MARKERS):
                raise PageOperationError(f"{label}发送失败，页面提示可以重试")
            if not await _marker_visible(scope, SEND_PENDING_MARKERS):
                return
        await page.wait_for_timeout(SEND_POLL_INTERVAL_MS)


async def _confirm_outgoing_message(
    page: Page,
    before: tuple[str, str],
    label: str,
    resource_key: str = "",
    expected_text: str = "",
) -> None:
    anchor, before_content = before
    try:
        await page.wait_for_function(
            """([selector, anchor, previousContent, expectedResource, expectedText]) => {
                const message = document.querySelector(selector);
                if (!message) return false;
                const content = message.querySelector('[data-e2e="msg-item-content"]') || message;
                const isNewMessage =
                    message.getAttribute('data-douyin-sender-anchor') !== anchor ||
                    content.innerHTML !== previousContent;
                if (!isNewMessage) return false;
                if (expectedText) {
                    const normalize = value => (value || '').replace(/[\s\u200B\u200C\u200D\uFEFF]+/g, ' ').trim();
                    return normalize(content.innerText).includes(normalize(expectedText));
                }
                if (!expectedResource) return true;
                const images = [...content.querySelectorAll('img')];
                return images.some(image => (image.src || '').includes(expectedResource)) || images.length > 0;
            }""",
            arg=[LATEST_OUTGOING_MESSAGE, anchor, before_content, resource_key, expected_text],
            timeout=15_000,
        )
        latest = page.locator(LATEST_OUTGOING_MESSAGE).first
        await _await_send_terminal_state(page, latest, label)
    except PageOperationError:
        raise
    except Exception as exc:
        raise PageOperationError(f"{label}已发送，但没有检测到新的已发送消息") from exc
    finally:
        anchors = page.locator(f"[{MESSAGE_CONFIRM_ANCHOR}]")
        try:
            await anchors.evaluate_all(
                "elements => elements.forEach(element => element.removeAttribute('data-douyin-sender-anchor'))"
            )
        except Exception:
            pass


# Backward-compatible aliases for tests
async def _mark_latest_outgoing_message(page, anchor):
    try:
        latest = page.locator(LATEST_OUTGOING_MESSAGE).first
        content = await latest.evaluate("el => el.getAttribute('data-douyin-sender-anchor') or ''")
        return (anchor, content or "")
    except Exception:
        pass
    return (anchor, "")


async def _get_scroll_position(page: Page) -> dict:
    return await page.evaluate("() => ({scrollX: window.scrollX, scrollY: window.scrollY})")


async def _restore_scroll_position(page: Page, pos: dict) -> None:
    await page.evaluate("window.scrollTo(" + str(pos['scrollX']) + ", " + str(pos['scrollY']) + ")")


# 向后兼容别名
async def _restore_composer(page):
    pass

async def _marker_visible(scope: Locator, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        try:
            locator = scope.locator(marker).first
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


# 向后兼容别名
async def _click_and_confirm_sticker(page, sticker, before=None, label=""):
    # Accept both old (page, sticker, before, label) and new signatures
    return await _click_and_open_sticker_panel(page, sticker)
    return await _click_and_open_sticker_panel(panel, sticker)

async def _click_and_open_sticker_panel(page: Page, sticker: Sticker) -> None:
    sticker_btn = await first_visible(page, STICKER_BUTTONS, timeout_ms=5_000)
    await sticker_btn.click()
    panel = await first_visible(page, STICKER_PANELS, timeout_ms=3_000)
    await _select_sticker(panel, sticker)


async def _select_sticker(panel: Locator, sticker: Sticker) -> None:
    index = sticker.fallback_index
    if index is not None:
        try:
            await panel.locator(f'[data-index="{index}"]').first.click()
            return
        except Exception:
            pass
    if sticker.accessible_name:
        await panel.get_by_role("option", name=sticker.accessible_name).first.click()
    elif sticker.category:
        cat_tab = panel.get_by_role("tab", name=sticker.category).first
        try:
            await cat_tab.click()
            await panel.get_by_role("option", name=sticker.name).first.click()
        except Exception:
            await panel.get_by_role("option", name=sticker.name).first.click()
    else:
        await panel.get_by_role("option", name=sticker.name).first.click()


async def _confirm_sticker_sent(page: Page, sticker: Sticker) -> None:
    anchor = f"data-douyin-sender-anchor-{secrets.token_hex(4)}"
    before_content = await _get_sticker_anchor(page, anchor)
    await page.locator(LATEST_OUTGOING_MESSAGE).evaluate(
        f"el => el.setAttribute('{MESSAGE_CONFIRM_ANCHOR}', '{anchor}')"
    )
    await _confirm_outgoing_message(page, (anchor, before_content), "原生表情", _sticker_resource_key(sticker))


async def _get_sticker_anchor(page: Page, anchor: str) -> str:
    try:
        return (await page.locator(LATEST_OUTGOING_MESSAGE).first.evaluate(
            "el => el.innerHTML"
        )) or ""
    except Exception:
        return ""


async def _sticker_resource_key(sticker: Sticker) -> str:
    if sticker.accessible_name:
        return sticker.accessible_name
    return sticker.name
