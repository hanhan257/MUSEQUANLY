"""Playwright automation: Muse AI login with Lay2FA OTP."""

from __future__ import annotations

import os
import random
import re
import subprocess
import threading
import time
from datetime import date, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page

from cookie_store import cookies_to_header, filter_muse_cookies, save_account_cookies, update_account_meta
from lay2fa_client import fetch_2fa_live_code, meta_message_ids, wait_for_new_muse_code

MUSE_URL = "https://muse.ai/"
CONNECTED = re.compile(r"Connected|Đã kết nối|kết nối", re.I)
EMAIL_PLACEHOLDER = re.compile(r"email|di động|mobile", re.I)
BTN_CONTINUE = re.compile(r"Continue|Tiếp", re.I)
BTN_EMAIL_NEXT = re.compile(r"^Tiếp tục$|^Continue$", re.I)
CODE_SCREEN = re.compile(r"Enter your code|Nhập mã|enter the code", re.I)
RECOVERY_OPTION = re.compile(r"Mã khôi phục|Recovery code", re.I)
LOGGED_IN_URL = re.compile(r"muse\.ai/(app|chat|home|access)|muse\.ai/\?", re.I)
CONSENT_STATUS_URL = "https://muse.ai/api/consent/status"


def _is_consent_status_url(url: str) -> bool:
    base = (url or "").split("?", 1)[0].rstrip("/")
    return base == CONSENT_STATUS_URL or base.endswith("/api/consent/status")


@dataclass
class LoginProgress:
    step: str = "idle"
    detail: str = ""
    code: str | None = None
    error: str | None = None
    final_url: str | None = None
    cookie_header: str | None = None
    cookies_count: int = 0
    cookies_saved_at: str | None = None


ProgressCallback = Callable[[LoginProgress], None]
_cancel = threading.Event()


def stop_requested() -> bool:
    return _cancel.is_set()


def clear_stop() -> None:
    _cancel.clear()


def request_stop() -> int:
    """Dừng batch và đóng Chrome do tool mở (bản Playwright, không phải Chrome thường)."""
    _cancel.set()
    if os_name_is_windows():
        return _kill_playwright_browsers()
    return 0


def os_name_is_windows() -> bool:
    import os

    return os.name == "nt"


def _kill_playwright_browsers() -> int:
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'chrome|chromium' -and $_.CommandLine -and "
        "($_.CommandLine -like '*ms-playwright*' -or $_.CommandLine -like '*playwright_chromiumdev_profile*') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return 0
    return len([line for line in (done.stdout or "").splitlines() if line.strip().isdigit()])


def _emit(cb: ProgressCallback | None, **kwargs: object) -> None:
    if cb:
        cb(LoginProgress(**kwargs))  # type: ignore[arg-type]


def _logged_in_now(page: Page) -> bool:
    if _confirm_screen_visible(page):
        return False
    url = page.url or ""
    if LOGGED_IN_URL.search(url):
        return True
    try:
        connected = page.get_by_text(CONNECTED)
        if connected.count() > 0 and connected.first.is_visible():
            return True
    except Exception:
        pass
    return False


def _email_code_screen(page: Page) -> bool:
    """Màn nhập mã thư Meta, không phải ô mã khôi phục."""
    for frame in _frames(page):
        try:
            recovery = frame.get_by_text(
                re.compile(r"mã khôi phục|recovery code|authenticator|ứng dụng xác thực|authentication app", re.I)
            )
            if recovery.count() > 0 and recovery.first.is_visible():
                return False
        except Exception:
            pass
        try:
            loc = frame.get_by_text(CODE_SCREEN)
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _wait_logged_in(page: Page, timeout_sec: float = 120) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        url = page.url or ""
        if LOGGED_IN_URL.search(url):
            return True
        if page.get_by_text(CONNECTED).count() > 0:
            try:
                if page.get_by_text(CONNECTED).first.is_visible():
                    return True
            except Exception:
                return True
        otp_visible = False
        try:
            otp = page.locator('input[autocomplete="one-time-code"]')
            otp_visible = otp.count() > 0 and otp.first.is_visible()
        except Exception:
            otp_visible = False
        if _confirm_screen_visible(page) or "checkpoints" in (page.url or ""):
            page.wait_for_timeout(1000)
            continue
        if "muse.ai" in url and not otp_visible and "/login" not in url.lower():
            if "auth.meta.com" not in url:
                return True
        page.wait_for_timeout(1000)
    return False


def _submit_otp(page: Page, otp_input, code: str) -> None:
    """Gửi OTP — input thường sr-only, dùng force fill + Enter (không click Next)."""
    otp = page.locator('input[autocomplete="one-time-code"]')
    otp.wait_for(state="attached", timeout=30000)
    try:
        otp.fill(code, force=True, timeout=15000)
    except Exception:
        page.evaluate(
            """(c) => {
              const el = document.querySelector('input[autocomplete="one-time-code"]');
              if (!el) return;
              el.focus();
              el.value = c;
              el.dispatchEvent(new InputEvent('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            code,
        )
    page.wait_for_timeout(400)
    try:
        otp.press("Enter", force=True)
    except Exception:
        page.keyboard.press("Enter")
    page.wait_for_timeout(2000)


def _dismiss_onboarding(page: Page) -> None:
    try:
        muse_btn = page.get_by_role("button", name=re.compile(r"^Muse$", re.I))
        if muse_btn.count() > 0 and muse_btn.first.is_visible():
            muse_btn.first.click(timeout=5000)
            page.wait_for_timeout(1500)
    except Exception:
        pass


def _cookie_from_consent_status_response(response) -> str | None:
    if response.status != 200 or not _is_consent_status_url(response.url):
        return None
    headers = response.request.headers
    return headers.get("cookie") or headers.get("Cookie")


def _human_verify_page(page: Page) -> bool:
    url = page.url or ""
    return (
        "checkpoints" in url
        or _text_visible(page, HUMAN_GATE)
        or _captcha_visible(page)
        or _selfie_upload_visible(page)
    )


def _refresh_and_get_status_cookie(page: Page) -> str | None:
    """F5 → chờ https://muse.ai/api/consent/status → Cookie header (DevTools)."""

    def try_reload_with_expect() -> str | None:
        if _human_verify_page(page):
            return None
        try:
            with page.expect_response(
                lambda r: _is_consent_status_url(r.url) and r.status == 200,
                timeout=20000,
            ) as resp_info:
                page.reload(wait_until="domcontentloaded", timeout=20000)
            if _human_verify_page(page):
                return None
            return _cookie_from_consent_status_response(resp_info.value)
        except Exception:
            return None

    header = try_reload_with_expect()
    if header or _human_verify_page(page):
        return header

    captured: dict[str, str | None] = {"cookie": None}

    def on_response(response) -> None:
        if captured["cookie"]:
            return
        captured["cookie"] = _cookie_from_consent_status_response(response)

    page.on("response", on_response)
    try:
        if _human_verify_page(page):
            return None
        page.reload(wait_until="domcontentloaded", timeout=20000)
        for _ in range(12):
            if captured["cookie"] or _human_verify_page(page):
                return captured["cookie"]
            page.wait_for_timeout(1000)
        if _human_verify_page(page):
            return None
        try:
            page.evaluate(
                "fetch('/api/consent/status', { credentials: 'include', cache: 'no-store' })"
            )
            for _ in range(8):
                if captured["cookie"] or _human_verify_page(page):
                    return captured["cookie"]
                page.wait_for_timeout(1000)
        except Exception:
            pass
    finally:
        page.remove_listener("response", on_response)
    return captured["cookie"]


DIE_TEXT = re.compile(
    r"vô hiệu|disabled|suspended|bị khóa|bị khoá|không tồn tại|account disabled|checkpoint",
    re.I,
)


def muse_token_from_cookies(cookies: list[dict], header: str) -> str:
    for name in ("hatch_sess", "hatch_native_auth_temp"):
        for cookie in cookies:
            if cookie.get("name") == name and cookie.get("value"):
                return str(cookie["value"])
    for part in (header or "").split(";"):
        piece = part.strip()
        for name in ("hatch_sess", "hatch_native_auth_temp"):
            if piece.startswith(name + "="):
                return piece.split("=", 1)[1].strip()
    return ""


def _check_muse_live(page: Page) -> str:
    try:
        data = page.evaluate(
            """async () => {
              const r = await fetch('/api/consent/status', { credentials: 'include', cache: 'no-store' });
              const text = await r.text();
              return { status: r.status, text: text.slice(0, 2000), href: location.href };
            }"""
        )
    except Exception:
        return "unknown"
    status = int((data or {}).get("status") or 0)
    text = str((data or {}).get("text") or "")
    href = str((data or {}).get("href") or "")
    if _confirm_screen_visible(page) or DIE_TEXT.search(text):
        return "die"
    if status in (401, 403):
        return "die"
    if status == 200 and LOGGED_IN_URL.search(href):
        return "live"
    return "unknown"


def _capture_and_save_cookies(
    context,
    email: str,
    *,
    status_cookie_header: str | None = None,
    page: Page | None = None,
) -> LoginProgress:
    raw = context.cookies()
    header = status_cookie_header.strip() if status_cookie_header and status_cookie_header.strip() else ""
    token = muse_token_from_cookies(raw, header)
    account_status = _check_muse_live(page) if page is not None else "unknown"
    if _confirm_screen_visible(page) if page is not None else False:
        account_status = "die"
    elif account_status == "unknown" and token and page is not None and LOGGED_IN_URL.search(page.url or ""):
        account_status = "live"
    record = save_account_cookies(
        email,
        raw,
        cookie_header_override=status_cookie_header,
        cookie_source="status_request" if status_cookie_header else "browser",
        muse_token=token,
        account_status=account_status,
        login_error="",
    )
    src = " (từ request status)" if status_cookie_header else ""
    return LoginProgress(
        step="cookies_saved",
        detail=f"Đã lưu cookie{src} cho {email}",
        cookie_header=record["cookie_header"],
        cookies_count=record["count"],
        cookies_saved_at=record["saved_at"],
    )


def normalize_proxy_line(raw: str | None) -> str:
    text = (raw or "").strip()
    for ch in ("\ufeff", "\u200b", "\u200c", "\u200d"):
        text = text.replace(ch, "")
    return text.strip()


def parse_proxy(raw: str | None) -> dict[str, str] | None:
    """Turn ip:port, ip:port:user:pass, or scheme://user:pass@host:port into a Playwright proxy."""
    text = normalize_proxy_line(raw)
    if not text:
        return None
    if "://" not in text:
        if "@" in text:
            auth, hostport = text.rsplit("@", 1)
            user, _, password = auth.partition(":")
            return {"server": f"http://{hostport}", "username": user, "password": password}
        parts = text.split(":")
        if len(parts) == 2:
            return {"server": f"http://{parts[0]}:{parts[1]}"}
        if len(parts) >= 4:
            host, port, user = parts[0], parts[1], parts[2]
            password = ":".join(parts[3:])
            return {"server": f"http://{host}:{port}", "username": user, "password": password}
        return None
    from urllib.parse import urlparse

    parsed = urlparse(text)
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    out = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
        out["password"] = parsed.password or ""
    return out


CONFIRM_SCREEN = re.compile(r"Xác nhận Tài khoản Meta|Confirm your Meta account", re.I)
HUMAN_GATE = re.compile(r"Xác nhận bạn là người thật", re.I)
CAPTCHA_GATE = re.compile(r"Chọn tất cả hình ảnh|Select all images|I'm not a robot", re.I)
SELFIE_GATE = re.compile(r"tải ảnh|tải hình|upload a photo|upload photo|selfie|ảnh chân dung|chụp ảnh khuôn mặt", re.I)
SELFIE_DIR = Path(r"D:\Sefile")
SELFIE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
UPLOAD_BTN = re.compile(r"Tải hình ảnh lên|Tải ảnh lên|Upload photo|Upload", re.I)
BTN_NEXT = re.compile(r"^Tiếp$|^Tiếp tục$|^Continue$|^Next$", re.I)
SIGNUP_SUBMIT = re.compile(r"^Xác nhận$|^Confirm$|^Tiếp$|^Tiếp tục$|^Continue$|^Next$", re.I)
SIGNUP_BLOCKED = re.compile(
    r"không thể tạo tài khoản mới|cannot create (?:a |an )?new account|can(?:not|'t) create (?:a |an )?(?:new )?account",
    re.I,
)
MAIL_FLAGGED = re.compile(
    r"spam|disposable|temporary email|email tạm|không hỗ trợ email|email này không|không thể dùng email|try a different email|use a different email|invalid email|email không hợp lệ|không chấp nhận",
    re.I,
)
CARD_PAGE = re.compile(r"Xác nhận tuổi của bạn|Confirm your age", re.I)
CARD_BAD = re.compile(
    r"thẻ không hợp lệ|thẻ bị từ chối|thẻ này không|không thể xác minh|không thể xử lý thẻ|thẻ đã bị|invalid card|card (?:was )?declined|incorrect card|try another card|card number is invalid",
    re.I,
)
CARD_BUSY = re.compile(r"too many attempts|too many failed|quá nhiều lần|thử lại sau", re.I)
CARD_ZIP = "06779"
CARD_CONFIRM = re.compile(r"^Xác nhận$|^Confirm$", re.I)
DISCLOSURE_PAGE = re.compile(
    r"Một số điều cần biết|A few things to know|Some things you should know|Things to know",
    re.I,
)
BTN_START = re.compile(r"^Bắt đầu$|^Get started$|^Start$", re.I)
INVITE_CODE_RE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{6,16}\b")
REDEEM_ROW = re.compile(r"Quy đổi mã lời mời|Redeem invite code|Have an invite code", re.I)
SETTINGS_NAME = re.compile(r"^Cài đặt$|^Settings$", re.I)
GENERAL_NAME = re.compile(r"^Chung$|^General$", re.I)
USAGE_NAME = re.compile(r"^Mức sử dụng$|^Sử dụng$|^Usage$", re.I)
INVITE_OPEN = re.compile(r"^Mời$|^Invite$", re.I)
INVITE_PANEL = re.compile(r"Invite friends|Mời bạn bè|Copy referral link|Sao chép liên kết", re.I)
REVEAL_CODE = re.compile(r"Hiện mã|Reveal|Show code|Xem mã", re.I)
COPY_CODE = re.compile(r"Copy referral link|Sao chép liên kết giới thiệu|Sao chép liên kết|^Sao chép$|^Copy$|^Copy code$", re.I)
REDEEM_OK = re.compile(r"^Quy đổi$|^Xác nhận$|^Redeem$|^Confirm$", re.I)
INVITE_OK = re.compile(
    r"quy đổi thành công|đã quy đổi|redeemed|you're all set|1 tỷ|1 billion|tokens added",
    re.I,
)
MENU_BTN = re.compile(r"^Menu$|^Open menu$|^Mở menu$|^Điều hướng$", re.I)
VERIFY_BTN = re.compile(r"^XÁC MINH$|^VERIFY$", re.I)
_signup_turn_lock = threading.Lock()
_next_signup_at = 0.0


def _wait_signup_turn(on_progress: ProgressCallback | None) -> None:
    """Mỗi tài khoản cách nhau 20 giây lúc mở Chrome, tránh đụng Muse cùng một lúc."""
    global _next_signup_at
    with _signup_turn_lock:
        now = time.monotonic()
        delay = _next_signup_at - now
        if delay < 0:
            delay = 0
        _next_signup_at = now + delay + 20
    if delay >= 1:
        _emit(on_progress, step="starting", detail="Chờ lượt tạo, không mở nhiều tài khoản cùng lúc")
        time.sleep(delay)


def _frames(page: Page):
    frames = list(page.frames)
    return frames or [page.main_frame]


def _confirm_screen_visible(page: Page) -> bool:
    for frame in _frames(page):
        try:
            loc = frame.get_by_text(CONFIRM_SCREEN)
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _visible_confirm_radios(page: Page) -> list:
    for frame in _frames(page):
        found = []
        try:
            radios = frame.get_by_role("radio")
            total = min(radios.count(), 6)
        except Exception:
            continue
        for index in range(total):
            item = radios.nth(index)
            try:
                if item.is_visible():
                    found.append(item)
            except Exception:
                continue
        if found:
            return found
    return []


def _radio_checked(item) -> bool:
    try:
        if (item.get_attribute("aria-checked") or "").lower() == "true":
            return True
        nested = item.locator("input[type='radio']")
        if nested.count() > 0 and nested.first.is_checked():
            return True
    except Exception:
        return False
    return False


_BACKUP_ONLY = re.compile(r"Backup|Ứng dụng xác thực|Authenticator", re.I)
_RECOVERY_ONLY = re.compile(r"Mã khôi phục|Recovery code", re.I)


def _checked_option_text(page: Page) -> str:
    """Chữ của đúng dòng đang được tích. Không lấy cả cụm hai lựa chọn."""
    script = """() => {
      function lineOf(el) {
        let node = el;
        for (let i = 0; i < 6 && node; i++) {
          const t = (node.innerText || node.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
          const backup = /backup|ứng dụng xác thực|authenticator/i.test(t);
          const recovery = /mã khôi phục|recovery code/i.test(t);
          if (backup && !recovery) return t.slice(0, 80);
          if (recovery && !backup) return t.slice(0, 80);
          node = node.parentElement;
        }
        return '';
      }
      const radios = [...document.querySelectorAll('[role="radio"], input[type="radio"]')];
      for (const radio of radios) {
        const checked = radio.getAttribute('aria-checked') === 'true' || radio.checked === true;
        if (!checked) continue;
        const text = lineOf(radio);
        if (text) return text;
      }
      return '';
    }"""
    for frame in _frames(page):
        try:
            text = frame.evaluate(script) or ""
        except Exception:
            continue
        if text:
            return str(text)
    return ""


def _first_2fa_selected(page: Page) -> bool:
    text = _checked_option_text(page)
    if _RECOVERY_ONLY.search(text):
        return False
    return bool(_BACKUP_ONLY.search(text))


def _click_backup_row(page: Page) -> None:
    """Bấm đúng dòng Backup bằng chuột thật. Không đụng dòng Mã khôi phục."""
    for frame in _frames(page):
        try:
            row = frame.get_by_role("radio").filter(has_text=_BACKUP_ONLY).filter(has_not_text=_RECOVERY_ONLY)
            if row.count() > 0 and row.first.is_visible():
                row.first.click(timeout=3000)
                page.wait_for_timeout(250)
                if _first_2fa_selected(page):
                    return
                row.first.focus(timeout=1000)
                page.keyboard.press("Space")
                page.wait_for_timeout(250)
                if _first_2fa_selected(page):
                    return
        except Exception:
            pass
        try:
            label = frame.get_by_text(re.compile(r"^Backup \(Ứng dụng xác thực\)$|^Backup$", re.I))
            if label.count() > 0 and label.first.is_visible():
                label.first.click(timeout=3000)
                page.wait_for_timeout(250)
                if _first_2fa_selected(page):
                    return
        except Exception:
            continue


def _click_first_2fa_option(page: Page) -> bool:
    """Tích đúng dòng Backup. Mã khôi phục đang sáng thì chưa được bấm Tiếp."""
    for _ in range(3):
        if _first_2fa_selected(page):
            return True
        _click_backup_row(page)
        page.wait_for_timeout(300)
    return _first_2fa_selected(page)


def _click_named_button(page: Page, pattern: re.Pattern[str]) -> bool:
    for frame in _frames(page):
        try:
            button = frame.get_by_role("button", name=pattern)
            if button.count() == 0:
                button = frame.get_by_text(pattern)
            if button.count() > 0 and button.first.is_visible():
                button.first.click(force=True, timeout=5000)
                return True
        except Exception:
            continue
    return False


def _visible_excerpt(page: Page, limit: int = 700) -> str:
    chunks: list[str] = []
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            text = frame.locator("body").inner_text(timeout=2000)
        except Exception:
            continue
        text = " ".join((text or "").split())
        if text:
            chunks.append(text)
    return " ".join(chunks)[:limit]


def _text_visible(page: Page, pattern: re.Pattern[str]) -> bool:
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            loc = frame.get_by_text(pattern)
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _captcha_visible(page: Page) -> bool:
    if _text_visible(page, CAPTCHA_GATE):
        return True
    for frame in _frames(page):
        url = (frame.url or "").lower()
        if "recaptcha" in url or "google.com/recaptcha" in url:
            return True
    return False


def _selfie_upload_visible(page: Page) -> bool:
    """Chỉ màn 'Tải ảnh selfie'. Input file ẩn ở trang khác không tính."""
    return _text_visible(page, SELFIE_GATE)


def _selfie_preview_visible(page: Page) -> bool:
    """Ảnh đã hiện trong khung upload, không phải icon nhỏ trên nút."""
    script = """() => {
      function bigEnough(el) {
        const r = el.getBoundingClientRect();
        return r.width >= 80 && r.height >= 80;
      }
      function walk(node) {
        if (!node) return false;
        if (node.shadowRoot && walk(node.shadowRoot)) return true;
        if (node.nodeType !== 1) {
          for (const child of node.children || []) {
            if (walk(child)) return true;
          }
          return false;
        }
        if (node.nodeType === 1) {
          const tag = (node.tagName || '').toLowerCase();
          if (tag === 'img') {
            const src = node.currentSrc || node.src || '';
            if (bigEnough(node) && src && !src.startsWith('data:image/svg')) return true;
          }
          const bg = getComputedStyle(node).backgroundImage || '';
          if (bigEnough(node) && /blob:|data:image\\/(?!svg)/.test(bg)) return true;
          for (const child of node.children || []) {
            if (walk(child)) return true;
          }
        }
        return false;
      }
      return walk(document.body || document.documentElement);
    }"""
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            if frame.evaluate(script):
                return True
        except Exception:
            continue
    return False


def _captcha_challenge_visible(page: Page) -> bool:
    """Lưới chọn hình đang mở. Iframe recaptcha còn sót không tính là đang làm captcha."""
    for frame in _frames(page):
        try:
            verify = frame.locator("#recaptcha-verify-button")
            if verify.count() > 0 and verify.first.is_visible():
                return True
            prompt = frame.get_by_text(CAPTCHA_GATE)
            if prompt.count() > 0 and prompt.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _click_captcha_verify(page: Page) -> str:
    """Bấm XÁC MINH, kể cả khi nút nằm trong iframe hoặc shadow. Không tự chọn ô hình."""
    notes: list[str] = []
    script = """() => {
      const re = /x[áa]c\\s*minh|^verify$/i;
      const hits = [];
      function walk(node) {
        if (!node) return;
        if (node.shadowRoot) walk(node.shadowRoot);
        if (node.nodeType !== 1) {
          for (const child of node.children || []) walk(child);
          return;
        }
        const tag = (node.tagName || '').toLowerCase();
        const role = (node.getAttribute && node.getAttribute('role')) || '';
        const id = node.id || '';
        const raw = (node.childElementCount > 4)
          ? ''
          : ((node.textContent || node.value || (node.getAttribute && node.getAttribute('aria-label')) || ''));
        const text = raw.replace(/\\s+/g, ' ').trim();
        const isBtn = tag === 'button' || role === 'button' || id === 'recaptcha-verify-button';
        if ((id === 'recaptcha-verify-button' || (isBtn && re.test(text) && text.length < 40))) {
          const r = node.getBoundingClientRect();
          if (r.width > 4 && r.height > 4) {
            hits.push({id, text: text.slice(0, 24), x: r.x, y: r.y, w: r.width, h: r.height});
          }
        }
        for (const child of node.children || []) walk(child);
      }
      walk(document.body || document.documentElement);
      const iframes = [...document.querySelectorAll('iframe')].map(f => (f.src || f.id || 'iframe').slice(0, 70));
      return {hits: hits.slice(0, 6), iframes: iframes.slice(0, 6)};
    }"""
    for frame in _frames(page):
        url = ((frame.url or "").split("?")[0])[-42:]
        try:
            info = frame.evaluate(script)
        except Exception as exc:
            notes.append(f"{type(exc).__name__}:{url}")
            continue
        hits = info.get("hits") or []
        iframes = info.get("iframes") or []
        if iframes:
            notes.append("if:" + ",".join(iframes)[:100])
        if not hits:
            notes.append(f"none:{url}")
            continue
        hit = hits[0]
        try:
            origin_x = 0.0
            origin_y = 0.0
            if frame != page.main_frame:
                iframe_box = frame.frame_element().bounding_box()
                if iframe_box:
                    origin_x = iframe_box["x"]
                    origin_y = iframe_box["y"]
            page.mouse.click(
                origin_x + hit["x"] + hit["w"] / 2,
                origin_y + hit["y"] + hit["h"] / 2,
            )
            return "ok"
        except Exception as exc:
            notes.append(f"click:{type(exc).__name__}:{url}")
    if not notes:
        return "no-frame"
    return " | ".join(notes)[:240]


def _random_selfie_path() -> Path | None:
    if not SELFIE_DIR.is_dir():
        return None
    files = [p for p in SELFIE_DIR.iterdir() if p.is_file() and p.suffix.lower() in SELFIE_EXTS]
    if not files:
        return None
    return random.choice(files)


def _wait_selfie_preview(page: Page, timeout_ms: int = 4000) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if _selfie_preview_visible(page):
            return True
        page.wait_for_timeout(250)
    return False


def _set_file_input(locator, photo: Path) -> None:
    locator.set_input_files(str(photo))
    try:
        locator.evaluate(
            """(el) => {
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
    except Exception:
        pass


def _tag_selfie_inputs(page: Page) -> int:
    script = """() => {
      let n = 0;
      function walk(node) {
        if (!node) return;
        if (node.shadowRoot) walk(node.shadowRoot);
        if (node.nodeType !== 1) {
          for (const child of node.children || []) walk(child);
          return;
        }
        if ((node.tagName || '').toLowerCase() === 'input' && (node.type || '').toLowerCase() === 'file') {
          node.setAttribute('data-muse-selfie', String(n));
          n += 1;
        }
        for (const child of node.children || []) walk(child);
      }
      walk(document.body || document.documentElement);
      return n;
    }"""
    total = 0
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            total += int(frame.evaluate(script) or 0)
        except Exception:
            continue
    return total


def _upload_button_point(page: Page) -> tuple[float, float] | None:
    """Tọa độ đúng chữ Tải hình ảnh lên, không phải cả khung lớn."""
    script = """() => {
      const hits = [];
      function walk(node) {
        if (!node) return;
        if (node.shadowRoot) walk(node.shadowRoot);
        if (node.nodeType !== 1) {
          for (const child of node.children || []) walk(child);
          return;
        }
        const own = [...(node.childNodes || [])]
          .filter((n) => n.nodeType === 3)
          .map((n) => n.textContent || '')
          .join('')
          .replace(/\\s+/g, ' ')
          .trim();
        if (own === 'Tải hình ảnh lên' || own === 'Upload photo' || own === 'Upload image') {
          const r = node.getBoundingClientRect();
          if (r.width > 2 && r.height > 2) {
            hits.push({x: r.x + r.width / 2, y: r.y + r.height / 2});
          }
        }
        for (const child of node.children || []) walk(child);
      }
      walk(document.body || document.documentElement);
      return hits[0] || null;
    }"""
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            hit = frame.evaluate(script)
        except Exception:
            continue
        if not hit:
            continue
        origin_x = 0.0
        origin_y = 0.0
        if frame != page.main_frame:
            try:
                box = frame.frame_element().bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            origin_x = box["x"]
            origin_y = box["y"]
        return origin_x + float(hit["x"]), origin_y + float(hit["y"])
    return None


def _upload_selfie_file(page: Page, photo: Path) -> bool:
    """Gắn ảnh vào input file. Chỉ bấm nút khi ô file chưa nhận ảnh."""
    _tag_selfie_inputs(page)
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            loc = frame.locator("[data-muse-selfie]")
            count = loc.count()
        except Exception:
            continue
        for i in range(count):
            try:
                _set_file_input(loc.nth(i), photo)
            except Exception:
                continue
    if _wait_selfie_preview(page, 5000):
        return True
    point = _upload_button_point(page)
    if point is None:
        return _selfie_preview_visible(page)
    try:
        with page.expect_file_chooser(timeout=6000) as chooser:
            page.mouse.click(point[0], point[1])
        chooser.value.set_files(str(photo))
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    return _wait_selfie_preview(page, 6000)


def _click_human_continue(page: Page) -> bool:
    """Bấm Tiếp tục khi nút đã sáng. Không bấm trên màn up ảnh khi ảnh chưa hiện."""
    if _selfie_upload_visible(page) and not _selfie_preview_visible(page):
        return False
    if _text_visible(page, UPLOAD_BTN) and not _selfie_preview_visible(page):
        return False
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            button = frame.get_by_role("button", name=re.compile(r"^Tiếp tục$|^Continue$"))
            if button.count() == 0 or not button.first.is_visible() or not button.first.is_enabled():
                continue
            if (button.first.get_attribute("aria-disabled") or "").lower() == "true":
                continue
            button.first.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def _drive_until_selfie(page: Page, on_progress: ProgressCallback | None) -> str | None:
    """Đi tiếp màn xác minh người thật tới lúc up ảnh. Không tự chọn ô hình captcha."""
    if not _human_verify_page(page):
        return None
    selfie_name = ""
    selfie_tries = 0
    reported_captcha = False
    for _ in range(120):
        if stop_requested():
            return None
        if _selfie_upload_visible(page):
            if not _selfie_preview_visible(page) and selfie_tries < 3:
                selfie_tries += 1
                photo = _random_selfie_path()
                if photo is None:
                    _emit(
                        on_progress,
                        step="needs_user",
                        detail="Không thấy ảnh trong D:\\Sefile. Chrome được giữ.",
                        final_url=page.url,
                    )
                    return "upload"
                _emit(
                    on_progress,
                    step="selfie",
                    detail=f"Up ảnh ngẫu nhiên {photo.name}",
                    final_url=page.url,
                )
                if _upload_selfie_file(page, photo):
                    selfie_name = photo.name
                    _emit(
                        on_progress,
                        step="selfie",
                        detail=f"Ảnh {photo.name} đã hiện trong khung.",
                        final_url=page.url,
                    )
                    page.wait_for_timeout(800)
                else:
                    page.wait_for_timeout(800)
                    continue
            if _selfie_preview_visible(page) and _click_human_continue(page):
                _emit(
                    on_progress,
                    step="selfie",
                    detail=f"Đã up {selfie_name or 'ảnh'} và bấm Tiếp tục",
                    final_url=page.url,
                )
                page.wait_for_timeout(2200)
                if not _selfie_upload_visible(page):
                    continue
                return "uploaded"
            if selfie_tries >= 3 and not _selfie_preview_visible(page):
                _emit(
                    on_progress,
                    step="needs_user",
                    detail="Đã tới bước up ảnh nhưng ảnh chưa hiện trong khung. Chrome được giữ, không bấm Tiếp tục.",
                    final_url=page.url,
                )
                return "upload"
            page.wait_for_timeout(1000)
            continue
        if _captcha_challenge_visible(page):
            if not reported_captcha:
                reported_captcha = True
                _emit(
                    on_progress,
                    step="needs_user",
                    detail="Đang captcha. Chrome được giữ tới khi qua, tool không tắt giữa chừng.",
                    final_url=page.url,
                )
            page.wait_for_timeout(1500)
            continue
        if _click_human_continue(page):
            _emit(on_progress, step="human_check", detail="Đã bấm Tiếp tục")
            page.wait_for_timeout(1800)
            continue
        if _text_visible(page, HUMAN_GATE) or "checkpoints" in (page.url or "") or _human_verify_page(page):
            if _click_human_continue(page):
                _emit(on_progress, step="human_check", detail="Captcha xong — bấm Tiếp tục")
            page.wait_for_timeout(1500)
            continue
        return None
    if _selfie_upload_visible(page):
        return "uploaded" if selfie_name else "upload"
    if _human_verify_page(page):
        return "captcha"
    return None


def _user_gate_detail(gate: str) -> str:
    if gate == "uploaded":
        return "Đã up ảnh ngẫu nhiên từ D:\\Sefile. Chrome được giữ."
    if gate == "upload":
        return "Chưa gắn được ảnh selfie từ D:\\Sefile. Chrome được giữ."
    return "Dừng ở captcha chọn hình. Tool không tự chọn ảnh. Chrome được giữ."


def _hold_browser(page: Page, browser) -> None:
    """Giữ Chrome cho tới khi người dùng tự tắt. Thoát hàm là Playwright đóng cửa sổ."""
    closed = {"v": False}

    def _on_closed() -> None:
        closed["v"] = True

    try:
        browser.on("disconnected", _on_closed)
    except Exception:
        pass
    while not closed["v"]:
        try:
            page.wait_for_timeout(5000)
        except Exception:
            break


def _select_recovery_code(page: Page) -> bool:
    """Trang xác nhận Meta: không bấm radio, chỉ bấm Tiếp."""
    for _ in range(3):
        _click_named_button(page, BTN_NEXT)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
        if not _confirm_screen_visible(page) or _visible_code_input(page) is not None:
            return True
    return False


def _visible_code_input(page: Page):
    selectors = (
        'input[autocomplete="one-time-code"]',
        'input[name="recovery_code"]',
        'input[inputmode="numeric"]',
        'input[type="tel"]',
        'input[type="text"]',
        "input:not([type])",
    )
    for frame in _frames(page):
        for selector in selectors:
            try:
                loc = frame.locator(selector)
                count = min(loc.count(), 6)
            except Exception:
                continue
            for index in range(count):
                item = loc.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    kind = item.evaluate(
                        """el => ((el.type || '') + ' ' + (el.placeholder || '') + ' ' + (el.name || '') + ' ' + (el.autocomplete || '')).toLowerCase()"""
                    )
                    if "email" in str(kind) or "di động" in str(kind) or "mobile" in str(kind):
                        continue
                    return item
                except Exception:
                    continue
    return None


def _recovery_code_screen(page: Page) -> bool:
    for frame in _frames(page):
        try:
            loc = frame.get_by_text(
                re.compile(
                    r"Nhập mã khôi phục|Enter your recovery code|Enter recovery code|authenticator|ứng dụng xác thực|authentication app",
                    re.I,
                )
            )
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _click_enabled_button(page: Page, pattern: re.Pattern[str]) -> bool:
    """Chỉ bấm nút đang sáng. Nút ẩn hoặc nút mờ phía trước không chặn nút thật."""
    for frame in _frames(page):
        groups = []
        try:
            groups.append(frame.get_by_role("button", name=pattern))
        except Exception:
            pass
        try:
            groups.append(frame.get_by_text(pattern))
        except Exception:
            pass
        for group in groups:
            try:
                total = min(group.count(), 8)
            except Exception:
                continue
            for index in range(total):
                button = group.nth(index)
                try:
                    if not button.is_visible() or not button.is_enabled():
                        continue
                    if (button.get_attribute("aria-disabled") or "").lower() == "true":
                        continue
                    button.click(timeout=5000)
                    return True
                except Exception:
                    continue
    return False


def _click_enabled_next(page: Page) -> bool:
    """Chỉ bấm Tiếp khi nút đã sáng. Nút mờ là form chưa nhận mã."""
    return _click_enabled_button(page, BTN_NEXT)


def _type_code(field, code: str) -> None:
    """Gõ từng số để React bật nút Tiếp. fill() chỉ hiện chữ, nút vẫn mờ."""
    try:
        field.click(timeout=3000)
    except Exception:
        pass
    try:
        field.fill("", force=True, timeout=3000)
    except Exception:
        pass
    field.press_sequentially(code, delay=70)
    try:
        field.evaluate(
            """(el, value) => {
              el.focus();
              const proto = Object.getPrototypeOf(el);
              const desc = Object.getOwnPropertyDescriptor(proto, 'value');
              if (desc && desc.set) desc.set.call(el, value);
              else el.value = value;
              el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            code,
        )
    except Exception:
        pass


def _submit_visible_code(page: Page, code: str) -> None:
    field = None
    for _ in range(24):
        field = _visible_code_input(page)
        if field is not None:
            break
        page.wait_for_timeout(500)
    if field is None:
        raise RuntimeError("Đã chọn Backup nhưng không thấy ô nhập mã.")
    _type_code(field, code)
    clicked = False
    for _ in range(16):
        if _click_enabled_next(page):
            clicked = True
            break
        page.wait_for_timeout(250)
    if not clicked:
        try:
            field.press("Enter")
        except Exception:
            page.keyboard.press("Enter")
    page.wait_for_timeout(1500)
    if _recovery_code_screen(page):
        _type_code(field, code)
        page.wait_for_timeout(400)
        if not _click_enabled_next(page):
            try:
                field.press("Enter")
            except Exception:
                page.keyboard.press("Enter")
        page.wait_for_timeout(1500)


def _enter_recovery_code(page: Page, totp_secret: str, on_progress: ProgressCallback | None) -> str:
    _emit(on_progress, step="fetch_2fa", detail="Lấy mã 2FA trên 2fa.live…")
    code = fetch_2fa_live_code(totp_secret)
    _emit(on_progress, step="enter_2fa", detail=f"Nhập mã 2FA {code}", code=code)
    _submit_visible_code(page, code)
    return code


def _installed_browser_channel() -> str | None:
    """Dùng Chrome hoặc Edge đã cài, vì bản Chromium của Playwright thường chưa được tải."""
    import os

    if os.name != "nt":
        return None
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    chrome = [
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
    ]
    edge = [
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
    ]
    if any(os.path.isfile(path) for path in chrome):
        return "chrome"
    if any(os.path.isfile(path) for path in edge):
        return "msedge"
    return None


def _browser_context_kwargs(proxy_cfg: dict[str, str] | None) -> dict:
    """Proxy Mỹ thì Chrome phải ra giờ, ngôn ngữ và vị trí Mỹ. Lệch giờ là Muse từ chối tạo."""
    kwargs: dict = {"viewport": {"width": 1280, "height": 900}}
    if proxy_cfg:
        kwargs["locale"] = "en-US"
        kwargs["timezone_id"] = "America/New_York"
        kwargs["geolocation"] = {"latitude": 40.7306, "longitude": -73.9352}
        kwargs["permissions"] = ["geolocation", "clipboard-read", "clipboard-write"]
    else:
        kwargs["locale"] = "vi-VN"
        kwargs["permissions"] = ["clipboard-read", "clipboard-write"]
    return kwargs


def _browser_launch_kwargs(proxy_cfg: dict[str, str] | None, *, headless: bool = False) -> dict:
    """Chrome thường, không bật cờ automation. Proxy gắn ngay lúc mở cửa sổ."""
    kwargs: dict = {
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-features=Translate",
            "--disable-blink-features=AutomationControlled",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ],
    }
    channel = _installed_browser_channel()
    if channel:
        kwargs["channel"] = channel
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    return kwargs


_HIDE_WEBDRIVER = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"


_BIRTHDAY_PAGE = re.compile(r"ngày sinh|birthday|date of birth|sinh nhật", re.I)


def _option_blob(select) -> str:
    try:
        return select.evaluate(
            """el => [...el.options].map(o => (o.value + ' ' + o.textContent).trim()).join('\\n')"""
        ) or ""
    except Exception:
        return ""


def _choose_option(select, wanted: list[str]) -> bool:
    try:
        options = select.locator("option")
        total = min(options.count(), 120)
    except Exception:
        return False
    for index in range(total):
        opt = options.nth(index)
        try:
            text = (opt.inner_text() or "").strip()
            value = (opt.get_attribute("value") or "").strip()
        except Exception:
            continue
        if text in wanted or value in wanted:
            try:
                select.select_option(value=value or None, label=text or None)
                return True
            except Exception:
                try:
                    select.select_option(value=value)
                    return True
                except Exception:
                    continue
    return False


def _select_kind(select) -> str:
    label = " ".join(
        filter(
            None,
            [
                select.get_attribute("aria-label") or "",
                select.get_attribute("name") or "",
                select.get_attribute("id") or "",
            ],
        )
    ).lower()
    if any(word in label for word in ("year", "năm")):
        return "year"
    if any(word in label for word in ("month", "tháng")):
        return "month"
    if any(word in label for word in ("day", "ngày")) and "tháng" not in label:
        return "day"
    blob = _option_blob(select).lower()
    if "1999" in blob or "2000" in blob:
        return "year"
    if "tháng 1" in blob or "january" in blob or "july" in blob:
        return "month"
    numbers: list[int] = []
    for line in blob.splitlines():
        token = (line.split(" ", 1)[0] if line else "").strip()
        if token.isdigit():
            numbers.append(int(token))
    if numbers and max(numbers) <= 12:
        return "month"
    if numbers and max(numbers) >= 28:
        return "day"
    return ""


def _input_blob(field) -> str:
    parts: list[str] = []
    for attr in ("aria-label", "placeholder", "name", "id", "autocomplete"):
        try:
            parts.append(field.get_attribute(attr) or "")
        except Exception:
            pass
    try:
        extra = field.evaluate(
            """el => {
              const bits = [];
              if (el.id) {
                const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (lab) bits.push(lab.innerText || '');
              }
              const prev = el.previousElementSibling;
              if (prev) bits.push((prev.innerText || prev.textContent || '').slice(0, 80));
              return bits.join(' ');
            }"""
        )
        parts.append(extra or "")
    except Exception:
        pass
    return " ".join(parts).lower()


def _name_kind(blob: str) -> str:
    compact = re.sub(r"\s+", " ", blob).strip()
    if re.search(r"email|mail|password|mật khẩu|mat khau|code|otp|phone|điện thoại|dien thoai", compact):
        return ""
    if re.search(r"given-name|firstname|first[-_ ]name|first_name", compact):
        return "first"
    if re.search(r"family-name|lastname|last[-_ ]name|last_name|surname", compact):
        return "last"
    if re.search(r"họ\s*(và\s*)?tên|ho va ten|ho ten|full[-_ ]name|your name", compact):
        return "full"
    tokens = set(re.findall(r"[a-zà-ỹ]+", compact))
    if "họ" in tokens and "tên" not in tokens:
        return "last"
    if "tên" in tokens and "họ" not in tokens:
        return "first"
    return ""


_GIVEN_NAMES = (
    "An", "Bao", "Chi", "Dung", "Giang", "Ha", "Hieu", "Hoa", "Hung", "Khanh",
    "Lan", "Linh", "Long", "Mai", "Minh", "Nam", "Ngoc", "Phong", "Quan", "Son",
    "Thao", "Trang", "Tuan", "Vy", "Yen",
)
_FAMILY_NAMES = (
    "Nguyen", "Tran", "Le", "Pham", "Hoang", "Huynh", "Phan", "Vu", "Vo", "Dang",
    "Bui", "Do", "Ngo", "Duong", "Ly",
)
_MONTH_LABELS = (
    [],
    ["1", "01", "Tháng 1", "January", "Jan"],
    ["2", "02", "Tháng 2", "February", "Feb"],
    ["3", "03", "Tháng 3", "March", "Mar"],
    ["4", "04", "Tháng 4", "April", "Apr"],
    ["5", "05", "Tháng 5", "May"],
    ["6", "06", "Tháng 6", "June", "Jun"],
    ["7", "07", "Tháng 7", "July", "Jul"],
    ["8", "08", "Tháng 8", "August", "Aug"],
    ["9", "09", "Tháng 9", "September", "Sep"],
    ["10", "10", "Tháng 10", "October", "Oct"],
    ["11", "11", "Tháng 11", "November", "Nov"],
    ["12", "12", "Tháng 12", "December", "Dec"],
)


def _shift_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def _random_person() -> dict[str, str]:
    given = random.choice(_GIVEN_NAMES)
    family = random.choice(_FAMILY_NAMES)
    return {"first": given, "last": family, "full": f"{family} {given}"}


def _random_birthday() -> dict[str, list[str] | str]:
    """Ngày sinh ngẫu nhiên, đủ 18 tuổi trở lên."""
    today = date.today()
    oldest = _shift_years(today, 45)
    youngest = _shift_years(today, 18)
    born = oldest + timedelta(days=random.randint(0, (youngest - oldest).days))
    return {
        "day": [str(born.day), f"{born.day:02d}"],
        "month": list(_MONTH_LABELS[born.month]),
        "year": [str(born.year)],
        "label": f"{born.day:02d}/{born.month:02d}/{born.year}",
    }


def _fill_name(page: Page, person: dict[str, str] | None = None) -> bool:
    """Điền tên ngẫu nhiên. Không thấy ô họ tên thì bỏ qua."""
    targets = person or _random_person()
    filled = {"first": False, "last": False, "full": False}
    pending: list[tuple[object, str]] = []
    for frame in _frames(page):
        try:
            inputs = frame.locator("input")
            total = min(inputs.count(), 40)
        except Exception:
            continue
        for index in range(total):
            field = inputs.nth(index)
            try:
                if not field.is_visible():
                    continue
                input_type = (field.get_attribute("type") or "text").lower()
                if input_type in ("hidden", "password", "email", "checkbox", "radio", "file", "submit", "button"):
                    continue
            except Exception:
                continue
            kind = _name_kind(_input_blob(field))
            if not kind or filled[kind]:
                continue
            filled[kind] = True
            pending.append((field, kind))
    if filled["first"] or filled["last"]:
        pending = [(field, kind) for field, kind in pending if kind != "full"]
    wrote = False
    for field, kind in pending:
        value = targets[kind]
        try:
            current = (field.input_value() or "").strip()
        except Exception:
            current = ""
        if current.lower() == value.lower():
            wrote = True
            continue
        try:
            field.click(timeout=2000)
        except Exception:
            pass
        field.fill(value)
        wrote = True
    return wrote


def _has_visible_select(page: Page) -> bool:
    for frame in _frames(page):
        try:
            selects = frame.locator("select")
            total = min(selects.count(), 8)
        except Exception:
            continue
        for index in range(total):
            try:
                if selects.nth(index).is_visible():
                    return True
            except Exception:
                continue
    return False


def _fill_birthday(
    page: Page,
    person: dict[str, str] | None = None,
    birthday: dict[str, list[str] | str] | None = None,
) -> None:
    """Ngày sinh ngẫu nhiên, đủ 18 tuổi. Ô họ tên trên cùng trang thì điền luôn."""
    person = person or _random_person()
    birthday = birthday or _random_birthday()
    deadline = time.time() + 25
    while time.time() < deadline and not _text_visible(page, _BIRTHDAY_PAGE):
        _fill_name(page, person)
        page.wait_for_timeout(400)
    if not _text_visible(page, _BIRTHDAY_PAGE):
        raise RuntimeError("Đã nhập mã nhưng chưa thấy trang ngày sinh")
    _fill_name(page, person)
    wanted = {
        "day": list(birthday["day"]),
        "month": list(birthday["month"]),
        "year": list(birthday["year"]),
    }
    filled = {"day": False, "month": False, "year": False}
    for frame in _frames(page):
        try:
            selects = frame.locator("select")
            total = selects.count()
        except Exception:
            continue
        for index in range(total):
            select = selects.nth(index)
            try:
                if not select.is_visible():
                    continue
            except Exception:
                continue
            kind = _select_kind(select)
            if not kind or filled[kind]:
                continue
            if _choose_option(select, wanted[kind]):
                filled[kind] = True
    missing = [name for name, ok in filled.items() if not ok]
    if missing:
        raise RuntimeError(f"Chưa điền được ngày sinh {birthday['label']}")
    page.wait_for_timeout(1500)
    clicked = False
    for _ in range(6):
        if _click_enabled_button(page, SIGNUP_SUBMIT):
            clicked = True
            break
        page.wait_for_timeout(400)
    if not clicked and not _click_named_button(page, SIGNUP_SUBMIT):
        raise RuntimeError("Đã điền ngày sinh nhưng chưa bấm được Xác nhận")


def _fill_account_password(page: Page, password: str) -> bool:
    """Điền mật khẩu Muse nếu trang có ô password."""
    if not password:
        return False
    wrote = False
    for frame in _frames(page):
        try:
            fields = frame.locator("input[type='password']")
            total = min(fields.count(), 4)
        except Exception:
            continue
        for index in range(total):
            field = fields.nth(index)
            try:
                if not field.is_visible():
                    continue
            except Exception:
                continue
            try:
                current = field.input_value() or ""
            except Exception:
                current = ""
            if current == password:
                wrote = True
                continue
            try:
                field.click(timeout=2000)
            except Exception:
                pass
            field.fill(password)
            wrote = True
    return wrote


def _click_use_password_instead(page: Page) -> bool:
    """Bấm link 'Use password instead' trên màn nhập code của Muse."""
    patterns = [
        re.compile(r"Use password instead", re.I),
        re.compile(r"Dùng mật khẩu", re.I),
        re.compile(r"Use password", re.I),
        re.compile(r"mật khẩu", re.I),
    ]
    for pattern in patterns:
        for frame in _frames(page):
            try:
                link = frame.get_by_role("link", name=pattern)
                if link.count() > 0 and link.first.is_visible():
                    link.first.click(force=True, timeout=5000)
                    return True
            except Exception:
                pass
            try:
                text = frame.get_by_text(pattern)
                if text.count() > 0 and text.first.is_visible():
                    text.first.click(force=True, timeout=5000)
                    return True
            except Exception:
                continue
            try:
                button = frame.get_by_role("button", name=pattern)
                if button.count() > 0 and button.first.is_visible():
                    button.first.click(force=True, timeout=5000)
                    return True
            except Exception:
                continue
    return False


def _email_screen_visible(page: Page) -> bool:
    try:
        box = page.get_by_placeholder(EMAIL_PLACEHOLDER)
        return box.count() > 0 and box.first.is_visible()
    except Exception:
        return False


def _commit_email(field, email: str) -> None:
    """Đưa email vào state của React khi gõ phím không làm nút sáng."""
    field.evaluate(
        """(el, value) => {
            el.focus();
            const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
            if (proto && proto.set) proto.set.call(el, value);
            else el.value = value;
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        email,
    )


def _email_value(page: Page) -> str:
    try:
        return (page.get_by_placeholder(EMAIL_PLACEHOLDER).first.input_value() or "").strip()
    except Exception:
        return ""


def _mark_email_field(page: Page) -> str:
    """Đánh dấu ô đang hiện. Muse hydrate sẽ thay ô này bằng ô mới."""
    field = page.get_by_placeholder(EMAIL_PLACEHOLDER).first
    return field.evaluate(
        """el => {
            if (!el.dataset.museEmail) el.dataset.museEmail = String(Date.now());
            return el.dataset.museEmail;
        }"""
    )


def _wait_email_form_settled(page: Page) -> None:
    """Chờ ô email và nút Tiếp tục giữ nguyên sau khi trang load xong."""
    page.get_by_placeholder(EMAIL_PLACEHOLDER).first.wait_for(state="visible", timeout=45000)
    stable = 0
    last_id = ""
    for _ in range(40):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        try:
            field = page.get_by_placeholder(EMAIL_PLACEHOLDER).first
            if not field.is_visible():
                stable = 0
                last_id = ""
                page.wait_for_timeout(300)
                continue
            node_id = _mark_email_field(page)
            button = page.get_by_role("button", name=BTN_EMAIL_NEXT).first
            button_ready = button.is_visible()
        except Exception:
            stable = 0
            last_id = ""
            page.wait_for_timeout(300)
            continue
        if button_ready and node_id and node_id == last_id:
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
        last_id = node_id
        page.wait_for_timeout(400)
    if not _email_screen_visible(page):
        raise RuntimeError("Trang Muse chưa hiện ô email")


def _type_email_once(page: Page, email: str) -> bool:
    field = page.get_by_placeholder(EMAIL_PLACEHOLDER).first
    try:
        field.click(timeout=3000)
    except Exception:
        pass
    try:
        field.fill("")
    except Exception:
        pass
    try:
        field.press_sequentially(email, delay=35)
    except Exception:
        return False
    if email.lower() not in _email_value(page).lower():
        try:
            _commit_email(field, email)
        except Exception:
            return False
    return email.lower() in _email_value(page).lower()


def _submit_signup_email(page: Page, email: str) -> None:
    """Chờ Muse load xong rồi mới gõ. Trang vẽ lại làm mất chữ thì gõ lại."""
    _wait_email_form_settled(page)
    kept = False
    for _ in range(4):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if not _type_email_once(page, email):
            page.wait_for_timeout(500)
            continue
        page.wait_for_timeout(900)
        if email.lower() in _email_value(page).lower():
            kept = True
            break
    if not kept:
        raise RuntimeError("Trang Muse load lại và làm mất email")
    clicked = False
    for _ in range(25):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if email.lower() not in _email_value(page).lower():
            if not _type_email_once(page, email):
                page.wait_for_timeout(400)
                continue
        if _click_enabled_button(page, BTN_EMAIL_NEXT):
            clicked = True
            break
        page.wait_for_timeout(400)
    if not clicked:
        try:
            field = page.get_by_placeholder(EMAIL_PLACEHOLDER).first
            field.click(timeout=2000)
            field.press("Enter")
            page.wait_for_timeout(1000)
        except Exception:
            pass
        if _email_screen_visible(page) and not _click_enabled_button(page, BTN_EMAIL_NEXT):
            raise RuntimeError("Đã nhập email nhưng chưa bấm được Tiếp tục")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=45000)
    except Exception:
        pass


def _input_label(field) -> str:
    parts = []
    for attr in ("placeholder", "aria-label", "name", "id", "autocomplete"):
        try:
            parts.append(field.get_attribute(attr) or "")
        except Exception:
            continue
    return " ".join(parts).lower()


def _find_labeled_input(page: Page, pattern: re.Pattern[str]):
    for frame in _frames(page):
        try:
            fields = frame.locator("input")
            total = min(fields.count(), 20)
        except Exception:
            continue
        for index in range(total):
            field = fields.nth(index)
            try:
                if not field.is_visible():
                    continue
                input_type = (field.get_attribute("type") or "text").lower()
                if input_type in ("hidden", "checkbox", "radio", "file", "submit", "button"):
                    continue
            except Exception:
                continue
            if pattern.search(_input_label(field)):
                return field
    return None


def _field_digits(field) -> str:
    try:
        return "".join(ch for ch in (field.input_value() or "") if ch.isdigit())
    except Exception:
        return ""


def _type_field(field, text: str) -> None:
    try:
        field.click(timeout=3000)
    except Exception:
        pass
    try:
        field.fill("")
    except Exception:
        pass
    field.press_sequentially(text, delay=30)


def _placeholder_input(page: Page, pattern: re.Pattern[str]):
    for frame in _frames(page):
        try:
            fields = frame.get_by_placeholder(pattern)
            total = min(fields.count(), 6)
        except Exception:
            continue
        for index in range(total):
            field = fields.nth(index)
            try:
                if field.is_visible():
                    return field
            except Exception:
                continue
    return _find_labeled_input(page, pattern)


def _card_form_visible(page: Page) -> bool:
    if _text_visible(page, CARD_PAGE):
        return True
    number = _placeholder_input(page, re.compile(r"số thẻ|card number", re.I))
    cvv = _placeholder_input(page, re.compile(r"^cvv$|^cvc$|security code", re.I))
    return number is not None and cvv is not None


def _select_united_states(page: Page) -> None:
    wanted = ["Hoa Kỳ", "United States", "US", "USA"]
    for frame in _frames(page):
        try:
            selects = frame.locator("select")
            total = min(selects.count(), 6)
        except Exception:
            continue
        for index in range(total):
            select = selects.nth(index)
            try:
                if not select.is_visible():
                    continue
            except Exception:
                continue
            if _choose_option(select, wanted):
                return


def _fill_age_card(page: Page, card: dict) -> None:
    number_field = _placeholder_input(page, re.compile(r"số thẻ|card number|cc-number|cardnumber", re.I))
    expiry_field = _placeholder_input(page, re.compile(r"mm\s*/\s*yy|expiry|expiration|cc-exp|hết hạn", re.I))
    cvv_field = _placeholder_input(page, re.compile(r"cvv|cvc|security code|cc-csc", re.I))
    if number_field is None or expiry_field is None or cvv_field is None:
        raise RuntimeError("Thấy trang thẻ nhưng chưa thấy đủ ô số thẻ, ngày, CVV")
    _type_field(number_field, card.get("number") or "")
    _type_field(expiry_field, card.get("expiry") or "")
    _type_field(cvv_field, card.get("cvv") or "")
    page.wait_for_timeout(300)
    last4 = (card.get("number") or "")[-4:]
    if last4 and last4 not in _field_digits(number_field):
        _type_field(number_field, card.get("number") or "")
        _type_field(expiry_field, card.get("expiry") or "")
        _type_field(cvv_field, card.get("cvv") or "")
    _select_united_states(page)
    zip_field = _placeholder_input(page, re.compile(r"zip|postal|mã zip", re.I))
    if zip_field is None:
        raise RuntimeError("Chưa thấy ô mã ZIP")
    _type_field(zip_field, CARD_ZIP)


def _submit_age_card(page: Page, on_progress: ProgressCallback | None) -> None:
    """Điền thẻ trong kho. Muse báo lỗi thì đánh dấu kho đó và sang kho kế tiếp."""
    from warehouse_store import mark_card_warehouse_bad, usable_card_warehouses

    cards = usable_card_warehouses()
    if not cards:
        raise RuntimeError("Đã tới bước thẻ nhưng kho thẻ đang trống")
    for card in cards:
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if not _card_form_visible(page):
            return
        last4 = (card.get("number") or "")[-4:]
        _emit(on_progress, step="card", detail=f"Lấy {card.get('name')} trong kho thẻ · ****{last4}")
        _fill_age_card(page, card)
        if not _click_enabled_button(page, CARD_CONFIRM):
            raise RuntimeError(f"Đã điền {card.get('name')} nhưng chưa bấm được Xác nhận")
        failed = False
        for tick in range(40):
            if stop_requested():
                raise RuntimeError("Đã dừng")
            if _text_visible(page, DISCLOSURE_PAGE) or "access/disclosure" in (page.url or ""):
                _emit(on_progress, step="card", detail=f"Đã qua bước thẻ bằng {card.get('name')}")
                _ack_disclosure(page, on_progress)
                return
            if not _card_form_visible(page):
                _emit(on_progress, step="card", detail=f"Đã qua bước thẻ bằng {card.get('name')}")
                _ack_disclosure(page, on_progress)
                return
            if _text_visible(page, CARD_BUSY):
                _click_named_button(page, re.compile(r"^OK$|^Đồng ý$"))
                raise RuntimeError(
                    f"Muse báo quá nhiều lần thử với {card.get('name')} · ****{last4}. Thẻ vẫn giữ trong kho, chờ rồi tạo lại, không bấm Xác nhận tiếp."
                )
            if _text_visible(page, CARD_BAD):
                failed = True
                break
            if tick in (8, 16, 24, 32):
                number_field = _placeholder_input(page, re.compile(r"số thẻ|card number|cc-number|cardnumber", re.I))
                digits = _field_digits(number_field) if number_field is not None else ""
                if last4 and last4 not in digits:
                    _emit(on_progress, step="card", detail=f"Trang làm mất số thẻ, điền lại {card.get('name')}")
                    _fill_age_card(page, card)
                else:
                    _emit(on_progress, step="card", detail="Trang thẻ chưa chuyển, bấm lại Xác nhận")
                _click_enabled_button(page, CARD_CONFIRM)
            page.wait_for_timeout(750)
        if not failed:
            raise RuntimeError(f"Đã điền {card.get('name')} nhưng trang xác nhận tuổi chưa chuyển")
        mark_card_warehouse_bad(card.get("id") or "")
        _emit(on_progress, step="card", detail=f"{card.get('name')} bị Muse báo lỗi, chuyển thẻ kho tiếp theo")
    raise RuntimeError("Các thẻ trong kho đều bị Muse báo lỗi")


def _click_label(page: Page, pattern: re.Pattern[str]) -> bool:
    if _click_enabled_button(page, pattern):
        return True
    return _click_named_button(page, pattern)


def _alert_text(page: Page) -> str:
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            alert = frame.locator("[role='alert']")
            total = min(alert.count(), 4)
        except Exception:
            continue
        for index in range(total):
            item = alert.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = (item.inner_text() or "").strip()
            except Exception:
                continue
            if text:
                return text
    return ""


def _click_bottom_left_menu(page: Page) -> bool:
    size = page.viewport_size or {"width": 1280, "height": 900}
    for frame in _frames(page):
        try:
            buttons = frame.get_by_role("button")
            total = min(buttons.count(), 40)
        except Exception:
            continue
        best = None
        best_score = None
        for index in range(total):
            button = buttons.nth(index)
            try:
                if not button.is_visible():
                    continue
                box = button.bounding_box()
            except Exception:
                continue
            if not box or box["width"] < 8 or box["height"] < 8:
                continue
            if box["y"] < size["height"] * 0.62 or box["x"] > 180:
                continue
            score = (box["y"], -box["x"])
            if best_score is None or score > best_score:
                best = button
                best_score = score
        if best is not None:
            try:
                best.click(timeout=4000)
                return True
            except Exception:
                continue
    return False


def _find_invite_input(page: Page):
    field = _find_labeled_input(page, re.compile(r"invite code|mã lời mời|mã mời|referral code"))
    if field is not None:
        return field
    for frame in _frames(page):
        try:
            loc = frame.locator(
                "input[aria-label*='invite' i], input[aria-label*='mã lời' i], input[aria-label*='mã mời' i], "
                "input[placeholder*='invite' i], input[placeholder*='code' i]"
            )
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    # Trang Quy đổi đã mở nhưng ô nhập không có nhãn nhận diện được: lấy ô text duy nhất đang hiện.
    if _text_visible(page, REDEEM_ROW):
        for frame in _frames(page):
            try:
                fields = frame.locator(
                    "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
                    ":not([type='file']):not([type='submit']):not([type='button'])"
                )
                visible = []
                total = min(fields.count(), 10)
                for index in range(total):
                    item = fields.nth(index)
                    try:
                        if item.is_visible():
                            visible.append(item)
                    except Exception:
                        continue
                if len(visible) == 1:
                    return visible[0]
            except Exception:
                continue
    return None


def _type_invite_code(field, code: str) -> None:
    try:
        field.click(timeout=2000, force=True)
    except Exception:
        try:
            field.focus()
        except Exception:
            pass
    try:
        field.fill("")
    except Exception:
        pass
    field.press_sequentially(code, delay=40)


def _pick_invite_code(text: str, email: str) -> str:
    local = (email or "").split("@", 1)[0].upper()

    def ok(code: str) -> bool:
        if not code or code == local:
            return False
        if code.startswith("HAN") and code[3:].isdigit():
            return False
        return True

    lines = []
    for line in (text or "").upper().splitlines():
        line = line.strip()
        if INVITE_CODE_RE.fullmatch(line) and ok(line):
            lines.append(line)
    found = lines or [code for code in INVITE_CODE_RE.findall((text or "").upper()) if ok(code)]
    if not found:
        return ""
    found.sort(key=lambda code: (0 if 6 <= len(code) <= 8 else 1, len(code)))
    return found[0]


def _page_body_text(page: Page) -> str:
    parts = []
    for frame in _frames(page):
        if "recaptcha" in (frame.url or "").lower():
            continue
        try:
            parts.append(frame.locator("body").inner_text(timeout=2000))
        except Exception:
            continue
    return "\n".join(parts)


def _muse_app_visible(page: Page) -> bool:
    """Tài khoản đã vào trong Muse, không còn trang Bắt đầu hay trang thẻ."""
    if _text_visible(page, DISCLOSURE_PAGE) or "access/disclosure" in (page.url or ""):
        return False
    if _card_form_visible(page) or _email_screen_visible(page):
        return False
    if _text_visible(page, INVITE_OPEN) or _text_visible(page, SETTINGS_NAME) or _text_visible(page, REDEEM_ROW):
        return True
    url = page.url or ""
    if "muse.ai" not in url or "/access/" in url:
        return False
    for frame in _frames(page):
        try:
            box = frame.locator("textarea, [contenteditable='true']")
            if box.count() > 0 and box.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _wait_inside_muse(page: Page, on_progress: ProgressCallback | None) -> None:
    """Bấm Bắt đầu rồi chờ đúng tới khi tài khoản Muse hiện ra. Chưa hiện thì không đóng Chrome."""
    announced = False
    for tick in range(120):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if _captcha_visible(page):
            _emit(on_progress, step="invite", detail="Đang chờ captcha, Chrome vẫn mở")
            page.wait_for_timeout(2000)
            continue
        if _card_form_visible(page):
            _submit_age_card(page, on_progress)
            continue
        if _text_visible(page, DISCLOSURE_PAGE) or "access/disclosure" in (page.url or ""):
            if not announced:
                _emit(on_progress, step="disclosure", detail="Đã tới Bắt đầu, chờ tài khoản Muse hiện ra")
                announced = True
            _click_enabled_button(page, BTN_START)
            page.wait_for_timeout(800)
            continue
        if _muse_app_visible(page):
            _emit(on_progress, step="inside", detail="Tài khoản Muse đã hiện")
            return
        if tick == 4:
            _emit(on_progress, step="inside", detail="Chờ tài khoản Muse hiện ra, chưa đóng Chrome")
        page.wait_for_timeout(1000)
    raise RuntimeError("Đã bấm Bắt đầu nhưng tài khoản Muse chưa hiện")


_INVITE_BOX_JS = r"""() => {
  const buttons = [...document.querySelectorAll('button, [role="button"]')];
  const btn = buttons.find((el) => /copy referral link|sao chép liên kết/i.test((el.innerText || el.textContent || '').trim()));
  let node = btn || null;
  if (!node) {
    const heads = [...document.querySelectorAll('h1,h2,h3,div,section,aside')];
    node = heads.find((el) => /invite friends|mời bạn bè/i.test((el.innerText || '').slice(0, 120))) || null;
  }
  for (let i = 0; i < 8 && node; i++) {
    const lines = (node.innerText || '').split(/\n/).map((s) => s.trim()).filter(Boolean);
    const code = lines.find((line) => /^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{6,8}$/.test(line));
    if (code) return code;
    node = node.parentElement;
  }
  return '';
}"""


def _invite_panel_open(page: Page) -> bool:
    return _text_visible(page, INVITE_PANEL) or _text_visible(page, COPY_CODE)


def _read_invite_box(page: Page, email: str) -> str:
    for frame in _frames(page):
        try:
            found = frame.evaluate(_INVITE_BOX_JS) or ""
        except Exception:
            continue
        code = _pick_invite_code(str(found), email)
        if code:
            return code
    return ""


def _clipboard_invite(page: Page, email: str) -> str:
    try:
        copied = page.evaluate("() => navigator.clipboard.readText()") or ""
    except Exception:
        copied = ""
    return _pick_invite_code(str(copied), email)


def _harvest_invite_code(page: Page, email: str, on_progress: ProgressCallback | None) -> str:
    """Mã trong khung Invite, ô như 23IVJJ, cạnh nút Copy referral link."""
    _emit(on_progress, step="invite", detail="Mở Invite và lấy mã giới thiệu")
    for _ in range(24):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if not _invite_panel_open(page):
            if not _click_enabled_button(page, INVITE_OPEN):
                _click_label(page, INVITE_OPEN)
            page.wait_for_timeout(700)
        _click_label(page, REVEAL_CODE)
        code = _read_invite_box(page, email)
        if not code and _invite_panel_open(page) and _click_enabled_button(page, COPY_CODE):
            page.wait_for_timeout(400)
            code = _clipboard_invite(page, email)
        if code:
            _emit(on_progress, step="invite", detail=f"Mã giới thiệu {code}")
            return code
        page.wait_for_timeout(700)
    raise RuntimeError("Đã vào Muse nhưng chưa trích xuất được mã giới thiệu")


_REDEEM_ROW_JS = r"""() => {
  const needles = ['redeem invite code', 'quy đổi mã lời mời', 'have an invite code'];
  let best = null;
  let bestDepth = -1;
  for (const el of document.querySelectorAll('body *')) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (!t || t.length > 80) continue;
    if (!needles.some((n) => t.includes(n))) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    let depth = 0;
    let node = el;
    while (node && node !== document.body) { depth++; node = node.parentElement; }
    if (depth > bestDepth) { best = el; bestDepth = depth; }
  }
  if (!best) return false;
  best.click();
  return true;
}"""


def _click_redeem_row(page: Page) -> bool:
    """Bấm dòng Quy đổi mã lời mời. Dòng này có thể chỉ là div bấm được,
    không phải nút, nên bấm thường không trúng thì bấm bằng JS vào đúng dòng đó."""
    if _click_label(page, REDEEM_ROW):
        return True
    for frame in _frames(page):
        try:
            if frame.evaluate(_REDEEM_ROW_JS):
                return True
        except Exception:
            continue
    return False


def _open_redeem(page: Page, on_progress: ProgressCallback | None = None) -> None:
    for _ in range(16):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if _find_invite_input(page) is not None:
            return
        if _click_redeem_row(page):
            _emit(on_progress, step="invite", detail="Đã bấm Quy đổi mã lời mời, chờ ô nhập mã…")
            page.wait_for_timeout(800)
            continue
        if _text_visible(page, SETTINGS_NAME):
            # Cài đặt đã mở nhưng chưa thấy dòng Quy đổi: về General rồi thử lại.
            if _click_label(page, GENERAL_NAME):
                page.wait_for_timeout(500)
                continue
        if _click_label(page, SETTINGS_NAME):
            _emit(on_progress, step="invite", detail="Đã mở Cài đặt…")
            page.wait_for_timeout(500)
            continue
        if not _click_label(page, MENU_BTN):
            _click_bottom_left_menu(page)
        page.wait_for_timeout(500)
    raise RuntimeError("Đã vào Muse nhưng chưa thấy Quy đổi mã lời mời")


def _redeem_invite(page: Page, code: str, on_progress: ProgressCallback | None) -> None:
    _emit(on_progress, step="invite", detail=f"Vào Cài đặt, quy đổi mã {code}")
    _open_redeem(page, on_progress)
    field = _find_invite_input(page)
    if field is None:
        raise RuntimeError("Đã mở Quy đổi mã lời mời nhưng chưa thấy ô nhập mã")
    _type_invite_code(field, code)
    page.wait_for_timeout(400)
    _click_enabled_button(page, REDEEM_OK)
    for tick in range(16):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        err = _alert_text(page)
        if err:
            raise RuntimeError(f"Muse không nhận mã {code}: {err}")
        if _text_visible(page, INVITE_OK):
            _emit(on_progress, step="invite", detail=f"Đã quy đổi mã {code}")
            return
        if tick >= 4 and _find_invite_input(page) is None:
            # Ô nhập đã biến mất, không có lỗi: trang đã quy đổi xong và quay lại.
            _emit(on_progress, step="invite", detail=f"Đã quy đổi mã {code}")
            return
        page.wait_for_timeout(500)
    raise RuntimeError(f"Đã nhập mã {code} nhưng chưa thấy Muse xác nhận quy đổi")


def _save_signup_cookies(context, page: Page, email: str, on_progress: ProgressCallback | None) -> LoginProgress:
    """Giống kho đăng nhập: có cookie phiên Muse thì mới lưu, chưa có thì chưa đóng Chrome."""
    _emit(on_progress, step="save_cookies", detail=f"Lưu cookie {email}")
    status_header = ""
    for attempt in range(8):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if attempt == 2:
            status_header = _refresh_and_get_status_cookie(page) or ""
        raw = context.cookies()
        browser_header = cookies_to_header(filter_muse_cookies(raw))
        header = status_header.strip() or browser_header
        if header and (muse_token_from_cookies(raw, header) or "hatch_sess=" in header or browser_header):
            saved = _capture_and_save_cookies(
                context,
                email,
                status_cookie_header=header,
                page=page,
            )
            if saved.cookie_header:
                return saved
        page.wait_for_timeout(1000)
    raise RuntimeError("Đã vào Muse nhưng chưa lấy được cookie")


def _chain_invites(
    page: Page,
    email: str,
    on_progress: ProgressCallback | None,
    take_prior: Callable[[], str | None] | None,
    on_invite: Callable[[str | None], None] | None,
    context=None,
) -> tuple[str, str]:
    """Thành công khi mã giới thiệu đã vào tool. Sau đó mới lưu cookie."""
    _wait_inside_muse(page, on_progress)
    own = _harvest_invite_code(page, email, on_progress)
    if not own:
        raise RuntimeError("Chưa trích xuất được mã giới thiệu vào tool")
    if on_invite is not None:
        on_invite(own)
    _emit(on_progress, step="invite", detail=f"Đã lưu mã giới thiệu {own} vào tool")
    if context is None:
        raise RuntimeError(f"Đã lưu mã giới thiệu {own} nhưng chưa lấy được cookie")
    try:
        saved = _save_signup_cookies(context, page, email, on_progress)
    except Exception as exc:
        if stop_requested() or str(exc) == "Đã dừng":
            raise
        raise RuntimeError(f"Đã lưu mã giới thiệu {own} vào tool nhưng chưa lấy được cookie") from exc
    _emit(on_progress, **saved.__dict__)
    prior = take_prior() if take_prior is not None else None
    if stop_requested():
        raise RuntimeError("Đã dừng")
    if prior and prior != own:
        try:
            _redeem_invite(page, prior, on_progress)
        except Exception as exc:
            if stop_requested() or str(exc) == "Đã dừng":
                raise
            note = f"Mã {own} và cookie đã lưu. Chưa quy đổi được mã trước: {exc}"
            _emit(on_progress, step="invite", detail=note)
            return own, note
    elif not prior:
        _emit(on_progress, step="invite", detail="Tài khoản đầu chuỗi, không nhập mã mời")
    return own, ""


def _ack_disclosure(page: Page, on_progress: ProgressCallback | None) -> None:
    """Sau khi thẻ được nhận, trang muse.ai/access/disclosure chỉ cần bấm Bắt đầu."""
    saw = False
    for _ in range(16):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        on_disclosure = _text_visible(page, DISCLOSURE_PAGE) or "access/disclosure" in (page.url or "")
        if on_disclosure:
            saw = True
            if _click_enabled_button(page, BTN_START):
                _emit(on_progress, step="disclosure", detail="Đã bấm Bắt đầu")
                page.wait_for_timeout(800)
                return
        page.wait_for_timeout(500)
    if saw:
        raise RuntimeError("Thấy trang Một số điều cần biết nhưng chưa bấm được Bắt đầu")


def _resolve_after_birthday(
    page: Page,
    on_progress: ProgressCallback | None,
) -> None:
    for tick in range(60):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if _text_visible(page, SIGNUP_BLOCKED):
            page.wait_for_timeout(2000)
            if _text_visible(page, SIGNUP_BLOCKED):
                raise RuntimeError(
                    "Muse chặn tạo tài khoản mới (You can't create a new account right now)."
                    " Không phải lỗi mail — thử proxy khác hoặc tạo 1 TK/lần."
                )
        if _card_form_visible(page):
            _emit(on_progress, step="card", detail="Thấy trang thẻ, lấy thẻ trong kho để điền")
            _submit_age_card(page, on_progress)
            return
        if tick == 6:
            _emit(on_progress, step="card", detail="Chờ trang xác nhận tuổi để điền thẻ")
        page.wait_for_timeout(500)
    if _card_form_visible(page):
        _submit_age_card(page, on_progress)


def create_muse_account(
    email: str,
    mail_token: str,
    *,
    account_password: str | None = None,
    proxy: str | None = None,
    keep_open: bool = True,
    on_progress: ProgressCallback | None = None,
    take_prior: Callable[[], str | None] | None = None,
    on_invite: Callable[[str | None], None] | None = None,
    prepare_mail: Callable[[], tuple[str, str, str | None]] | None = None,
) -> LoginProgress:
    """Chrome mới, nhận proxy. Mail tạo sau bước mở Chrome."""
    from tempmail_plus_client import TempMailPlusError, wait_for_code

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return LoginProgress(step="error", error=f"Chưa cài Playwright: {exc}")

    proxy_cfg = parse_proxy(proxy) if (proxy or "").strip() else None
    if (proxy or "").strip() and not proxy_cfg:
        err = LoginProgress(step="error", error="Proxy không đúng dạng ip:port hoặc ip:port:user:pass")
        _emit(on_progress, **err.__dict__)
        return err
    _wait_signup_turn(on_progress)
    _emit(
        on_progress,
        step="starting",
        detail=f"Mở Chrome mới qua proxy {proxy_cfg['server']}" if proxy_cfg else "Mở Chrome mới…",
    )
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**_browser_launch_kwargs(proxy_cfg, headless=False))
        except Exception as exc:
            message = str(exc).split("╔", 1)[0].strip()
            err = LoginProgress(step="error", error=message or "Không mở được Chrome")
            _emit(on_progress, **err.__dict__)
            return err
        context = browser.new_context(**_browser_context_kwargs(proxy_cfg))
        context.add_init_script(_HIDE_WEBDRIVER)
        try:
            context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            if prepare_mail is not None:
                email, mail_token, prepared_password = prepare_mail()
                if prepared_password:
                    account_password = prepared_password
                _emit(
                    on_progress,
                    step="mail",
                    detail=f"Hộp thư {email} đọc lại được. Bắt đầu tạo tài khoản.",
                )
            if not email or not mail_token:
                raise RuntimeError("Chưa có mail đăng nhập được, chưa tạo tài khoản")
            _emit(on_progress, step="open_muse", detail="Tải muse.ai…")
            page.goto(MUSE_URL, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("load", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            _emit(on_progress, step="email", detail=f"Chờ Muse load xong rồi nhập {email}")
            _submit_signup_email(page, email)
            _emit(on_progress, step="wait_code", detail="Chờ mã trong tempmail.plus…")
            field = None
            password_sent = False
            for _ in range(40):
                if stop_requested():
                    raise RuntimeError("Đã dừng")
                if _text_visible(page, MAIL_FLAGGED):
                    raise RuntimeError(
                        "Muse không nhận mail này. Có thể @mailto.plus bị đánh spam."
                    )
                if _email_screen_visible(page):
                    if email.lower() not in _email_value(page).lower():
                        _type_email_once(page, email)
                    _click_enabled_button(page, BTN_EMAIL_NEXT)
                    page.wait_for_timeout(500)
                    continue
                if account_password and _fill_account_password(page, account_password) and not password_sent:
                    password_sent = True
                    _emit(on_progress, step="password", detail="Điền mật khẩu tài khoản")
                    page.wait_for_timeout(400)
                    if _visible_code_input(page) is None and not _text_visible(page, _BIRTHDAY_PAGE):
                        if not _click_enabled_next(page):
                            _click_enabled_button(page, BTN_EMAIL_NEXT)
                field = _visible_code_input(page)
                if field is not None:
                    break
                page.wait_for_timeout(500)
            if field is None:
                raise RuntimeError("Chưa thấy ô nhập mã")
            code = wait_for_code(mail_token, timeout=120)
            _emit(on_progress, step="enter_code", detail=f"Nhập mã {code}", code=code)
            _type_code(field, code)
            if not _click_enabled_next(page):
                try:
                    field.press("Enter")
                except Exception:
                    page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            if account_password:
                _emit(on_progress, step="password", detail="Điền mật khẩu tài khoản")
                for _ in range(12):
                    if stop_requested():
                        raise RuntimeError("Đã dừng")
                    if _text_visible(page, _BIRTHDAY_PAGE) or _has_visible_select(page):
                        _fill_account_password(page, account_password)
                        break
                    if _fill_account_password(page, account_password):
                        page.wait_for_timeout(400)
                        if not _text_visible(page, _BIRTHDAY_PAGE) and not _has_visible_select(page):
                            if not _click_enabled_next(page):
                                _click_named_button(page, BTN_NEXT)
                        break
                    page.wait_for_timeout(400)
            person = _random_person()
            birthday = _random_birthday()
            _emit(on_progress, step="name", detail=f"Điền tên {person['full']}")
            named = False
            for _ in range(15):
                if stop_requested():
                    raise RuntimeError("Đã dừng")
                if _text_visible(page, _BIRTHDAY_PAGE):
                    break
                if _fill_name(page, person):
                    named = True
                    page.wait_for_timeout(800)
                    if not _text_visible(page, _BIRTHDAY_PAGE) and not _has_visible_select(page):
                        if not _click_enabled_next(page):
                            _click_named_button(page, BTN_NEXT)
                    break
                page.wait_for_timeout(400)
            if named:
                _emit(on_progress, step="name", detail=f"Đã điền tên {person['full']}")
            _emit(on_progress, step="birthday", detail=f"Điền ngày sinh {birthday['label']}")
            _fill_birthday(page, person, birthday)
            _resolve_after_birthday(page, on_progress)
            own_invite, invite_note = _chain_invites(page, email, on_progress, take_prior, on_invite, context)
            if not own_invite:
                raise RuntimeError("Chưa trích xuất được mã giới thiệu vào tool")
            done_detail = f"Đã tạo {email}. Mã giới thiệu {own_invite} đã vào tool. Cookie đã lưu."
            if keep_open:
                done_detail += " Chrome được giữ."
            if invite_note:
                done_detail += " " + invite_note
            result = LoginProgress(
                step="done",
                detail=done_detail,
                code=code,
                final_url=page.url,
            )
            _emit(on_progress, **result.__dict__)
            if keep_open:
                _hold_browser(page, browser)
            return result
        except Exception as exc:
            message = "Đã dừng" if stop_requested() else str(exc)
            if isinstance(exc, TempMailPlusError):
                message = str(exc)
            err = LoginProgress(step="error", error=message)
            try:
                err.final_url = page.url
                page.screenshot(path=str(Path(os.environ.get("TEMP", ".")) / "muse-signup-error.png"))
            except Exception:
                pass
            _emit(on_progress, **err.__dict__)
            return err


def login_muse(
    email: str,
    *,
    headless: bool = False,
    close_after: bool = False,
    save_cookies: bool = True,
    proxy: str | None = None,
    totp_secret: str | None = None,
    mailtm_password: str | None = None,
    account_password: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> LoginProgress:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return LoginProgress(step="error", error=f"Chưa cài Playwright: {exc}")

    result = LoginProgress(step="starting")
    _emit(on_progress, step="starting", detail="Mở trình duyệt…")

    known_ids = meta_message_ids(email)
    mailbox_password = (mailtm_password or "").strip()
    mail_token = ""
    known_mail_ids: set[str] = set()
    if mailbox_password:
        from tempmail_plus_client import TempMailPlusError, login_mailbox, message_ids

        _emit(on_progress, step="mail", detail="Mở hộp thư tempmail.plus để lấy mã lần sau")
        try:
            mail_token = login_mailbox(email, mailbox_password)
            known_mail_ids = message_ids(mail_token)
        except TempMailPlusError as exc:
            err = LoginProgress(step="error", error=str(exc))
            _emit(on_progress, **err.__dict__)
            return err

    with sync_playwright() as p:
        proxy_cfg = parse_proxy(proxy)
        if proxy_cfg:
            _emit(on_progress, step="proxy", detail=f"Dùng proxy {proxy_cfg['server']}")
        try:
            browser = p.chromium.launch(**_browser_launch_kwargs(proxy_cfg, headless=headless))
        except Exception as exc:
            message = str(exc).split("╔", 1)[0].strip()
            if "Executable doesn't exist" in message:
                message = (
                    "Không mở được trình duyệt. Chưa có Chromium của Playwright, "
                    "và không tìm thấy Chrome/Edge. Chạy: python -m playwright install chromium"
                )
            err = LoginProgress(step="error", error=message or "Không mở được trình duyệt")
            _emit(on_progress, **err.__dict__)
            try:
                update_account_meta(email, login_error=err.error, account_status="lỗi")
            except Exception:
                pass
            return err
        context = browser.new_context(**_browser_context_kwargs(proxy_cfg))
        context.add_init_script(_HIDE_WEBDRIVER)
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            _emit(on_progress, step="open_muse", detail="Tải muse.ai…")
            page.goto(MUSE_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(1500)

            _emit(on_progress, step="email", detail=f"Chờ Muse load xong rồi nhập email {email}")
            # Cả hai dạng tài khoản (có mật khẩu hộp thư hay dạng mật khẩu/2FA)
            # đều phải chờ form email ổn định rồi mới nhập. Nhánh cũ nhập ngay
            # khi ô vừa hiện, lúc Muse chưa load xong: trang hydrate/load lại
            # sẽ thay ô input và làm mất email vừa nhập.
            # _submit_signup_email đã gồm cả hai việc: chờ Muse load xong mới
            # nhập, và tự nhập lại nếu trang load lại làm mất email.
            _submit_signup_email(page, email)

            if account_password:
                for _ in range(20):
                    if _click_use_password_instead(page):
                        _emit(on_progress, step="password", detail="Sử dụng mật khẩu tài khoản")
                        page.wait_for_timeout(1200)
                    if _fill_account_password(page, account_password):
                        _emit(on_progress, step="password", detail="Điền mật khẩu tài khoản")
                        page.wait_for_timeout(500)
                        if not _click_enabled_next(page):
                            try:
                                page.keyboard.press("Enter")
                            except Exception:
                                pass
                        break
                    if _logged_in_now(page):
                        break
                    if _confirm_screen_visible(page):
                        page.wait_for_timeout(500)
                        if _click_use_password_instead(page):
                            page.wait_for_timeout(1000)
                    page.wait_for_timeout(400)

            _emit(on_progress, step="wait_otp_mail", detail="Chờ mã hoặc màn hình xác nhận…")
            otp_input = page.locator('input[autocomplete="one-time-code"]')
            secret = (totp_secret or "").strip()
            saw_confirm = False
            for _ in range(4):
                try:
                    otp_input.or_(page.get_by_text(CONFIRM_SCREEN)).first.wait_for(state="visible", timeout=20000)
                except Exception:
                    pass
                if not _confirm_screen_visible(page):
                    break
                saw_confirm = True
                _emit(on_progress, step="recovery", detail="Thấy màn xác nhận Meta — bấm Tiếp")
                if _select_recovery_code(page):
                    break
                page.wait_for_timeout(800)
            if _confirm_screen_visible(page):
                raise RuntimeError(
                    "Đã thấy màn xác nhận Meta nhưng chưa bấm được Tiếp."
                )
            twofa_code = None
            if saw_confirm and secret:
                twofa_code = _enter_recovery_code(page, secret, on_progress)
                page.wait_for_timeout(2500)
            elif saw_confirm and not mailbox_password:
                raise RuntimeError(
                    "Màn xác nhận Meta cần mã 2FA. Dán tài khoản dạng email|link lay2fa|MÃ 2FA."
                )
            email_code = None
            if not _logged_in_now(page):
                need_email = not saw_confirm
                if saw_confirm:
                    deadline = time.time() + 25
                    while time.time() < deadline and not _logged_in_now(page) and not _email_code_screen(page):
                        page.wait_for_timeout(1000)
                    need_email = _email_code_screen(page) and not _logged_in_now(page)
                if need_email:
                    try:
                        otp_input.wait_for(state="visible", timeout=45000)
                    except Exception:
                        page.get_by_text(CODE_SCREEN).wait_for(timeout=15000)
                        otp_input = page.locator('input[autocomplete="one-time-code"]')
                    if mailbox_password:
                        from tempmail_plus_client import TempMailPlusError, login_mailbox, wait_for_code

                        _emit(on_progress, step="fetch_otp", detail="Lấy mã mới từ tempmail.plus…")
                        try:
                            email_code = wait_for_code(mail_token, timeout=120, exclude_ids=known_mail_ids)
                        except TempMailPlusError as exc:
                            if "hết phiên" not in str(exc):
                                raise
                            mail_token = login_mailbox(email, mailbox_password)
                            email_code = wait_for_code(mail_token, timeout=90, exclude_ids=known_mail_ids)
                    else:
                        _emit(on_progress, step="fetch_otp", detail="Lấy mã từ Lay2FA…")
                        otp_result = wait_for_new_muse_code(
                            email,
                            exclude_ids=known_ids,
                            timeout=120,
                            interval=3,
                        )
                        if not otp_result.code:
                            raise RuntimeError(otp_result.error or "Không lấy được mã OTP")
                        email_code = otp_result.code
                    _emit(
                        on_progress,
                        step="enter_otp",
                        detail=f"Mã {email_code}",
                        code=email_code,
                    )
                    _submit_otp(page, otp_input, email_code)

            if _confirm_screen_visible(page):
                _emit(on_progress, step="recovery", detail="Mã đăng nhập xong — bấm Tiếp")
                if not _select_recovery_code(page):
                    raise RuntimeError(
                        "Đã có mã đăng nhập, nhưng chưa bấm được Tiếp trên màn xác nhận Meta."
                    )
                if secret:
                    twofa_code = _enter_recovery_code(page, secret, on_progress)
                elif _confirm_screen_visible(page):
                    raise RuntimeError(
                        "Đã có mã đăng nhập, nhưng chưa bấm được Tiếp trên màn xác nhận Meta."
                    )

            gate = _drive_until_selfie(page, on_progress)
            if gate in ("upload", "uploaded", "captcha"):
                detail = _user_gate_detail(gate)
                held = LoginProgress(
                    step="needs_user",
                    detail=detail,
                    code=email_code or twofa_code,
                    final_url=page.url,
                )
                _emit(on_progress, **held.__dict__)
                _hold_browser(page, browser)
                return held

            _emit(
                on_progress,
                step="submit",
                detail="OTP xong — trang có thể đứng im vài giây (bình thường)…",
            )
            _wait_logged_in(page, timeout_sec=45)
            page.wait_for_timeout(2000)
            gate = _drive_until_selfie(page, on_progress)
            if gate in ("upload", "uploaded", "captcha"):
                detail = _user_gate_detail(gate)
                held = LoginProgress(
                    step="needs_user",
                    detail=detail,
                    code=email_code or twofa_code,
                    final_url=page.url,
                )
                _emit(on_progress, **held.__dict__)
                _hold_browser(page, browser)
                return held

            _emit(
                on_progress,
                step="refresh_status",
                detail="F5 + chờ muse.ai/api/consent/status…",
            )
            status_header = _refresh_and_get_status_cookie(page)
            if _human_verify_page(page):
                gate = _drive_until_selfie(page, on_progress)
                if gate in ("upload", "uploaded", "captcha"):
                    detail = _user_gate_detail(gate)
                    held = LoginProgress(
                        step="needs_user",
                        detail=detail,
                        code=email_code or twofa_code,
                        final_url=page.url,
                    )
                    _emit(on_progress, **held.__dict__)
                    _hold_browser(page, browser)
                    return held
            _dismiss_onboarding(page)
            page.wait_for_timeout(2000)

            cookie_info = LoginProgress()
            if save_cookies:
                if not status_header:
                    status_header = cookies_to_header(filter_muse_cookies(context.cookies()))
                _emit(on_progress, step="save_cookies", detail="Lưu cookie…")
                cookie_info = _capture_and_save_cookies(
                    context,
                    email,
                    status_cookie_header=status_header,
                    page=page,
                )
                if not cookie_info.cookie_header:
                    raise RuntimeError(
                        "Không lấy được cookie. Thử Lấy cookie lại (đảm bảo đã restart python app.py)."
                    )
                _emit(on_progress, **cookie_info.__dict__)

            result = LoginProgress(
                step="done",
                detail="Đăng nhập xong — cookie đã lưu." if cookie_info.cookie_header else "Đăng nhập xong.",
                code=email_code or twofa_code,
                final_url=page.url,
                cookie_header=cookie_info.cookie_header,
                cookies_count=cookie_info.cookies_count,
                cookies_saved_at=cookie_info.cookies_saved_at,
            )
            _emit(on_progress, **result.__dict__)
            if close_after or headless:
                browser.close()
                return result
            # Giữ process Playwright sống để Chrome không bị đóng theo; user có thể tắt Chrome bất cứ lúc nào.
            browser_closed = {"v": False}

            def _on_browser_closed() -> None:
                browser_closed["v"] = True

            browser.on("disconnected", _on_browser_closed)
            while not browser_closed["v"]:
                try:
                    page.wait_for_timeout(5000)
                except Exception:
                    break
            return result
        except Exception as exc:
            stopped = stop_requested()
            message = "Đã dừng" if stopped else str(exc)
            status = "" if stopped else ("die" if DIE_TEXT.search(message) else "lỗi")
            if not stopped:
                try:
                    update_account_meta(email, login_error=message, account_status=status)
                except Exception:
                    pass
            final_url = None
            try:
                final_url = page.url
            except Exception:
                pass
            err = LoginProgress(step="error", error=message, final_url=final_url)
            _emit(on_progress, **err.__dict__)
            still_open = False
            if not headless:
                try:
                    still_open = browser.is_connected()
                except Exception:
                    still_open = False
            if still_open:
                _emit(
                    on_progress,
                    step="error",
                    error=message,
                    detail="Chrome được giữ mở. Chưa qua captcha thì tool không tắt cửa sổ.",
                )
                _hold_browser(page, browser)
            elif headless or close_after:
                try:
                    browser.close()
                except Exception:
                    pass
            return err
