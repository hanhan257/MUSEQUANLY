"""Playwright automation: Muse AI login with Lay2FA OTP."""

from __future__ import annotations

import json
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

from cookie_store import cookies_to_header, filter_muse_cookies, get_account_cookies, save_account_cookies, update_account_meta
from warehouse_store import set_redeemed_invite_code, warehouse_invite_code
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
    """Turn ip:port, ip:port:user:pass, or scheme://user:pass@host:port into a Playwright proxy.
    Mat khau co the chua @ hoac : nen PHAI nhan dang host:port truoc
    (phan tu thu 2 la so port) roi moi xet dang user:pass@host — giong
    cach v115 da sua cho requests_proxies. Neu khong, mat khau chua @
    se bi tach sai thanh server rac (vd http://ss) va Chrome se hoi
    user/pass proxy."""
    text = normalize_proxy_line(raw)
    if not text:
        return None
    if "://" not in text:
        parts = text.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            if len(parts) == 2:
                return {"server": f"http://{parts[0]}:{parts[1]}"}
            if len(parts) >= 4:
                host, port, user = parts[0], parts[1], parts[2]
                password = ":".join(parts[3:])
                return {"server": f"http://{host}:{port}", "username": user, "password": password}
            return None
        if "@" in text:
            auth, hostport = text.rsplit("@", 1)
            user, _, password = auth.partition(":")
            return {"server": f"http://{hostport}", "username": user, "password": password}
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
# Tiêu đề dialog chặn ("Something went wrong") — dự phòng khi dòng lỗi
# "You can't create..." không khớp được (vd dấu nháy cong ' thay vì ').
SIGNUP_WRONG = re.compile(r"something went wrong", re.I)
MAIL_FLAGGED = re.compile(
    r"spam|disposable|temporary email|email tạm|không hỗ trợ email|email này không|không thể dùng email|try a different email|use a different email|invalid email|email không hợp lệ|không chấp nhận",
    re.I,
)
CARD_PAGE = re.compile(r"Xác nhận tuổi của bạn|Confirm your age", re.I)
CARD_BAD = re.compile(
    r"thẻ không hợp lệ|thẻ bị từ chối|thẻ này không|không thể xác minh|không thể xử lý thẻ|thẻ đã bị|invalid card|card (?:was )?declined|incorrect card|try another card|card number is invalid|unable to add card|too many ad accounts|associated with too many|select a different payment method",
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
_SIGNUP_TURN_GAP = 20.0


def set_signup_turn_gap(seconds: float) -> None:
    """Khoang cach giua cac lan mo Chrome. Dong nho thi 20 giay; dong lon (nhieu luong) thi 5 giay de lan cuoi khong phai cho lau."""
    global _SIGNUP_TURN_GAP
    _SIGNUP_TURN_GAP = max(1.0, float(seconds or 20))


def _wait_signup_turn(on_progress: ProgressCallback | None) -> None:
    """Mỗi tài khoản cách nhau một khoảng lúc mở Chrome, tránh đụng Muse cùng một lúc."""
    global _next_signup_at
    with _signup_turn_lock:
        now = time.monotonic()
        delay = _next_signup_at - now
        if delay < 0:
            delay = 0
        _next_signup_at = now + delay + _SIGNUP_TURN_GAP
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


def _try_auto_solve_captcha(page: Page) -> tuple[bool, str]:
    """Giai captcha bang dich vu ben ngoai neu da cau hinh key.

    Tra (True, ten dich vu) neu chen duoc token vao trang,
    (False, ly do) neu chua cau hinh / loi — buoc goi se doi user giai tay.
    """
    try:
        import captcha_solver
    except Exception as exc:
        return False, f"khong nap duoc captcha_solver ({exc})"
    try:
        return captcha_solver.auto_solve_page(page)
    except Exception as exc:
        return False, f"loi giai captcha ({exc})"


def _click_recaptcha_checkbox(page: Page) -> bool:
    """Tu bam vao o checkbox 'Tôi không phải là người máy' trong iframe
    anchor cua reCAPTCHA (user 2026-11-13 00:05: 'da lam tool con bam
    tay' — tool phai tu bam, khong de user bam tay). Tra True neu bam
    duoc. Neu Google hien luoi chon hinh sau khi bam thi buoc goi cho
    user giai tay (hoac thu token sau)."""
    try:
        frames = list(page.frames)
    except Exception:
        return False
    for frame in frames:
        url = (frame.url or "").lower()
        if "recaptcha" not in url or "anchor" not in url:
            continue
        for sel in ("#recaptcha-anchor", ".recaptcha-checkbox", '[role="checkbox"]'):
            try:
                loc = frame.locator(sel)
                if loc.count() > 0:
                    loc.first.click(timeout=5000)
                    return True
            except Exception:
                continue
    return False


def _drive_until_selfie(page: Page, on_progress: ProgressCallback | None) -> str | None:
    """Đi tiếp màn xác minh người thật tới lúc up ảnh.

    Gap luoi chon hinh captcha: thu giai tu dong bang dich vu (2Captcha /
    CapSolver) neu da cau hinh key; neu khong duoc thi doi user giai tay
    trong Chrome duoc giu nhu cu.
    """
    if not _human_verify_page(page):
        return None
    selfie_name = ""
    selfie_tries = 0
    reported_captcha = False
    solver_tried = False
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
            if not solver_tried:
                solver_tried = True
                _emit(
                    on_progress,
                    step="human_check",
                    detail="Thấy lưới captcha — đang thử giải tự động…",
                    final_url=page.url,
                )
                solved, note = _try_auto_solve_captcha(page)
                if solved:
                    _emit(
                        on_progress,
                        step="human_check",
                        detail=f"Đã giải captcha bằng {note} — bấm Tiếp tục",
                        final_url=page.url,
                    )
                    page.wait_for_timeout(800)
                    if _click_named_button(page, BTN_NEXT) or _click_captcha_verify(page) == "ok":
                        page.wait_for_timeout(1800)
                        continue
                else:
                    _emit(
                        on_progress,
                        step="needs_user",
                        detail=f"Giải tự động không được ({note}) — chờ bạn giải tay.",
                        final_url=page.url,
                    )
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
    try:
        import captcha_solver

        auto = captcha_solver.configured() is not None
    except Exception:
        auto = False
    if auto:
        return "Dừng ở captcha chọn hình. Giải tự động không qua được — chờ bạn giải tay. Chrome được giữ."
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


def _click_enabled_button(page: Page, pattern: re.Pattern[str], reverse_text: bool = False) -> bool:
    """Chỉ bấm nút đang sáng. Nút ẩn hoặc nút mờ phía trước không chặn nút thật.
    reverse_text=True: voi nut dang text (khong phai <button>/<a>), duyet
    match tu cuoi ve dau — phan tu trong cung chua text moi la nut that;
    bam vao giua phan tu bao ngoai thi khong chuyen trang (gap o nut
    'Dùng email hoặc số di động' cua Meta)."""
    for frame in _frames(page):
        groups = []
        try:
            groups.append(("role", frame.get_by_role("button", name=pattern)))
        except Exception:
            pass
        try:
            groups.append(("role", frame.get_by_role("link", name=pattern)))
        except Exception:
            pass
        try:
            groups.append(("text", frame.get_by_text(pattern)))
        except Exception:
            pass
        for kind, group in groups:
            try:
                total = group.count()
            except Exception:
                continue
            if kind == "text" and reverse_text:
                indices = range(total - 1, max(total - 8, 0) - 1, -1)
            else:
                indices = range(min(total, 8))
            for index in indices:
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


def _click_button_js(page: Page, pattern: re.Pattern[str], reverse_text: bool = False) -> bool:
    """Bam nut bang JavaScript (el.click()), khong kiem tra bi che.
    Dung khi nut bi dropdown dang mo che (Playwright click() thuong tu
    choi vi nut bi che) nhung nut van bam duoc. Chi dung lam fallback
    sau khi _click_enabled_button that bai."""
    for frame in _frames(page):
        groups = []
        try:
            groups.append(("role", frame.get_by_role("button", name=pattern)))
        except Exception:
            pass
        try:
            groups.append(("role", frame.get_by_role("link", name=pattern)))
        except Exception:
            pass
        try:
            groups.append(("text", frame.get_by_text(pattern)))
        except Exception:
            pass
        for kind, group in groups:
            try:
                total = group.count()
            except Exception:
                continue
            if kind == "text" and reverse_text:
                indices = range(total - 1, max(total - 8, 0) - 1, -1)
            else:
                indices = range(min(total, 8))
            for index in indices:
                button = group.nth(index)
                try:
                    if not button.is_visible() or not button.is_enabled():
                        continue
                    if (button.get_attribute("aria-disabled") or "").lower() == "true":
                        continue
                    button.evaluate("el => el.click()")
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


def _chrome_exe_paths() -> list[str]:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
    ]


def _installed_browser_channel() -> str | None:
    """Dùng Chrome hoặc Edge đã cài, vì bản Chromium của Playwright thường chưa được tải."""
    if os.name != "nt":
        return None
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    edge = [
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
    ]
    if any(os.path.isfile(path) for path in _chrome_exe_paths()):
        return "chrome"
    if any(os.path.isfile(path) for path in edge):
        return "msedge"
    return None


BROWSERS = {
    "auto": "Tự động (Chrome/Edge đã cài, không có thì Chromium)",
    "chromium": "Chromium (Playwright)",
    "chrome": "Google Chrome",
    "firefox": "Firefox",
    "coccoc": "Cốc Cốc",
}


def normalize_browser(value) -> str:
    """Chuan hoa lua chon trinh duyet: 'auto'/trong -> 'auto' (giữ hành vi cũ:
    dùng Chrome/Edge đã cài, không có thì Chromium của Playwright)."""
    v = (value or "").strip().lower().replace(".", "").replace("-", "").replace(" ", "")
    if v in ("chrome", "googlechrome"):
        return "chrome"
    if v in ("firefox", "ff"):
        return "firefox"
    if v in ("coccoc", "cốccốc"):
        return "coccoc"
    if v in ("chromium", "playwright"):
        return "chromium"
    return "auto"


def find_coccoc_path() -> str | None:
    """Duong dan Cốc Cốc da cai (Windows la chinh; them Linux/macOS).
    Bien moi truong COCCOC_PATH uu tien hon duong dan mac dinh."""
    override = os.environ.get("COCCOC_PATH", "").strip()
    if override and os.path.isfile(override):
        return override
    candidates: list[str] = []
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            os.path.join(pf, r"CocCoc\Browser\Application\browser.exe"),
            os.path.join(pf86, r"CocCoc\Browser\Application\browser.exe"),
            os.path.join(local, r"CocCoc\Browser\Application\browser.exe"),
        ]
    candidates += [
        "/opt/coccoc/coccoc",
        "/usr/bin/coccoc",
        "/usr/local/bin/coccoc",
        "/Applications/CocCoc.app/Contents/MacOS/CocCoc",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _launch_browser(p, proxy_cfg, *, headless: bool = False, browser_name: str = "auto"):
    """Mo trinh duyet da chon, gan proxy ngay luc mo. 'auto' = hanh vi cu
    (Chrome/Edge da cai neu co, khong thi Chromium cua Playwright);
    'chromium' = ban Chromium cua Playwright; 'chrome' = Google Chrome da
    cai; 'firefox' = ban Firefox cua Playwright; 'coccoc' = Cốc Cốc da cai.
    Thieu trinh duyet thi bao loi tieng Viet ro rang thay vi loi goc cua
    Playwright."""
    name = normalize_browser(browser_name)
    if name == "firefox":
        kwargs: dict = {
            "headless": headless,
            # Tat bang dich cua Firefox (hien len che trang, tool khong
            # bam duoc — gap khi dung proxy US o v145).
            "firefox_user_prefs": {
                "browser.translations.enable": False,
                "browser.translations.automaticallyPopup": False,
            },
        }
        if proxy_cfg:
            kwargs["proxy"] = proxy_cfg
        try:
            return p.firefox.launch(**kwargs)
        except Exception as exc:
            if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                raise RuntimeError("Chưa cài Firefox của Playwright. Chạy: playwright install firefox rồi thử lại.") from exc
            raise
    kwargs = _browser_launch_kwargs(proxy_cfg, headless=headless)
    if name == "chrome":
        if os.name == "nt" and not any(os.path.isfile(path) for path in _chrome_exe_paths()):
            raise RuntimeError("Không tìm thấy Google Chrome đã cài. Cài Chrome rồi thử lại, hoặc chọn Tự động/Chromium.")
        kwargs["channel"] = "chrome"
    elif name == "coccoc":
        path = find_coccoc_path()
        if not path:
            raise RuntimeError("Không tìm thấy Cốc Cốc đã cài. Cài Cốc Cốc rồi thử lại, hoặc chọn trình duyệt khác.")
        kwargs["executable_path"] = path
        kwargs.pop("channel", None)
    elif name == "chromium":
        kwargs.pop("channel", None)
    try:
        return p.chromium.launch(**kwargs)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            raise RuntimeError("Chưa tải Chromium của Playwright. Chạy: playwright install chromium rồi thử lại.") from exc
        raise


def _browser_context_kwargs(
    proxy_cfg: dict[str, str] | None, browser_name: str = "auto", fingerprint: dict | None = None, locale: str | None = None
) -> dict:
    """Proxy Mỹ thì Chrome phải ra giờ, ngôn ngữ và vị trí Mỹ. Lệch giờ là Muse từ chối tạo.
    Firefox KHÔNG hỗ trợ quyền clipboard-read/clipboard-write trong new_context
    (Playwright báo 'Browser.new_context: Unknown permission: clipboard-read'
    và flow chết ngay khi mở context) nên với Firefox chỉ xin geolocation.
    Viewport/screen lấy từ fingerprint (ngẫu nhiên mỗi lần tạo) để mỗi lần mở
    không giống hệt nhau như trước (1280x900 cố định).
    locale='vi' giữ giờ/ngôn ngữ Việt Nam ngay cả khi có proxy (dùng cho tạo
    TK Meta — IP Việt Nam thường không bị hỏi captcha)."""
    fp = fingerprint or _random_fingerprint(browser_name)
    firefox = (browser_name or "auto").strip().lower() == "firefox"
    kwargs: dict = {"viewport": fp["viewport"], "screen": fp["viewport"]}
    use_us = bool(proxy_cfg) and (locale or "").strip().lower() not in ("vi", "vi-vn", "vn")
    if use_us:
        kwargs["locale"] = "en-US"
        kwargs["timezone_id"] = "America/New_York"
        kwargs["geolocation"] = {"latitude": 40.7306, "longitude": -73.9352}
        permissions = ["geolocation"] if firefox else ["geolocation", "clipboard-read", "clipboard-write"]
    else:
        kwargs["locale"] = "vi-VN"
        permissions = [] if firefox else ["clipboard-read", "clipboard-write"]
    if permissions:
        kwargs["permissions"] = permissions
    return kwargs


def _browser_launch_kwargs(proxy_cfg: dict[str, str] | None, *, headless: bool = False) -> dict:
    """Chrome thường, không bật cờ automation. Proxy gắn ngay lúc mở cửa sổ."""
    kwargs: dict = {
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-features=Translate",
            "--disable-translate",
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


# --- Fingerprint spoofing (v129) ---
# Genlogin qua duoc buoc ngay sinh vi moi profile co fingerprint nhu may that
# (canvas, WebGL, plugin, phan cung...). Playwright mac dinh de lo dau vet
# automation: navigator.webdriver, canvas/WebGL giong het nhau moi lan mo,
# viewport 1280x900 co dinh, plugins trong. Muse chan o buoc ngay sinh du
# proxy co tot. Moi context lay mot fingerprint ngau nhien nhung nhat quan
# trong suot lan dung context do (viewport, canvas seed, WebGL...). Khong
# doi user_agent: UA phai khop dung ban trinh duyet that, doi UA moi lo.
_VIEWPORTS = [(1920, 1080), (1366, 768), (1536, 864), (1440, 900), (1280, 720), (1600, 900)]
_WEBGL_PAIRS = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]


def _random_fingerprint(browser_name: str = "auto") -> dict:
    """Mot fingerprint ngau nhien cho mot lan mo context. Moi lan tao TK goi
    mot lan, giu nguyen trong suot lan do."""
    width, height = random.choice(_VIEWPORTS)
    vendor, renderer = random.choice(_WEBGL_PAIRS)
    return {
        "viewport": {"width": width, "height": height},
        "hardware_concurrency": random.choice([4, 8, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 8]),
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "canvas_seed": random.randint(1, 10**6),
        # Win32 chi co tren Windows (may user); VM Linux de nguyen platform that
        # cho khop UA Linux, khong gia Win32 o day.
        "platform": "Win32" if os.name == "nt" else None,
        # Firefox that khong co plugin Chrome; gia plugin Chrome vao Firefox
        # chi lam lo hon nen chi gia cho trinh duyet Chromium.
        "chromium": (browser_name or "auto").strip().lower() != "firefox",
    }


def _stealth_init_script(fp: dict, has_proxy: bool) -> str:
    """Script chay truoc moi trang (add_init_script): an webdriver, gia
    canvas/WebGL/plugin/phan cung theo fingerprint da random."""
    vendor = json.dumps(fp["webgl_vendor"])
    renderer = json.dumps(fp["webgl_renderer"])
    seed = int(fp["canvas_seed"])
    langs = '["en-US", "en"]' if has_proxy else '["vi-VN", "vi", "en-US", "en"]'
    platform_patch = (
        f"Object.defineProperty(navigator, 'platform', {{get: () => {json.dumps(fp['platform'])}}});"
        if fp.get("platform")
        else ""
    )
    plugins_patch = ""
    if fp.get("chromium"):
        plugins_patch = """const _mkPlugin = (name, filename, description) => ({name, filename, description, length: 1, item: () => null, namedItem: () => null});
      const _plugins = [_mkPlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format'), _mkPlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'), _mkPlugin('Native Client', 'internal-nacl-plugin', 'Native Client Executable')];
      _plugins.item = (i) => _plugins[i] || null;
      _plugins.namedItem = (n) => _plugins.find((p) => p.name === n) || null;
      Object.defineProperty(navigator, 'plugins', {get: () => _plugins});"""
    return f"""(() => {{
      Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
      {platform_patch}
      Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {fp['hardware_concurrency']}}});
      Object.defineProperty(navigator, 'deviceMemory', {{get: () => {fp['device_memory']}}});
      Object.defineProperty(navigator, 'languages', {{get: () => {langs}}});
      {plugins_patch}
      const _seed = {seed};
      const _noise = (ctx, w, h) => {{ try {{ const x = _seed % Math.max(1, w); const y = (_seed >> 4) % Math.max(1, h); ctx.fillStyle = 'rgba(' + (_seed % 255) + ',' + ((_seed >> 3) % 255) + ',' + ((_seed >> 5) % 255) + ',0.02)'; ctx.fillRect(x, y, 1, 1); }} catch (e) {{}} }};
      const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function () {{ try {{ const c = this.getContext('2d'); if (c) _noise(c, this.width, this.height); }} catch (e) {{}} return _toDataURL.apply(this, arguments); }};
      const _spoofGL = (proto) => {{ if (!proto) return; const _gp = proto.getParameter; proto.getParameter = function (p) {{ if (p === 37445) return {vendor}; if (p === 37446) return {renderer}; return _gp.call(this, p); }}; }};
      try {{ _spoofGL(WebGLRenderingContext && WebGLRenderingContext.prototype); }} catch (e) {{}}
      try {{ _spoofGL(WebGL2RenderingContext && WebGL2RenderingContext.prototype); }} catch (e) {{}}
      try {{ const _q = navigator.permissions.query.bind(navigator.permissions); navigator.permissions.query = (params) => {{ if (params && params.name === 'notifications') return Promise.resolve({{state: 'prompt'}}); return _q(params); }}; }} catch (e) {{}}
    }})();"""


def _new_browser_context(browser, proxy_cfg: dict[str, str] | None, browser_name: str = "auto", locale: str | None = None):
    """Tao context moi voi fingerprint ngau nhien + script an automation,
    nhat quan trong suot lan dung context do. Thay cho cap lenh new_context
    + add_init_script(script gia fingerprint) o 4 cho (redeem, token-check, signup,
    login). locale='vi' giu gio/ngon ngu Viet Nam ngay ca khi co proxy (tao TK Meta)."""
    fp = _random_fingerprint(browser_name)
    context = browser.new_context(**_browser_context_kwargs(proxy_cfg, browser_name, fp, locale))
    context.add_init_script(_stealth_init_script(fp, bool(proxy_cfg)))
    return context


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


_CARD_LOCK = threading.Lock()
_CARD_INFLIGHT: dict[str, int] = {}


def _reserve_card() -> dict | None:
    """Lay the it dung nhat trong kho the de dung ngay (xoay vong co khoa).
    Chon kho theo (luot da dung + luot dang chay) thap nhat: 20 luong / 4 kho thi moi kho nhan ~5 luong, khong don het vao 1 the.
    Tra None khi khong con the nao dung duoc (het, loi, hoac da du 10 luot)."""
    from warehouse_store import usable_card_warehouses

    with _CARD_LOCK:
        cards = usable_card_warehouses()
        if not cards:
            return None
        cards.sort(
            key=lambda c: (
                int(c.get("uses") or 0) + _CARD_INFLIGHT.get(c.get("id") or "", 0),
                c.get("name") or "",
            )
        )
        card = cards[0]
        cid = card.get("id") or ""
        _CARD_INFLIGHT[cid] = _CARD_INFLIGHT.get(cid, 0) + 1
        return card


def _release_card(card_id: str, success: bool) -> dict | None:
    """Tra luot dang chay cua the. Thanh cong thi +1 luot dung (du 10 luot thi khoa kho, tra ve thong tin kho sau khi cap nhat)."""
    from warehouse_store import bump_card_warehouse_use

    with _CARD_LOCK:
        if card_id in _CARD_INFLIGHT:
            _CARD_INFLIGHT[card_id] = max(0, _CARD_INFLIGHT[card_id] - 1)
    if success and card_id:
        return bump_card_warehouse_use(card_id)
    return None


def _submit_age_card(page: Page, on_progress: ProgressCallback | None) -> None:
    """Điền thẻ trong kho, xoay vòng có khóa giữa các luồng.
    Thẻ nào Muse báo lỗi thì khóa kho đó và sang thẻ tiếp theo ngay; thẻ nào đủ 10 lượt dùng thành công thì khóa kho.
    Hết thẻ dùng được thì báo lỗi để tài khoản dừng lại thay vì cứ thử thẻ chết."""
    from warehouse_store import mark_card_warehouse_bad

    while True:
        card = _reserve_card()
        if card is None:
            raise RuntimeError("Kho thẻ đã hết: các thẻ đều bị Muse báo lỗi hoặc đã đủ 10 lượt")
        cid = card.get("id") or ""
        settled = False
        try:
            if not _card_form_visible(page):
                settled = True
                _release_card(cid, success=False)
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
                    done = _release_card(cid, success=True)
                    settled = True
                    locked_note = (
                        f" · {card.get('name')} đã đủ 10 lượt, khóa kho"
                        if done and done.get("status") == "used"
                        else ""
                    )
                    _emit(on_progress, step="card", detail=f"Đã qua bước thẻ bằng {card.get('name')}" + locked_note)
                    _ack_disclosure(page, on_progress)
                    return
                if not _card_form_visible(page):
                    done = _release_card(cid, success=True)
                    settled = True
                    locked_note = (
                        f" · {card.get('name')} đã đủ 10 lượt, khóa kho"
                        if done and done.get("status") == "used"
                        else ""
                    )
                    _emit(on_progress, step="card", detail=f"Đã qua bước thẻ bằng {card.get('name')}" + locked_note)
                    _ack_disclosure(page, on_progress)
                    return
                if _text_visible(page, CARD_BUSY):
                    _click_named_button(page, re.compile(r"^OK$|^Đồng ý$"))
                    raise RuntimeError(
                        f"Muse báo quá nhiều lần thử với {card.get('name')} · ****{last4}. Thẻ vẫn giữ trong kho, chờ rồi tạo lại, không bấm Xác nhận tiếp."
                    )
                if _text_visible(page, CARD_BAD):
                    _click_named_button(page, re.compile(r"^OK$|^Đồng ý$"))
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
            mark_card_warehouse_bad(cid)
            settled = True
            _release_card(cid, success=False)
            _emit(on_progress, step="card", detail=f"{card.get('name')} bị Muse báo lỗi, khóa kho và chuyển thẻ tiếp theo")
        finally:
            if not settled:
                _release_card(cid, success=False)


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
        page.context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin="https://muse.ai"
        )
    except Exception:
        pass
    try:
        copied = page.evaluate("() => navigator.clipboard.readText().catch(() => '')") or ""
    except Exception:
        copied = ""
    return _pick_invite_code(str(copied), email)


_INVITE_CODE_DOM_JS = r"""() => {
  const isCode = (s) => /^[A-Z0-9]{6}$/.test((s || '').trim());
  // Ma nam trong span co title chinh la ma; lop canvas chi phu len tren de che mat.
  for (const el of document.querySelectorAll('[title]')) {
    const t = (el.getAttribute('title') || '').trim();
    if (isCode(t) && (el.textContent || '').trim() === t) return t;
  }
  // Dong trang thai cho trinh doc man hinh khi ma da lo:
  // "Da tiet lo ma loi moi: ATGXDX" / "Invite code revealed:".
  for (const el of document.querySelectorAll('span')) {
    if (el.childElementCount > 0) continue;
    const m = (el.textContent || '').match(/(?:mã lời mời|invite code)[^A-Z0-9]{0,12}([A-Z0-9]{6})/i);
    if (m) return m[1].toUpperCase();
  }
  for (const el of document.querySelectorAll('[data-pel-impression*="invite_code"]')) {
    const t = (el.textContent || '').trim();
    if (isCode(t)) return t;
    const title = (el.getAttribute('title') || '').trim();
    if (isCode(title)) return title;
  }
  const bodyText = document.body ? (document.body.textContent || '') : '';
  const ann = bodyText.match(/(?:mã lời mời|invite code)[^A-Z0-9]{0,20}([A-Z0-9]{6})/i);
  if (ann) return ann[1].toUpperCase();
  return '';
}"""


_SCRATCH_RECT_JS = r"""() => {
  const rectOf = (el) => {
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
  };
  const isCode = (s) => /^[A-Z0-9]{6}$/.test((s || '').trim());
  // 1. Theo cau truc, khong theo ngon ngu: canvas nam cung container voi span mang ma.
  for (const el of document.querySelectorAll('[title]')) {
    const t = (el.getAttribute('title') || '').trim();
    if (!isCode(t)) continue;
    let node = el;
    for (let i = 0; i < 4 && node; i++) {
      const c = node.querySelector ? node.querySelector('canvas') : null;
      if (c) {
        const r = c.getBoundingClientRect();
        if (r.width > 0) return rectOf(c);
      }
      node = node.parentElement;
    }
  }
  // 2. Canvas dong vai tro nut trong panel Moi (role=button, phu kin thanh ma).
  for (const c of document.querySelectorAll('canvas[role="button"]')) {
    const r = c.getBoundingClientRect();
    if (r.width > 0 && r.height >= 8) return rectOf(c);
  }
  // 3. Theo aria-label cua canvas.
  for (const c of document.querySelectorAll('canvas')) {
    if (/mã lời mời|invite code/i.test(c.getAttribute('aria-label') || '')) {
      const r = c.getBoundingClientRect();
      if (r.width > 0) return rectOf(c);
    }
  }
  // 4. O chu "Scratch to reveal" / "Cao de...": o cao that la mot thanh ngang.
  const hits = [...document.querySelectorAll('div, span, button, canvas, section')].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 40 && r.height >= 8 && /scratch to reveal|cào để/i.test(el.textContent || '');
  });
  if (!hits.length) return null;
  const bars = hits.filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 150 && r.width <= 650 && r.height >= 24 && r.height <= 90;
  });
  const pool = bars.length ? bars : hits;
  pool.sort((a, b) => {
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    return bars.length ? rb.width * rb.height - ra.width * ra.height : ra.width * ra.height - rb.width * rb.height;
  });
  return rectOf(pool[0]);
}"""


def _ok_invite_code(code: str, email: str) -> bool:
    """Ma lay tu DOM da neo san (title == ma, hoac dong 'ma loi moi: XXXXXX') nen chi can loai ma trung mail va ma HAN gia."""
    if not code:
        return False
    if code == (email or "").split("@", 1)[0].upper():
        return False
    if code.startswith("HAN") and code[3:].isdigit():
        return False
    return True


def _scan_invite_code(page: Page, email: str) -> str:
    """Doc ma thang tu DOM cua panel Moi.
    Ma nam san trong DOM ke ca khi lop cao chua to mo, doc duoc la khoi can to hay copy."""
    for frame in _frames(page):
        try:
            found = frame.evaluate(_INVITE_CODE_DOM_JS) or ""
        except Exception:
            continue
        code = str(found).strip().upper()
        if re.fullmatch(r"[A-Z0-9]{6}", code) and _ok_invite_code(code, email):
            return code
    return _read_invite_box(page, email)


def _scratch_rect(page: Page):
    for frame in _frames(page):
        try:
            box = frame.evaluate(_SCRATCH_RECT_JS)
        except Exception:
            continue
        if box:
            return box
    return None


def _harvest_via_copy_link(page: Page, email: str, seconds: float = 10) -> str:
    """Bam "Copy referral link" roi boc ma khoi noi dung vua copy.
    Chi co tac dung sau khi ma da lo (chua to mo lop phu thi nut copy con mo, copy khong ra ma).
    Panel mo cham qua proxy nen cho nut copy xuat hien toi vai giay."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if page.is_closed():
            return ""
        if _click_enabled_button(page, COPY_CODE):
            page.wait_for_timeout(700)
            code = _clipboard_invite(page, email)
            if code:
                return code
        page.wait_for_timeout(700)
    return ""


def _paint_slow(page: Page, x_from: float, x_to: float, y: float) -> None:
    """Quet chuot CHAM tu x_from toi x_to tren hang y.
    Trang xu ly vi tri chuot theo tung khung hinh (~60fps): quet nhanh la chuot nhay ca tram px, co chi cham duoc vai diem roi.
    Moi doan ~10px nghi ~15ms thi khung nao cung bat duoc diem to, nhu tay nguoi to that."""
    distance = abs(x_to - x_from)
    steps = max(1, int(distance // 10))
    for index in range(1, steps + 1):
        page.mouse.move(x_from + (x_to - x_from) * index / steps, y)
        page.wait_for_timeout(15)


def _paint_reveal(page: Page, box: dict) -> bool:
    """Mot luot to mo lop phu "Scratch to reveal".
    Dua chuot vao o, nhan giu roi TO phu kin ca o — ngang tung hang cham rai, roi doc tung cot phu cheo.
    Lop phu bi xoa o noi co quet qua; to du thi ma hien ra va nut Copy referral link chuyen xanh, bam duoc."""
    try:
        left = box["x"] + 4
        right = box["x"] + box["width"] - 4
        top = box["y"] + 4
        bottom = box["y"] + box["height"] - 4
        page.mouse.move((left + right) / 2, (top + bottom) / 2)
        page.wait_for_timeout(400)
        page.mouse.down()
        page.wait_for_timeout(200)
        rows = max(4, int((bottom - top) // 6) + 1)
        for index in range(rows):
            y = top + (bottom - top) * index / (rows - 1) if rows > 1 else (top + bottom) / 2
            if index % 2 == 0:
                page.mouse.move(left, y)
                _paint_slow(page, left, right, y)
            else:
                page.mouse.move(right, y)
                _paint_slow(page, right, left, y)
        columns = max(3, int((right - left) // 60) + 1)
        for index in range(columns):
            x = left + (right - left) * index / (columns - 1) if columns > 1 else (left + right) / 2
            page.mouse.move(x, top)
            steps = max(1, int((bottom - top) // 10))
            for step in range(1, steps + 1):
                page.mouse.move(x, top + (bottom - top) * step / steps)
                page.wait_for_timeout(15)
        page.wait_for_timeout(200)
        page.mouse.up()
        page.wait_for_timeout(600)
        return True
    except Exception:
        try:
            page.mouse.up()
        except Exception:
            pass
        return False


def _harvest_invite_code(page: Page, email: str, on_progress: ProgressCallback | None) -> str:
    """Lay ma gioi thieu tu panel "Moi ban be".
    Ma bi che boi lop canvas "Scratch to reveal": bam thuong khong mo duoc, phai to chuot cao lop phu ra thi ma moi hien va nut Copy moi sang.
    Thu tu: doc thang tu DOM (ma nam san trong span title) -> bam Copy referral link -> to mo lop phu (toi da 4 luot, moi luot doc lai + copy lai).
    Khong lay duoc thi tra "" — tai khoan van luu binh thuong, khong lam hong ca lane."""
    _emit(on_progress, step="invite", detail="Mở Invite và lấy mã giới thiệu")
    code = ""
    try:
        deadline = time.monotonic() + 20
        opened = False
        while time.monotonic() < deadline:
            if stop_requested():
                raise RuntimeError("Đã dừng")
            if _invite_panel_open(page):
                opened = True
                break
            if not _click_enabled_button(page, INVITE_OPEN):
                _click_label(page, INVITE_OPEN)
            page.wait_for_timeout(800)
        if opened:
            code = _scan_invite_code(page, email)
            if not code:
                code = _harvest_via_copy_link(page, email)
            for attempt in range(4):
                if code or stop_requested():
                    break
                box = _scratch_rect(page)
                if not box:
                    break
                try:
                    page.evaluate(
                        """(pt) => {
                            const el = document.elementFromPoint(pt.x, pt.y);
                            if (el) el.scrollIntoView({block: 'center', inline: 'center'});
                        }""",
                        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
                    )
                    page.wait_for_timeout(400)
                    box = _scratch_rect(page) or box
                except Exception:
                    pass
                if not _paint_reveal(page, box):
                    break
                _emit(on_progress, step="invite", detail=f"Đã tô lớp phủ che mã mời (lần {attempt + 1})")
                code = _scan_invite_code(page, email)
                if not code:
                    code = _harvest_via_copy_link(page, email, seconds=5)
            if not code:
                code = _scan_invite_code(page, email)
    except Exception as exc:
        if stop_requested() or str(exc) == "Đã dừng":
            raise
        code = ""
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    if code:
        _emit(on_progress, step="invite", detail=f"Mã giới thiệu {code}")
    else:
        _emit(on_progress, step="invite", detail="Chưa lấy được mã giới thiệu, tài khoản vẫn lưu bình thường")
    return code


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
        else:
            # Chưa mở Cài đặt: phím tắt Ctrl+, mở thẳng, khỏi bấm menu.
            try:
                page.keyboard.press("Control+,")
                _emit(on_progress, step="invite", detail="Đã bấm Ctrl+, để mở Cài đặt…")
            except Exception:
                pass
            page.wait_for_timeout(600)
            if _text_visible(page, SETTINGS_NAME):
                continue
            if _click_label(page, SETTINGS_NAME):
                _emit(on_progress, step="invite", detail="Đã mở Cài đặt…")
                page.wait_for_timeout(500)
                continue
        if not _click_label(page, MENU_BTN):
            _click_bottom_left_menu(page)
        page.wait_for_timeout(500)
    raise RuntimeError("Đã vào Muse nhưng chưa thấy Quy đổi mã lời mời")


def _parse_usage_text(text: str) -> dict[str, str]:
    """Đọc số liệu từ trang Mức sử dụng: % đã dùng, token còn lại, ngày làm mới.
    Chỉ đọc text, không mở trình duyệt, để test được riêng."""
    flat = (text or "").replace("\u00a0", " ")
    lines = [ln.strip() for ln in flat.splitlines() if ln.strip()]
    used_pct = ""
    for ln in lines:
        low = ln.lower()
        if "used" in low or "đã dùng" in low or "đã sử dụng" in low:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", ln)
            if m:
                used_pct = m.group(1).replace(",", ".")
                break
    if not used_pct:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", flat)
        if m:
            used_pct = m.group(1).replace(",", ".")
    left = ""
    for pattern in (
        r"([\d.,]+\s*(?:B|M|K|tỷ|triệu|tỉ)?)\s*tokens?\s*(?:left|remaining|còn lại)",
        r"(?:còn lại|remaining|left)\s*:?\s*([\d.,]+\s*(?:B|M|K|tỷ|triệu|tỉ)?)\s*tokens?",
        r"([\d.,]+\s*(?:tỷ|triệu|tỉ))\s*token",
    ):
        m = re.search(pattern, flat, re.I)
        if m:
            left = re.sub(r"\s+", " ", m.group(1)).strip()
            break
    reset = ""
    for ln in lines:
        low = ln.lower()
        if "reset" in low or "làm mới" in low or "tuần" in low or "week" in low:
            m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", ln)
            if m:
                reset = m.group(1)
                break
    if not reset:
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", flat)
        if m:
            reset = m.group(1)
    return {"used_pct": used_pct, "left": left, "reset": reset}


_USAGE_ROW_JS = r"""() => {
  const needles = ['mức sử dụng', 'usage'];
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


def _click_usage_row(page: Page) -> bool:
    """Bấm dòng Mức sử dụng trong Cài đặt. Dòng này có thể chỉ là div bấm được,
    không phải nút, nên bấm thường không trúng thì bấm bằng JS vào đúng dòng đó."""
    if _click_label(page, USAGE_NAME):
        return True
    for frame in _frames(page):
        try:
            if frame.evaluate(_USAGE_ROW_JS):
                return True
        except Exception:
            continue
    return False


def _usage_panel_text(page: Page) -> str:
    """Text trong hộp Cài đặt (nếu có) hoặc toàn trang, nếu có cả % và token."""
    try:
        dialogs = page.locator('[role="dialog"]')
        if dialogs.count() >= 1:
            text = (dialogs.first.inner_text() or "").strip()
        else:
            text = (page.locator("body").first.inner_text() or "").strip()
    except Exception:
        return ""
    if "%" in text and "token" in text.lower():
        return text
    return ""


def _open_usage(page: Page, on_progress: ProgressCallback | None = None) -> str:
    """Mở trang Mức sử dụng trong Cài đặt, trả về text của trang đó."""
    for _ in range(16):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        text = _usage_panel_text(page)
        if text:
            return text
        if _click_usage_row(page):
            _emit(on_progress, step="usage", detail="Đã bấm Mức sử dụng, chờ số liệu…")
            page.wait_for_timeout(800)
            continue
        if _text_visible(page, SETTINGS_NAME):
            if _click_label(page, GENERAL_NAME):
                page.wait_for_timeout(500)
                continue
        else:
            try:
                page.keyboard.press("Control+,")
                _emit(on_progress, step="usage", detail="Đã bấm Ctrl+, để mở Cài đặt…")
            except Exception:
                pass
            page.wait_for_timeout(600)
            if _text_visible(page, SETTINGS_NAME):
                continue
            if _click_label(page, SETTINGS_NAME):
                _emit(on_progress, step="usage", detail="Đã mở Cài đặt…")
                page.wait_for_timeout(500)
                continue
        if not _click_label(page, MENU_BTN):
            _click_bottom_left_menu(page)
        page.wait_for_timeout(500)
    raise RuntimeError("Đã vào Muse nhưng chưa thấy trang Mức sử dụng")


def _load_saved_cookies(email: str) -> list[dict]:
    """Cookie da luu cua TK (dang Playwright) de mo lai Muse; het thi lay tu cookie_header."""
    rec = get_account_cookies(email) or {}
    cookies = [c for c in (rec.get("cookies") or []) if isinstance(c, dict) and c.get("name") and c.get("value")]
    if not cookies:
        for part in (rec.get("cookie_header") or "").split(";"):
            piece = part.strip()
            if "=" not in piece:
                continue
            name, value = piece.split("=", 1)
            name, value = name.strip(), value.strip()
            if name and value:
                cookies.append({"name": name, "value": value, "domain": ".muse.ai", "path": "/"})
    return cookies


def redeem_invite_for_account(
    email: str,
    code: str,
    *,
    proxy: str | None = None,
    headless: bool = True,
    on_progress: ProgressCallback | None = None,
    browser_name: str = "auto",
) -> dict:
    """Chạy lại quy đổi mã mời cho 1 tài khoản đã tạo: dùng cookie đã lưu mở Muse,
    vào Cài đặt -> Quy đổi mã lời mời và nhập mã cho trước. Chỉ nhập mã, không tạo tài khoản mới."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return {"ok": False, "email": email, "error": f"Chưa cài Playwright: {exc}"}
    code = (code or "").strip().upper()
    if not code:
        return {"ok": False, "email": email, "error": "Không có mã để nhập"}
    cookies = _load_saved_cookies(email)
    if not cookies:
        return {"ok": False, "email": email, "error": "Chưa có cookie"}
    _emit(on_progress, step="invite", detail=f"Quy đổi lại mã {code} cho {email}…")
    try:
        with sync_playwright() as p:
            proxy_cfg = parse_proxy(proxy)
            try:
                browser = _launch_browser(p, proxy_cfg, headless=headless, browser_name=browser_name)
            except Exception as exc:
                message = str(exc).split("╔", 1)[0].strip()
                return {"ok": False, "email": email, "error": message or "Không mở được trình duyệt"}
            try:
                context = _new_browser_context(browser, proxy_cfg, browser_name)
                page = context.new_page()
                page.set_default_timeout(20000)
                try:
                    context.add_cookies(cookies)
                except Exception:
                    pass
                page.goto(MUSE_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(1500)
                if not _muse_app_visible(page):
                    return {"ok": False, "email": email, "error": "Cookie hết hạn hoặc chưa đăng nhập"}
                last_error: Exception | None = None
                redeemed_code = ""
                try:
                    _redeem_invite(page, code, on_progress)
                    redeemed_code = code
                except Exception as exc:
                    if stop_requested() or str(exc) == "Đã dừng":
                        raise
                    last_error = exc
                    if _should_retry_redeem(exc):
                        fallback = warehouse_invite_code(email, exclude_codes=(code,))
                        if fallback:
                            try:
                                _redeem_invite(page, fallback, on_progress)
                                redeemed_code = fallback
                            except Exception as exc2:
                                if stop_requested() or str(exc2) == "Đã dừng":
                                    raise
                                last_error = exc2
                if not redeemed_code:
                    return {"ok": False, "email": email, "error": str(last_error or "Không quy đổi được mã")}
                set_redeemed_invite_code(email, redeemed_code)
                return {"ok": True, "email": email, "code": redeemed_code}
            finally:
                browser.close()
    except Exception as exc:
        if stop_requested() or str(exc) == "Đã dừng":
            return {"ok": False, "email": email, "error": "Đã dừng"}
        return {"ok": False, "email": email, "error": str(exc)}


def check_token_usage(
    email: str,
    *,
    proxy: str | None = None,
    headless: bool = True,
    on_progress: ProgressCallback | None = None,
    browser_name: str = "auto",
) -> dict:
    """Dùng cookie đã lưu mở trang Mức sử dụng, đọc % đã dùng / token còn lại / ngày làm mới.
    Chỉ đọc, không tạo hay đổi tài khoản. Lưu kết quả vào cookie record của tài khoản."""
    from datetime import datetime, timezone

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return {"ok": False, "email": email, "error": f"Chưa cài Playwright: {exc}"}
    rec = get_account_cookies(email) or {}
    cookies = [c for c in (rec.get("cookies") or []) if isinstance(c, dict) and c.get("name") and c.get("value")]
    if not cookies:
        for part in (rec.get("cookie_header") or "").split(";"):
            piece = part.strip()
            if "=" not in piece:
                continue
            name, value = piece.split("=", 1)
            name, value = name.strip(), value.strip()
            if name and value:
                cookies.append({"name": name, "value": value, "domain": ".muse.ai", "path": "/"})
    if not cookies:
        return {"ok": False, "email": email, "error": "Chưa có cookie"}

    def _fail(message: str) -> dict:
        try:
            update_account_meta(email, token_error=message)
        except Exception:
            pass
        return {"ok": False, "email": email, "error": message}

    _emit(on_progress, step="usage", detail=f"Kiểm tra token tuần {email}…")
    try:
        with sync_playwright() as p:
            proxy_cfg = parse_proxy(proxy)
            try:
                browser = _launch_browser(p, proxy_cfg, headless=headless, browser_name=browser_name)
            except Exception as exc:
                message = str(exc).split("╔", 1)[0].strip()
                return _fail(message or "Không mở được trình duyệt")
            try:
                context = _new_browser_context(browser, proxy_cfg, browser_name)
                page = context.new_page()
                page.set_default_timeout(20000)
                try:
                    context.add_cookies(cookies)
                except Exception:
                    pass
                page.goto(MUSE_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(1500)
                if not _muse_app_visible(page):
                    return _fail("Cookie hết hạn hoặc chưa đăng nhập")
                text = _open_usage(page, on_progress)
                parsed = _parse_usage_text(text)
                if not parsed["used_pct"] and not parsed["left"]:
                    return _fail("Đã mở Mức sử dụng nhưng chưa đọc được số liệu")
                checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                update_account_meta(
                    email,
                    token_used_pct=parsed["used_pct"],
                    token_left=parsed["left"],
                    token_reset=parsed["reset"],
                    token_checked_at=checked_at,
                    token_error="",
                )
                return {"ok": True, "email": email, **parsed, "checked_at": checked_at}
            finally:
                browser.close()
    except Exception as exc:
        if stop_requested() or str(exc) == "Đã dừng":
            return {"ok": False, "email": email, "error": "Đã dừng"}
        return _fail(str(exc))


def _dialog_scope(page: Page):
    """Phạm vi hộp thoại đang mở, để không đụng trang phía sau (khung chat vẫn có ô nhập)."""
    try:
        dialogs = page.locator('[role="dialog"]')
        if dialogs.count() >= 1:
            return dialogs.first
    except Exception:
        pass
    return page


def _fill_redeem_code(page: Page, code: str) -> bool:
    """Điền mã 6 ký tự vào hộp thoại quy đổi, có đọc lại kiểm chứng.
    Hộp thoại có 6 ô riêng (mỗi ô 1 ký tự): thử lần lượt điền từng ô -> bấm ô đầu rồi gõ như người dùng (các ô tự nhảy) -> điền một ô duy nhất chứa cả mã.
    Chỉ dùng phạm vi hộp thoại [role="dialog"]; không có hộp thoại thì dùng ô nhập tìm được như cũ."""
    try:
        dialogs = page.locator('[role="dialog"]')
        has_dialog = dialogs.count() >= 1
    except Exception:
        has_dialog = False
    if not has_dialog:
        field = _find_invite_input(page)
        if field is None:
            return False
        _type_invite_code(field, code)
        return True
    scope = dialogs.first
    try:
        boxes = scope.locator("input:visible")
        count = boxes.count()
    except Exception:
        count = 0
    if count == 0:
        return False

    def total_filled() -> int:
        try:
            return sum(len(boxes.nth(index).input_value() or "") for index in range(boxes.count()))
        except Exception:
            return -1

    if count == len(code) and count > 1:
        try:
            for index, char in enumerate(code):
                boxes.nth(index).fill(char)
        except Exception:
            pass
        if total_filled() == len(code):
            return True
    try:
        boxes.first.click(timeout=5000)
        page.keyboard.type(code, delay=80)
    except Exception:
        pass
    if total_filled() == len(code):
        return True
    if count == 1:
        try:
            boxes.first.fill(code)
        except Exception:
            pass
        return total_filled() == len(code)
    return False


def _should_retry_redeem(exc: Exception) -> bool:
    """Chỉ thử lại khi lỗi có vẻ do mã bị Muse từ chối (nút Xác nhận không sáng,
    hoặc Muse báo không nhận mã). Lỗi mở trang / điền mã thì thử lại cũng hỏng, không phí thêm 1 vòng."""
    text = str(exc)
    return "không nhận mã" in text or "chưa sáng" in text


def _redeem_invite(page: Page, code: str, on_progress: ProgressCallback | None) -> None:
    _emit(on_progress, step="invite", detail=f"Vào Cài đặt, quy đổi mã {code}")
    _open_redeem(page, on_progress)
    if not _fill_redeem_code(page, code):
        raise RuntimeError("Đã mở Quy đổi mã lời mời nhưng chưa điền được mã")
    page.wait_for_timeout(400)
    clicked = False
    for _ in range(20):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if _click_enabled_button(page, REDEEM_OK):
            clicked = True
            break
        page.wait_for_timeout(500)
    if not clicked:
        raise RuntimeError(f"Nút Xác nhận chưa sáng sau khi nhập mã {code}")
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
    on_redeem: Callable[[str | None], None] | None = None,
    allow_redeem_fallback: bool = True,
) -> tuple[str, str]:
    """Lay ma gioi thieu cua tai khoan nay, luu cookie, roi quy doi ma cua tai khoan truoc.
    Khong lay duoc ma rieng thi tai khoan van duoc luu (ma trong, chuoi roi ve ma gan nhat / ma goc); chi loi quy doi moi la ghi chu, khong lam hong tai khoan."""
    _wait_inside_muse(page, on_progress)
    own = _harvest_invite_code(page, email, on_progress)
    if not own:
        _emit(on_progress, step="invite", detail="Chưa lấy được mã giới thiệu, tài khoản vẫn lưu bình thường")
    if on_invite is not None:
        on_invite(own)
    if own:
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
        redeemed_code = ""
        last_error: Exception | None = None
        try:
            _redeem_invite(page, prior, on_progress)
            redeemed_code = prior
        except Exception as exc:
            if stop_requested() or str(exc) == "Đã dừng":
                raise
            last_error = exc
            # Mã bị Muse từ chối: thử lại 1 lần bằng mã trong kho (khác mã vừa thử và khác mã riêng).
            # Buff thì không thử mã khác: tài khoản chỉ được nhập đúng mã buff, không thì ghi chú lỗi.
            if allow_redeem_fallback and _should_retry_redeem(exc):
                fallback = warehouse_invite_code(email, exclude_codes=(prior, own))
                if fallback:
                    try:
                        _redeem_invite(page, fallback, on_progress)
                        redeemed_code = fallback
                    except Exception as exc2:
                        if stop_requested() or str(exc2) == "Đã dừng":
                            raise
                        last_error = exc2
        if redeemed_code:
            if on_redeem is not None:
                on_redeem(redeemed_code)
        else:
            note = f"Mã {own} và cookie đã lưu. Chưa quy đổi được mã trước: {last_error}"
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
        if _text_visible(page, SIGNUP_BLOCKED) or _text_visible(page, SIGNUP_WRONG):
            page.wait_for_timeout(2000)
            if _text_visible(page, SIGNUP_BLOCKED) or _text_visible(page, SIGNUP_WRONG):
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
        return
    # Kẹt ở trang ngày sinh quá 30 giây mà không thấy trang thẻ: không trả về
    # im lặng nữa (trước đây flow chạy tiếp sang bước mã mời trên trang kẹt,
    # lỗi sau đó không còn là lỗi proxy nên proxy không bị khóa, Chrome thì
    # đứng im ở dialog). Coi như proxy bị Muse chặn: khóa proxy, đóng Chrome,
    # lấy proxy khác tạo TK mới.
    raise RuntimeError(
        "Muse chặn tạo tài khoản mới (kẹt ở trang ngày sinh quá 30 giây, không thấy trang thẻ)."
        " Thử proxy khác."
    )


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
    on_redeem: Callable[[str | None], None] | None = None,
    prepare_mail: Callable[[], tuple[str, str, str | None]] | None = None,
    allow_redeem_fallback: bool = True,
    mail_provider: str = "tempmail",
    browser_name: str = "auto",
) -> LoginProgress:
    """Chrome mới, nhận proxy. Mail tạo sau bước mở Chrome."""
    from mail_provider import client as mail_client, error_class as mail_error_class, label as mail_label, normalize_provider, requests_proxies

    _mail_provider = normalize_provider(mail_provider)
    _mc = mail_client(_mail_provider)
    wait_for_code = _mc.wait_for_code
    _MailError = mail_error_class(_mail_provider)
    _mail_label = mail_label(_mail_provider)
    _mail_proxies = requests_proxies(proxy)

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
    # Tao mail TRUOC khi mo Chrome (v122): tao mail co the cham (generator.email
    # thu proxy roi truc tiep, doi domain nhieu lan). Truoc day mail tao SAU khi
    # mo Chrome nen Chrome nam o about:blank trong suot luc tao mail, user tuong
    # Chrome hong. Gio mail xong moi mo Chrome, mo xong la vao muse.ai ngay.
    if prepare_mail is not None:
        _emit(on_progress, step="mail", detail="Đang tạo hộp thư…")
        try:
            email, mail_token, prepared_password = prepare_mail()
        except Exception as exc:
            message = str(exc)
            err = LoginProgress(step="error", error=message or "Không tạo được hộp thư")
            _emit(on_progress, **err.__dict__)
            return err
        if prepared_password:
            account_password = prepared_password
        _emit(
            on_progress,
            step="mail",
            detail=f"Hộp thư {email} đọc lại được. Bắt đầu tạo tài khoản.",
        )
    if not email or not mail_token:
        err = LoginProgress(step="error", error="Chưa có mail đăng nhập được, chưa tạo tài khoản")
        _emit(on_progress, **err.__dict__)
        return err
    _emit(
        on_progress,
        step="starting",
        detail=f"Mở Chrome mới qua proxy {proxy_cfg['server']}" if proxy_cfg else "Mở Chrome mới…",
    )
    with sync_playwright() as p:
        try:
            browser = _launch_browser(p, proxy_cfg, headless=False, browser_name=browser_name)
        except Exception as exc:
            message = str(exc).split("╔", 1)[0].strip()
            err = LoginProgress(step="error", error=message or "Không mở được Chrome")
            _emit(on_progress, **err.__dict__)
            return err
        context = _new_browser_context(browser, proxy_cfg, browser_name)
        try:
            context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            _emit(on_progress, step="open_muse", detail="Tải muse.ai…")
            page.goto(MUSE_URL, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("load", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            _emit(on_progress, step="email", detail=f"Chờ Muse load xong rồi nhập {email}")
            _submit_signup_email(page, email)
            _emit(on_progress, step="wait_code", detail=f"Chờ mã trong {_mail_label}…")
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
            code = wait_for_code(mail_token, timeout=120, proxies=_mail_proxies)
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
            own_invite, invite_note = _chain_invites(page, email, on_progress, take_prior, on_invite, context, on_redeem, allow_redeem_fallback)
            if own_invite:
                done_detail = f"Đã tạo {email}. Mã giới thiệu {own_invite} đã vào tool. Cookie đã lưu."
            else:
                done_detail = f"Đã tạo {email}. Chưa lấy được mã giới thiệu. Cookie đã lưu."
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
            if isinstance(exc, _MailError):
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
    browser_name: str = "auto",
) -> LoginProgress:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return LoginProgress(step="error", error=f"Chưa cài Playwright: {exc}")

    # Nha cung cap mail suy ra tu domain cua email (TK tao bang mail.tm thi
    # dang nhap lai cung bang mail.tm, TK tempmail.plus thi bang tempmail.plus).
    from mail_provider import (
        client as mail_client,
        error_class as mail_error_class,
        label as mail_label,
        provider_for_email,
        requests_proxies,
    )

    _mail_proxies = requests_proxies(proxy)
    _mail_provider = provider_for_email(email, proxies=_mail_proxies)
    _mc = mail_client(_mail_provider)
    login_mailbox = _mc.login_mailbox
    message_ids = _mc.message_ids
    wait_for_code = _mc.wait_for_code
    _MailError = mail_error_class(_mail_provider)
    _mail_label = mail_label(_mail_provider)

    result = LoginProgress(step="starting")
    _emit(on_progress, step="starting", detail="Mở trình duyệt…")

    known_ids = meta_message_ids(email)
    mailbox_password = (mailtm_password or "").strip()
    if _mail_provider in ("gmail", "yahoo"):
        # Mat khau ung dung chi nam trong gmail_accounts.json /
        # yahoo_accounts.json (file rieng, gitignore) — kho TK chi luu
        # ACCOUNT_PASSWORD chung, nen lay lai mat khau that theo email
        # truoc khi mo hop thu.
        try:
            _store = (
                __import__("gmail_store")
                if _mail_provider == "gmail"
                else __import__("yahoo_store")
            )
            mailbox_password = _store.get_app_password(email) or ""
        except Exception:
            mailbox_password = ""
    mail_token = ""
    known_mail_ids: set[str] = set()
    if mailbox_password:
        _emit(on_progress, step="mail", detail=f"Mở hộp thư {_mail_label} để lấy mã lần sau")
        try:
            mail_token = login_mailbox(email, mailbox_password, proxies=_mail_proxies)
            known_mail_ids = message_ids(mail_token, proxies=_mail_proxies)
        except _MailError as exc:
            err = LoginProgress(step="error", error=str(exc))
            _emit(on_progress, **err.__dict__)
            return err

    with sync_playwright() as p:
        proxy_cfg = parse_proxy(proxy)
        if proxy_cfg:
            _emit(on_progress, step="proxy", detail=f"Dùng proxy {proxy_cfg['server']}")
        try:
            browser = _launch_browser(p, proxy_cfg, headless=headless, browser_name=browser_name)
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
        context = _new_browser_context(browser, proxy_cfg, browser_name)
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
                        _emit(on_progress, step="fetch_otp", detail=f"Lấy mã mới từ {_mail_label}…")
                        try:
                            email_code = wait_for_code(mail_token, timeout=120, exclude_ids=known_mail_ids, proxies=_mail_proxies)
                        except _MailError as exc:
                            if "hết phiên" not in str(exc):
                                raise
                            mail_token = login_mailbox(email, mailbox_password, proxies=_mail_proxies)
                            email_code = wait_for_code(mail_token, timeout=90, exclude_ids=known_mail_ids, proxies=_mail_proxies)
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


# --- Tài khoản META-MUSE (v133) ---
# Luồng 2 bước: (1) tạo TK Meta bằng IP của máy (KHÔNG proxy US) qua
# meta.ai -> auth.meta.com, dùng mail temp, sinh nhật đủ 18 tuổi, mật khẩu
# cố định Congphu21@; (2) dùng proxy US đăng nhập muse.ai bằng TK Meta đã lưu.
# User mô tả luồng (4 ảnh chụp 2026-11-12): "Đăng nhập hoặc tạo tài khoản"
# (Facebook / Instagram / "Dùng email hoặc số di động") -> nhập email ->
# "Bắt đầu trên Meta AI" (sinh nhật + mật khẩu) -> "Chỉnh sửa thông tin về
# Meta AI" (tên người dùng tự sinh) -> Xác nhận. IP Việt Nam không bị hỏi
# captcha ở các bước đầu, NHƯNG sau khi bấm Xác nhận có bước "Xác nhận bạn
# là người thật" (reCAPTCHA Enterprise — ảnh chụp user 2026-11-12 21:37):
# màn trung gian -> bấm Tiếp tục, rồi checkbox "Tôi không phải là người
# máy" (+ lưới chọn hình). create_meta_account xử lý bước này bằng dịch vụ
# giải captcha tự động (v132, cần key) và chờ giải tay tối đa 3 phút.
META_AI_URL = "https://www.meta.ai/"
# Vao thang auth.meta.com thay vi meta.ai (user 2026-11-12 23:20):
# nut "Dang nhap" o trang chu meta.ai bam tay thi duoc nhung tool bam
# khong duoc (khi dung proxy US) — vao thang trang dang nhap se bo qua
# luon buoc bam nut do. Link OIDC day du co waterfall_id/state/code_
# challenge/rcs dung 1 lan nen khong dung nguyen link user gui; vao
# https://auth.meta.com/ de Meta tao phien moi.
META_AUTH_URL = "https://auth.meta.com/"
_META_AUTH_TEXT = re.compile(r"Đăng nhập hoặc tạo tài khoản", re.I)
_META_START_TEXT = re.compile(r"Bắt đầu trên Meta AI", re.I)
_META_EDIT_TEXT = re.compile(r"Chỉnh sửa thông tin về Meta AI", re.I)
_META_EMAIL_TEXT = re.compile(r"Dùng email hoặc số di động", re.I)


_META_COOKIE_TEXT = re.compile(r"Cho phép tất cả cookie|Allow all cookies", re.I)
_META_EMAIL_HEADING = re.compile(r"Số di động hoặc email của bạn là gì|What is your mobile number or email", re.I)
_META_CREATE_NEW_TEXT = re.compile(r"^Tạo tài khoản mới$|^Create a new account$|^Bắt đầu$|^Get started$", re.I)
# Luong moi (vao thang auth.meta.com, user 2026-11-12 23:24): Meta tach
# thanh cac man rieng — "Ban ten la gi?" (ho + ten) roi "Ngay sinh cua
# ban la khi nao?" (Ngay/Thang/Nam rieng, khong co mat khau tren man),
# thay vi man gop "Bat dau tren Meta AI" (sinh nhat + mat khau).
_META_NAME_TEXT = re.compile(r"Bạn tên là gì|What.s your name", re.I)
_META_BIRTHDAY_TEXT = re.compile(r"Ngày sinh của bạn là khi nào|When.s your birthday", re.I)
# Man "Luu thong tin dang nhap?" (bam "Luu") va man tom tat "Hoan tat
# qua trinh tao tai khoan Meta" (tich o nhan email khuyen mai roi bam
# "Tao tai khoan") — user 2026-11-12 23:26/23:27.
_META_SAVE_LOGIN_TEXT = re.compile(r"Lưu thông tin đăng nhập|Save your login info", re.I)
_META_FINISH_TEXT = re.compile(r"Hoàn tất quá trình tạo tài khoản Meta|Complete.*Meta account", re.I)
_META_MARKETING_TEXT = re.compile(r"Tôi muốn nhận email về các đợt giảm giá", re.I)
# Man "Dong y voi dieu khoan va chinh sach cua chung toi" (co the
# xuat hien them truoc buoc xac nhan tai khoan — user 2026-11-12
# 23:35): bat cong tac Email quang cao (cung lua chon nhan email
# khuyen mai) roi bam "Toi dong y".
_META_TERMS_TEXT = re.compile(r"Đồng ý với điều khoản và chính sách|Agree to our terms", re.I)


def _dismiss_meta_cookie_banner(page: Page) -> bool:
    """Tat bang dong y cookie cua Meta ('Cho phép tất cả cookie') neu co.
    Bang nay hien tren profile moi va che man chon dang nhap — khong tat
    thi khong bam duoc nut email. Khong bao gio raise."""
    try:
        btn = page.get_by_role("button", name=_META_COOKIE_TEXT).first
        if btn.count() > 0 and btn.is_visible():
            btn.click()
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False


def _visible_email_input(page: Page):
    """Tra o nhap email DANG HIEN. O that cua auth.meta.com KHONG co
    placeholder (nhan 'Số di động hoặc email' la <label> noi, kieu
    floating label) nen tim bang label TRUOC; placeholder chi la du phong
    (trang gia / bien the). Trang co the co nhieu input cung nhan nen
    duyet tat ca de tim cai đang hiện. Tra ve locator cua o dang hien
    dau tien, hoac None neu chua co."""
    label_pat = re.compile(r"Số di động hoặc email|Mobile number or email", re.I)
    for locator in (
        page.get_by_label(label_pat),
        page.get_by_placeholder("Số di động hoặc email"),
        page.get_by_placeholder("Mobile number or email"),
    ):
        try:
            total = locator.count()
        except Exception:
            continue
        for index in range(min(total, 8)):
            el = locator.nth(index)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
    # Du phong cuoi: man nhap email cua Meta chi co 1 o text va khong co
    # placeholder; neu dang o dung man do ma label khong gan duoc thi lay
    # o text dang hien duy nhat (chi dung khi tieu de man email dang hien
    # de khoi nham o tim kiem o trang chu meta.ai).
    try:
        if _text_visible(page, _META_EMAIL_HEADING):
            inp = page.locator('input[type="text"]')
            total = inp.count()
            for index in range(min(total, 8)):
                el = inp.nth(index)
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _fill_email_input(page: Page, email: str) -> None:
    """Dien email vao o dang hien + xac minh gia tri da vao o. Co trang
    kieu controlled input "nuot" gia tri sau khi fill (con tro nhay
    trong o nhung o van trong) nen neu gia tri khong vao thi go lai tung
    ky tu; van khong vao thi bao loi ro thay vi chay tiep."""
    email_el = _visible_email_input(page)
    if email_el is None:
        raise RuntimeError("Không thấy ô nhập email")
    email_el.fill(email)
    try:
        _filled = email_el.input_value()
    except Exception:
        _filled = ""
    if _filled == email:
        return
    # Fill khong vao — go lai tung ky tu.
    try:
        email_el.click()
        email_el.press("ControlOrMeta+a")
        email_el.press_sequentially(email, delay=20)
        _filled = email_el.input_value()
    except Exception:
        _filled = ""
    if _filled != email:
        raise RuntimeError("Đã thấy ô nhập email nhưng không điền được mail vào (giá trị không vào ô)")


def _dismiss_open_dropdown(page: Page) -> None:
    """Dong dropdown dang mo (kieu div cua Meta): bam Escape, roi bam ra
    ngoai (tieu de trang) de kich hoat dong dropdown khi click ben
    ngoai. Escape mot minh khong du (user xac nhan v140 van bi che).
    Khong bao gio raise."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass
    try:
        heading = page.get_by_text("Bắt đầu trên Meta AI").first
        if heading.count() > 0 and heading.is_visible():
            heading.click(timeout=3000)
            page.wait_for_timeout(300)
    except Exception:
        pass


def _meta_pick_div_option(page: Page, label_text: str, wanted: list[str]) -> bool:
    """Chon 1 muc trong dropdown dang div: bam vao o co nhan label_text
    (vd 'Ngày'), roi bam vao muc co text nam trong wanted. Tra True neu
    chon duoc. (Du phong khi sinh nhat khong phai <select> that.)"""
    opened = page.evaluate(
        """([labelText, wanted]) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            let trigger = null;
            for (const el of document.querySelectorAll('div,button,span')) {
                const own = [...el.childNodes].filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join('').trim();
                if (norm(own) === norm(labelText)) { trigger = el; break; }
            }
            if (!trigger) return 'no-trigger';
            trigger.click();
            return 'opened';
        }""",
        [label_text, wanted],
    )
    if opened != "opened":
        return False
    page.wait_for_timeout(900)
    picked = page.evaluate(
        """(wanted) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const want = wanted.map(norm);
            for (const el of document.querySelectorAll('[role="option"], li, div, button, span')) {
                const t = norm(el.textContent);
                if (!t || t.length > 40) continue;
                if (want.includes(t)) { el.click(); return t; }
            }
            return '';
        }""",
        wanted,
    )
    if not picked:
        return False
    # Dropdown dang div cua Meta KHONG tu dong sau khi chon (anh user
    # 2026-11-12 22:45/22:50: dropdown Thang van mo, che nut Tiep tuc).
    # Dong bang Escape + bam ra ngoai, khong thi nut Tiep tuc bi che.
    _dismiss_open_dropdown(page)
    return True


def _meta_fill_birthday(page: Page, birthday: dict[str, list[str] | str]) -> None:
    """Dien sinh nhat (du 18 tuoi) vao 3 o Ngay/Thang/Nam. Thu <select>
    that truoc, roi thu dropdown dang div."""
    day_cands = list(birthday.get("day") or [])
    month_cands = list(birthday.get("month") or [])
    year_cands = list(birthday.get("year") or [])
    selects = page.locator("select")
    if selects.count() >= 3:
        for idx, cands in enumerate((day_cands, month_cands, year_cands)):
            done = False
            for cand in cands:
                for kw in ("label", "value"):
                    try:
                        selects.nth(idx).select_option(**{kw: str(cand)})
                        done = True
                        break
                    except Exception:
                        continue
                if done:
                    break
            if not done:
                raise RuntimeError(f"Không chọn được {'Ngày' if idx == 0 else 'Tháng' if idx == 1 else 'Năm'} sinh")
        return
    # Dropdown dang div (kieu facebook.com/r.php): CHI doi Nam.
    # Theo user 2026-11-12 22:58 — "chi thay doi nam thoi, ngay vs thang
    # k can sua": Ngay/Thang de mac dinh la duoc, khong mo dropdown
    # Thang de tranh no mo che nut Tiep tuc (anh image-127).
    if not _meta_pick_div_option(page, "Năm", [str(c) for c in year_cands]):
        raise RuntimeError("Không chọn được Năm sinh (không thấy dropdown)")
    # An toan: dong bat ky dropdown nao con mo truoc khi bam Tiep tuc.
    _dismiss_open_dropdown(page)


def _meta_accept_terms(page) -> bool:
    """Man "Dong y voi dieu khoan va chinh sach": bat cong tac Email
    quang cao (cung lua chon nhan email khuyen mai da tich o man tom
    tat) roi bam "Toi dong y". Tra True neu da bam, False neu khong
    thay man dieu khoan. Khong bao gio raise."""
    try:
        if not _text_visible(page, _META_TERMS_TEXT):
            return False
    except Exception:
        return False
    # Bat cong tac Email quang cao (best-effort).
    try:
        sw = page.get_by_role("switch").first
        if sw.count() > 0 and sw.is_visible():
            checked = (sw.get_attribute("aria-checked") or "").strip().lower()
            if checked != "true":
                sw.click(timeout=5000)
                page.wait_for_timeout(500)
    except Exception:
        pass
    try:
        if _click_enabled_button(page, re.compile(r"^Tôi đồng ý$|^I agree$", re.I)) or _click_button_js(page, re.compile(r"^Tôi đồng ý$|^I agree$", re.I)):
            page.wait_for_timeout(2000)
            return True
    except Exception:
        pass
    return False


def create_meta_account(
    *,
    mail_provider: str = "tempmail",
    browser_name: str = "auto",
    account_password: str | None = None,
    on_progress: ProgressCallback | None = None,
    entry_url: str | None = None,
    proxy: str | None = None,
    muse_after: bool = False,
) -> LoginProgress:
    """Tao 1 TK Meta: mo thang auth.meta.com -> dang nhap bang mail temp
    -> sinh nhat du 18 tuoi -> mat khau co dinh -> xac nhan ten nguoi
    dung Meta tu sinh (co the rong). proxy=None thi dung IP cua may
    (khong dung proxy); proxy='host:port[:user:pass]' thi Chrome mo qua
    proxy do nhung context van giu gio/ngon ngu Viet Nam (locale='vi').
    entry_url chi dung cho test (trang gia mo phong auth.meta.com).
    Vao thang auth.meta.com thay vi meta.ai de bo qua nut "Dang nhap" o
    trang chu (user 2026-11-12 23:20)."""
    from mail_provider import (
        client as mail_client,
        create_mailbox_for,
        error_class as mail_error_class,
        label as mail_label,
        normalize_provider,
    )

    _mail_provider = normalize_provider(mail_provider)
    _mc = mail_client(_mail_provider)
    wait_for_code = _mc.wait_for_code
    _MailError = mail_error_class(_mail_provider)
    _mail_label = mail_label(_mail_provider)
    password = (account_password or "Congphu21@").strip() or "Congphu21@"

    # Kiem tra proxy TRUOC khi tao mail (sai dang thi loi ngay, khong phi mail).
    proxy_cfg: dict[str, str] | None = None
    if proxy and proxy.strip():
        proxy_cfg = parse_proxy(proxy)
        if proxy_cfg is None:
            err = LoginProgress(step="error", error="Proxy không đúng dạng (host:port hoặc host:port:user:pass)")
            _emit(on_progress, **err.__dict__)
            return err

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return LoginProgress(step="error", error=f"Chưa cài Playwright: {exc}")

    # Tao mail TRUOC khi mo trinh duyet (kieu v122): tao mail co the cham.
    _emit(on_progress, step="mail", detail="Đang tạo hộp thư temp cho TK Meta…")
    try:
        email, _mb_pw, mail_token = create_mailbox_for(_mail_provider, password, "password", proxies=None)
    except Exception as exc:
        err = LoginProgress(step="error", error=str(exc) or "Không tạo được hộp thư")
        _emit(on_progress, **err.__dict__)
        return err
    if not email or not mail_token:
        err = LoginProgress(step="error", error="Chưa tạo được hộp thư temp")
        _emit(on_progress, **err.__dict__)
        return err
    _emit(on_progress, step="mail", detail=f"Hộp thư {email} đọc lại được. Bắt đầu tạo TK Meta.")

    _emit(on_progress, step="starting", detail="Mở Chrome qua proxy…" if proxy_cfg else "Mở Chrome bằng IP máy (không dùng proxy)…")
    with sync_playwright() as p:
        try:
            browser = _launch_browser(p, proxy_cfg, headless=False, browser_name=browser_name)
        except Exception as exc:
            message = str(exc).split("╔", 1)[0].strip()
            err = LoginProgress(step="error", error=message or "Không mở được Chrome")
            _emit(on_progress, **err.__dict__)
            return err
        context = _new_browser_context(browser, proxy_cfg, browser_name, locale="vi")
        try:
            context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            _emit(on_progress, step="open_meta", detail="Tải meta.ai…")
            page.goto(entry_url or META_AUTH_URL, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("load", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            # Tat bang dong y cookie neu co (profile moi hien bang nay va
            # che man chon dang nhap, khong tat thi khong bam duoc nut email).
            _dismiss_meta_cookie_banner(page)
            # Trang dang nhap Meta (auth.meta.com). Co 3 truong hop:
            # - Meta dua thang sang man nhap email (khong qua man chon);
            # - hien man chon cach dang nhap truoc;
            # - dung o trang chu meta.ai (co nut Dang nhap).
            # Bam nut Dang nhap NGAY khi thay (khong cho het vong lap)
            # roi cho 1 trong 2 man xuat hien.
            email_el = None
            clicked_login = False
            for _ in range(12):
                email_el = _visible_email_input(page)
                if email_el is not None:
                    break
                if _text_visible(page, _META_AUTH_TEXT):
                    break
                if not clicked_login:
                    # Nut Dang nhap trang chu co data-testid="login-button"
                    # (user gui HTML 2026-11-12 23:16) — tim bang testid
                    # truoc cho chac, roi moi tim theo role/text.
                    _clicked = False
                    try:
                        _lb = page.locator('[data-testid="login-button"]').first
                        if _lb.count() > 0 and _lb.is_visible():
                            try:
                                _lb.click(timeout=5000)
                            except Exception:
                                _lb.evaluate("el => el.click()")
                            _clicked = True
                    except Exception:
                        _clicked = False
                    if not _clicked:
                        _clicked = _click_enabled_button(page, re.compile(r"Đăng nhập|Log in", re.I)) or _click_button_js(page, re.compile(r"Đăng nhập|Log in", re.I))
                    if _clicked:
                        clicked_login = True
                        page.wait_for_timeout(2000)
                        continue
                page.wait_for_timeout(2500)
            if email_el is None:
                if not _text_visible(page, _META_AUTH_TEXT):
                    raise RuntimeError("Không vào được trang đăng nhập Meta (meta.ai / auth.meta.com)")
                _emit(on_progress, step="email", detail="Chọn đăng nhập bằng email…")
                # Bang cookie co the hien sau khi chuyen sang auth.meta.com
                # — tat truoc khi bam nut email.
                _dismiss_meta_cookie_banner(page)
                # Bam nut email roi cho man nhap email hien. Chi bam lai
                # khi URL khong doi + man chon van hien (click khong chuyen
                # trang); neu URL doi roi thi chi cho o nhap hien, khong
                # bam lai de khoi khoi dong lai chuyen trang dang chay.
                for _attempt in range(3):
                    try:
                        _url_before = page.url or ""
                    except Exception:
                        _url_before = ""
                    _click_enabled_button(page, _META_EMAIL_TEXT, reverse_text=True)
                    for _tick in range(12):
                        email_el = _visible_email_input(page)
                        if email_el is not None:
                            break
                        try:
                            _url_now = page.url or ""
                        except Exception:
                            _url_now = ""
                        if _tick >= 3 and _text_visible(page, _META_AUTH_TEXT) and _url_now == _url_before:
                            break
                        page.wait_for_timeout(2500)
                    if email_el is not None:
                        break
                    try:
                        _url_now = page.url or ""
                    except Exception:
                        _url_now = ""
                    if _url_now != _url_before:
                        # Da chuyen trang: cho them 20s cho o nhap hien.
                        for _ in range(8):
                            email_el = _visible_email_input(page)
                            if email_el is not None:
                                break
                            page.wait_for_timeout(2500)
                        break
                if email_el is None:
                    try:
                        _url = page.url or ""
                    except Exception:
                        _url = ""
                    _chooser = _text_visible(page, _META_AUTH_TEXT)
                    raise RuntimeError(
                        "Bấm 'Dùng email hoặc số di động' xong nhưng không thấy ô nhập email"
                        f" (trang: {_url or 'không rõ'}; màn chọn còn hiện: {'có' if _chooser else 'không'})"
                    )
            # Dien mail vao o dang hien (co kiem tra gia tri da vao o).
            _fill_email_input(page, email)
            _emit(on_progress, step="email", detail=f"Đã nhập {email} — chờ Meta tạo TK…")
            if not _click_enabled_button(page, BTN_NEXT):
                raise RuntimeError("Không bấm được Tiếp tục sau khi nhập email")
            # Xu ly cac man trung gian cho toi khi toi man xac nhan ten
            # nguoi dung ("Chinh sua thong tin ve Meta AI"). Co 2 luong:
            # - Luong cu: man gop "Bat dau tren Meta AI" (sinh nhat +
            #   mat khau cung man).
            # - Luong moi (auth.meta.com truc tiep): man "Ban ten la
            #   gi?" -> man "Ngay sinh cua ban la khi nao?" -> man mat
            #   khau rieng -> man xac nhan ten.
            # Giua duong co the hoi ma OTP hoac man xac nhan "Tao tai
            # khoan Meta moi?" (bam "Tao tai khoan moi"/"Bat dau").
            birthday = _random_birthday()
            person = _random_person()
            clicked_create_new = False
            confirmed = False
            finish_clicked = False
            username = ""
            for _ in range(40):
                if stop_requested():
                    raise RuntimeError("Đã dừng")
                # Trang Meta Quest (auth.meta.com/language, trang
                # trang) — cu load lai cho toi khi hien man xac nhan
                # nguoi that (user 2026-11-12 23:52). Khong gioi han
                # 1 lan; vong lap 40 lan gioi han tong so lan load.
                try:
                    _u = page.url or ""
                except Exception:
                    _u = ""
                if "auth.meta.com/language" in _u:
                    _emit(on_progress, step="verify", detail="Thấy trang Meta Quest — tải lại trang để ra màn xác nhận…")
                    try:
                        # Di thang toi link (navigation moi) thay vi chi
                        # bam tai lai — buoc Meta chuyen sang man xac
                        # nhan nguoi that.
                        page.goto(_u, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    page.wait_for_timeout(5000)
                    continue
                # Man xac nhan nguoi that co the hien ngay tai day
                # (sau khi load lai trang Meta Quest) — chuyen sang
                # vong xac minh (captcha) o duoi.
                if _text_visible(page, HUMAN_GATE):
                    confirmed = True
                    break
                # Man xac nhan ten nguoi dung -> xong vong lap.
                if _text_visible(page, _META_EDIT_TEXT):
                    confirmed = True
                    break
                # Man gop (luong cu): sinh nhat + mat khau cung man.
                if _text_visible(page, _META_START_TEXT):
                    _emit(on_progress, step="birthday", detail="Điền sinh nhật đủ 18 tuổi…")
                    _meta_fill_birthday(page, birthday)
                    pw = page.get_by_placeholder("Mật khẩu")
                    if pw.count() == 0:
                        pw = page.locator('input[type="password"]').first
                    pw.wait_for(state="visible", timeout=15000)
                    pw.fill(password)
                    _emit(on_progress, step="password", detail="Đã điền mật khẩu — bấm Tiếp tục…")
                    # Nut Tiep tuc co the bi dropdown sinh nhat dang mo
                    # che: click() thuong that bai vi bi che, fallback
                    # sang bam JS.
                    if not _click_enabled_button(page, BTN_NEXT) and not _click_button_js(page, BTN_NEXT):
                        raise RuntimeError("Không bấm được Tiếp tục sau khi nhập mật khẩu")
                    page.wait_for_timeout(2000)
                    continue
                # Man ten (luong moi): dien ho ten.
                if _text_visible(page, _META_NAME_TEXT):
                    _emit(on_progress, step="name", detail="Điền họ tên…")
                    _fill_name(page, person)
                    if not _click_enabled_button(page, BTN_NEXT) and not _click_button_js(page, BTN_NEXT):
                        raise RuntimeError("Không bấm được Tiếp sau khi nhập tên")
                    page.wait_for_timeout(2000)
                    continue
                # Man sinh nhat rieng (luong moi): dien sinh nhat.
                if _text_visible(page, _META_BIRTHDAY_TEXT):
                    _emit(on_progress, step="birthday", detail="Điền sinh nhật đủ 18 tuổi…")
                    _meta_fill_birthday(page, birthday)
                    if not _click_enabled_button(page, BTN_NEXT) and not _click_button_js(page, BTN_NEXT):
                        raise RuntimeError("Không bấm được Tiếp sau khi nhập ngày sinh")
                    page.wait_for_timeout(2000)
                    continue
                # Man "Luu thong tin dang nhap?" (luong moi): bam "Luu".
                if _text_visible(page, _META_SAVE_LOGIN_TEXT):
                    _emit(on_progress, step="save_login", detail="Lưu thông tin đăng nhập…")
                    if not _click_enabled_button(page, re.compile(r"^Lưu$|^Save$", re.I)) and not _click_button_js(page, re.compile(r"^Lưu$|^Save$", re.I)):
                        raise RuntimeError("Không bấm được Lưu")
                    page.wait_for_timeout(2000)
                    continue
                # Man tom tat "Hoan tat qua trinh tao tai khoan Meta"
                # (luong moi): tich o nhan email khuyen mai Meta Quest
                # roi bam "Tao tai khoan". Xong thi thoat vong lap, de
                # vong xac minh lo phan con lai (nguoi that / meta.ai).
                if _text_visible(page, _META_FINISH_TEXT):
                    _emit(on_progress, step="finish", detail="Tích ô nhận email, tạo tài khoản…")
                    try:
                        _cb = page.get_by_role("checkbox", name=_META_MARKETING_TEXT).first
                        if _cb.count() > 0 and not _cb.is_checked():
                            _cb.check(timeout=5000)
                    except Exception:
                        pass
                    if not _click_enabled_button(page, re.compile(r"^Tạo tài khoản$|^Create account$", re.I)) and not _click_button_js(page, re.compile(r"^Tạo tài khoản$|^Create account$", re.I)):
                        raise RuntimeError("Không bấm được Tạo tài khoản")
                    page.wait_for_timeout(2000)
                    finish_clicked = True
                    confirmed = True
                    break
                # Man dieu khoan (co the xuat hien them): bam "Toi dong y".
                if _text_visible(page, _META_TERMS_TEXT):
                    _emit(on_progress, step="terms", detail="Đồng ý với điều khoản…")
                    _meta_accept_terms(page)
                    page.wait_for_timeout(2000)
                    continue
                # Man xac nhan tao TK moi.
                if not clicked_create_new and _text_visible(page, _META_CREATE_NEW_TEXT):
                    _emit(on_progress, step="email", detail="Xác nhận tạo tài khoản Meta mới…")
                    if _click_enabled_button(page, _META_CREATE_NEW_TEXT) or _click_button_js(page, _META_CREATE_NEW_TEXT):
                        clicked_create_new = True
                        page.wait_for_timeout(2000)
                        continue
                    clicked_create_new = True
                if _text_visible(page, re.compile(r"captcha|Tôi không phải là người máy|robot", re.I)):
                    raise RuntimeError("Gặp captcha lúc tạo TK Meta — thử lại sau hoặc đổi IP")
                # Man mat khau rieng (luong moi): chi co o mat khau,
                # khong co sinh nhat (man gop da xu ly o tren nen chi
                # con man rieng toi duoc day).
                pw_only = page.locator('input[type="password"]').first
                if pw_only.count() > 0 and pw_only.is_visible():
                    _emit(on_progress, step="password", detail="Điền mật khẩu…")
                    pw_only.fill(password)
                    if not _click_enabled_button(page, BTN_NEXT) and not _click_button_js(page, BTN_NEXT):
                        raise RuntimeError("Không bấm được Tiếp sau khi nhập mật khẩu")
                    page.wait_for_timeout(2000)
                    continue
                otp = page.locator('input[autocomplete="one-time-code"]')
                if otp.count() > 0 and otp.first.is_visible():
                    _emit(on_progress, step="wait_code", detail=f"Chờ mã trong {_mail_label}…")
                    code = wait_for_code(mail_token, timeout=120, proxies=None)
                    otp.first.fill(code)
                    _emit(on_progress, step="enter_otp", detail="Đã nhập mã — bấm Tiếp tục…")
                    if not _click_enabled_button(page, BTN_NEXT):
                        raise RuntimeError("Không bấm được Tiếp tục sau khi nhập mã")
                    page.wait_for_timeout(2000)
                page.wait_for_timeout(5000)
            if not confirmed:
                raise RuntimeError("Nhập email xong nhưng không tới được màn xác nhận tên")
            if not finish_clicked and _text_visible(page, _META_EDIT_TEXT):
                # Luong cu: man xac nhan ten nguoi dung — doc ten tu
                # sinh roi bam "Xac nhan". Luong moi (man tom tat) da
                # bam "Tao tai khoan" trong vong lap nen bo qua. Neu
                # vong lap thoat o man xac nhan nguoi that (HUMAN_GATE)
                # thi cung bo qua, vong xac minh duoi se xu ly.
                try:
                    # Ten nguoi dung Meta tu sinh nam trong o input gan nhan
                    # "Tên người dùng" — doc gia tri neu tim thay.
                    for frame in _frames(page):
                        try:
                            loc = frame.get_by_label(re.compile(r"Tên người dùng", re.I))
                            if loc.count() > 0:
                                username = (loc.first.input_value() or "").strip()
                                break
                        except Exception:
                            continue
                except Exception:
                    username = ""
                _emit(on_progress, step="confirm", detail="Xác nhận tạo TK Meta…")
                if not _click_enabled_button(page, re.compile(r"^Xác nhận$", re.I)):
                    raise RuntimeError("Không bấm được Xác nhận")
            # Sau khi bam Xac nhan / Tao tai khoan: Meta hien buoc "Xac nhan ban la nguoi that"
            # (reCAPTCHA Enterprise, anh chup user 2026-11-12 21:37) truoc khi
            # tao xong TK: man trung gian -> bam Tiep tuc, roi checkbox
            # "Tôi không phải là người máy" (+ luoi chon hinh). Thu giai tu
            # dong 1 lan bang dich vu (v132, can key 2Captcha/CapSolver);
            # khong duoc thi doi user giai tay trong Chrome toi da 3 phut.
            # Thanh cong khi: het auth.meta.com (ve meta.ai) HOAC trang hien
            # "Chào mừng / Welcome to Meta AI".
            success = False
            tried_auto = False
            manual_deadline = 0.0
            for _ in range(100):
                if stop_requested():
                    raise RuntimeError("Đã dừng")
                url = page.url or ""
                if "meta.ai" in url and "auth.meta.com" not in url:
                    success = True
                    break
                # Trang Meta Quest (auth.meta.com/language, trang trang)
                # KHONG phai la tao xong — cu load lai cho toi khi hien
                # man "Xac nhan ban la nguoi that" (user 2026-11-12
                # 23:52). Khong gioi han 1 lan; vong lap 48 lan gioi
                # han tong so lan load.
                if "auth.meta.com/language" in url:
                    _emit(on_progress, step="verify", detail="Thấy trang Meta Quest — tải lại trang để ra màn xác nhận…")
                    try:
                        # Di thang toi link (navigation moi) thay vi chi
                        # bam tai lai.
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    page.wait_for_timeout(5000)
                    continue
                if _text_visible(page, re.compile(r"Chào mừng|Welcome to Meta AI", re.I)):
                    success = True
                    break
                # Man dieu khoan (co the xuat hien them sau khi bam Tao
                # tai khoan): bam "Toi dong y" roi moi toi buoc xac
                # nhan tai khoan (user 2026-11-12 23:35).
                if _text_visible(page, _META_TERMS_TEXT):
                    _emit(on_progress, step="terms", detail="Đồng ý với điều khoản…")
                    _meta_accept_terms(page)
                    page.wait_for_timeout(2000)
                    continue
                otp = page.locator('input[autocomplete="one-time-code"]')
                if otp.count() > 0 and otp.first.is_visible():
                    _emit(on_progress, step="wait_code", detail=f"Chờ mã trong {_mail_label}…")
                    code = wait_for_code(mail_token, timeout=120, proxies=None)
                    otp.first.fill(code)
                    if not _click_enabled_button(page, BTN_NEXT):
                        raise RuntimeError("Không bấm được Tiếp tục sau khi nhập mã")
                    page.wait_for_timeout(2000)
                    continue
                # Man trung gian "Xac nhan ban la nguoi that..." (chua co
                # khung recaptcha) -> bam Tiep tuc.
                if _text_visible(page, HUMAN_GATE) and not _captcha_visible(page):
                    _emit(on_progress, step="verify", detail="Thấy màn xác nhận người thật — bấm Tiếp tục…")
                    if _click_enabled_button(page, BTN_NEXT):
                        page.wait_for_timeout(2000)
                    else:
                        page.wait_for_timeout(2000)
                    continue
                # reCAPTCHA (checkbox / luoi chon hinh).
                if _captcha_visible(page) or _text_visible(page, re.compile(r"Tôi không phải là người máy", re.I)):
                    if not tried_auto:
                        tried_auto = True
                        _emit(on_progress, step="captcha", detail="Thấy reCAPTCHA — thử giải tự động bằng dịch vụ…")
                        ok, note = _try_auto_solve_captcha(page)
                        if ok:
                            _emit(on_progress, step="captcha", detail=f"Đã giải tự động bằng {note} — bấm Tiếp tục…")
                            page.wait_for_timeout(1500)
                            if _click_enabled_button(page, BTN_NEXT):
                                page.wait_for_timeout(2000)
                            continue
                        # Chen token khong duoc (hoac khong tich duoc o
                        # Enterprise) -> tool TU BAM vao o checkbox thay
                        # vi de user bam tay (user 2026-11-13 00:05).
                        _emit(on_progress, step="captcha", detail="Giải tự động chưa được — tool tự bấm ô captcha…")
                        if _click_recaptcha_checkbox(page):
                            page.wait_for_timeout(3000)
                            if _click_enabled_button(page, BTN_NEXT):
                                page.wait_for_timeout(2000)
                            continue
                        _emit(
                            on_progress,
                            step="needs_user",
                            detail=f"Không giải tự động được ({note}) — giải tay trong Chrome, tool chờ tối đa 3 phút.",
                        )
                        manual_deadline = time.time() + 180
                        continue
                    # Da thu tu dong roi: doi user giai tay. Giai xong thi
                    # nut Tiep tuc moi sang -> bam thu moi vong.
                    if manual_deadline and time.time() > manual_deadline:
                        raise RuntimeError("Chưa qua được xác nhận người thật (hết 3 phút chờ giải tay)")
                    if _click_enabled_button(page, BTN_NEXT):
                        page.wait_for_timeout(2000)
                    else:
                        page.wait_for_timeout(2000)
                    continue
                page.wait_for_timeout(5000)
            url = page.url or ""
            if not success and "auth.meta.com" in url:
                raise RuntimeError("Xác nhận xong nhưng chưa vào được Meta AI")
            result = LoginProgress(
                step="done",
                detail=f"Đã tạo TK Meta {email}" + (f" ({username})" if username else ""),
                code=username,
                final_url=url,
            )
            _emit(on_progress, **result.__dict__)
            if muse_after:
                # Mo muse.ai ngay tren cung trinh duyet (phien Meta con
                # dang nhap) — Muse se hien TK Meta vua tao de dang
                # nhap (user 2026-11-12 23:31). Luu cookie Muse.
                try:
                    muse_result = _muse_login_drive(
                        page, email, password,
                        on_progress=on_progress, proxy=proxy,
                    )
                    if muse_result.step == "done":
                        result = LoginProgress(
                            step="done",
                            detail=f"Đã tạo TK Meta {email} và đăng nhập Muse",
                            code=username,
                            cookies_count=muse_result.cookies_count,
                            final_url=muse_result.final_url or url,
                        )
                    else:
                        result = LoginProgress(
                            step="done",
                            detail=f"Đã tạo TK Meta {email} (chưa đăng nhập được Muse: {muse_result.error or ''})",
                            code=username,
                            final_url=url,
                        )
                    _emit(on_progress, **result.__dict__)
                except Exception as exc:
                    result = LoginProgress(
                        step="done",
                        detail=f"Đã tạo TK Meta {email} (mở Muse lỗi: {str(exc).strip()})",
                        code=username,
                        final_url=url,
                    )
                    _emit(on_progress, **result.__dict__)
            return result
        except Exception as exc:
            message = str(exc).strip() or "Không tạo được TK Meta"
            err = LoginProgress(step="error", error=message, final_url=page.url if page else None)
            _emit(on_progress, **err.__dict__)
            return err
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _muse_login_drive(
    page,
    email: str,
    password: str,
    *,
    on_progress: ProgressCallback | None = None,
    proxy: str | None = None,
    muse_url: str | None = None,
) -> LoginProgress:
    """Dang nhap muse.ai tren 1 trang da mo (dung chung phien Meta).
    Mo muse.ai -> neu da dang nhap (phien Meta con) thi luu cookie
    ngay; neu hien nut "Tiep tuc bang Meta" thi bam; neu hien o email
    thi nhap email -> Continue -> mat khau/OTP nhu thuong. Tra
    LoginProgress; khi xong step='done', cookies_count > 0.
    muse_url chi dung cho test."""
    _emit(on_progress, step="open_muse", detail="Mở muse.ai…")
    # Muse tai qua proxy US co the rat cham — cho toi da 4 phut de
    # trang tai xong (user 2026-11-12 23:40: "doi no load xong luon di").
    page.goto(muse_url or MUSE_URL, wait_until="domcontentloaded", timeout=240000)
    try:
        page.wait_for_load_state("load", timeout=120000)
    except Exception:
        pass
    page.wait_for_timeout(5000)
    try:
        login_btn = page.get_by_role("button", name=re.compile(r"^Log in$", re.I)).first
        if login_btn.count() > 0 and login_btn.is_visible():
            login_btn.click()
            page.wait_for_timeout(2000)
    except Exception:
        pass
    if _logged_in_now(page):
        cookies = page.context.cookies()
        record = save_account_cookies(email, cookies, cookie_source="meta-login")
        result = LoginProgress(
            step="done",
            detail=f"Đã đăng nhập Muse bằng TK Meta {email} (phiên Meta)",
            cookies_count=int(record.get("count") or 0),
            final_url=page.url,
        )
        _emit(on_progress, **result.__dict__)
        return result
    try:
        email_inp = page.get_by_placeholder("Mobile number or email")
        if email_inp.count() == 0:
            email_inp = page.locator("input").first
        email_inp.wait_for(state="visible", timeout=15000)
        email_inp.fill(email)
    except Exception:
        pass
    else:
        _emit(on_progress, step="email", detail=f"Đã nhập {email} — bấm Continue…")
        if not _click_enabled_button(page, BTN_NEXT):
            raise RuntimeError("Không bấm được Continue sau khi nhập email")
    mail_token: str | None = None
    wait_for_code = None
    _mail_proxies = None
    for _ in range(24):
        if stop_requested():
            raise RuntimeError("Đã dừng")
        if _logged_in_now(page):
            break
        if _text_visible(page, re.compile(r"Ngày sinh|Sinh nhật|Confirm your age|Thẻ|Enter a valid credit card", re.I)):
            raise RuntimeError("Muse không nhận TK Meta này — quay lại bước tạo TK Meta")
        meta_btn = page.get_by_text(re.compile(r"Tiếp tục bằng Meta|Continue with Meta", re.I))
        if meta_btn.count() > 0 and meta_btn.first.is_visible():
            _emit(on_progress, step="meta_login", detail="Thấy nút đăng nhập bằng Meta — bấm vào…")
            meta_btn.first.click()
            page.wait_for_timeout(2000)
            continue
        pw = page.locator('input[type="password"]')
        if pw.count() > 0 and pw.first.is_visible():
            _emit(on_progress, step="meta_password", detail="Nhập mật khẩu Meta…")
            pw.first.fill(password)
            if not _click_enabled_button(page, BTN_NEXT):
                raise RuntimeError("Không bấm được Tiếp tục sau khi nhập mật khẩu Meta")
            page.wait_for_timeout(2000)
            continue
        otp = page.locator('input[autocomplete="one-time-code"]')
        if otp.count() > 0 and otp.first.is_visible():
            if wait_for_code is None:
                from mail_provider import (
                    client as mail_client,
                    provider_for_email,
                    requests_proxies,
                )

                _mail_proxies = requests_proxies(proxy)
                _prov = provider_for_email(email, proxies=_mail_proxies)
                _mc = mail_client(_prov)
                wait_for_code = _mc.wait_for_code
                try:
                    mail_token = _mc.login_mailbox(email, password, proxies=_mail_proxies)
                except Exception:
                    mail_token = email
            _emit(on_progress, step="wait_code", detail="Chờ mã đăng nhập gửi về mail…")
            code = wait_for_code(mail_token or email, timeout=120, proxies=_mail_proxies)
            otp.first.fill(code)
            if not _click_enabled_button(page, BTN_NEXT):
                raise RuntimeError("Không bấm được Tiếp tục sau khi nhập mã")
            page.wait_for_timeout(2000)
            continue
        page.wait_for_timeout(2500)
    if not _logged_in_now(page):
        raise RuntimeError("Đăng nhập xong nhưng chưa vào được Muse")
    cookies = page.context.cookies()
    record = save_account_cookies(email, cookies, cookie_source="meta-login")
    result = LoginProgress(
        step="done",
        detail=f"Đã đăng nhập Muse bằng TK Meta {email}",
        cookies_count=int(record.get("count") or 0),
        final_url=page.url,
    )
    _emit(on_progress, **result.__dict__)
    return result


def login_muse_with_meta(
    email: str,
    password: str,
    *,
    proxy: str | None = None,
    browser_name: str = "auto",
    on_progress: ProgressCallback | None = None,
    muse_url: str | None = None,
) -> LoginProgress:
    """Dang nhap muse.ai bang TK Meta da luu (dung proxy US): nhap email
    Meta -> Continue -> neu hien dang nhap Meta (auth.meta.com) thi nhap
    mat khau -> vao Muse, luu cookie. Tra LoginProgress; khi xong
    step='done' va cookies_count > 0. muse_url chi dung cho test."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return LoginProgress(step="error", error=f"Chưa cài Playwright: {exc}")

    proxy_cfg = parse_proxy(proxy) if (proxy or "").strip() else None
    if (proxy or "").strip() and not proxy_cfg:
        err = LoginProgress(step="error", error="Proxy không đúng dạng ip:port hoặc ip:port:user:pass")
        _emit(on_progress, **err.__dict__)
        return err

    _emit(on_progress, step="starting", detail=f"Mở Chrome qua proxy {proxy_cfg['server']}" if proxy_cfg else "Mở Chrome mới…")
    with sync_playwright() as p:
        try:
            browser = _launch_browser(p, proxy_cfg, headless=False, browser_name=browser_name)
        except Exception as exc:
            message = str(exc).split("╔", 1)[0].strip()
            err = LoginProgress(step="error", error=message or "Không mở được Chrome")
            _emit(on_progress, **err.__dict__)
            return err
        context = _new_browser_context(browser, proxy_cfg, browser_name)
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            _emit(on_progress, step="open_muse", detail="Tải muse.ai…")
            page.goto(muse_url or MUSE_URL, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("load", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            # Nut "Log in" tren trang chu (neu co) -> form dang nhap.
            try:
                login_btn = page.get_by_role("button", name=re.compile(r"^Log in$", re.I)).first
                if login_btn.count() > 0 and login_btn.is_visible():
                    login_btn.click()
                    page.wait_for_timeout(2000)
            except Exception:
                pass
            email_inp = page.get_by_placeholder("Mobile number or email")
            if email_inp.count() == 0:
                email_inp = page.locator("input").first
            email_inp.wait_for(state="visible", timeout=15000)
            email_inp.fill(email)
            _emit(on_progress, step="email", detail=f"Đã nhập {email} — bấm Continue…")
            if not _click_enabled_button(page, BTN_NEXT):
                raise RuntimeError("Không bấm được Continue sau khi nhập email")
            # Cho 1 trong: vao thang Muse / man dang nhap Meta / nut Meta / ma OTP.
            mail_token: str | None = None
            wait_for_code = None
            _mail_proxies = None
            for _ in range(24):
                if stop_requested():
                    raise RuntimeError("Đã dừng")
                if _logged_in_now(page):
                    break
                if _text_visible(page, re.compile(r"Ngày sinh|Sinh nhật|Confirm your age|Thẻ|Enter a valid credit card", re.I)):
                    raise RuntimeError("Muse không nhận TK Meta này — quay lại bước tạo TK Meta")
                meta_btn = page.get_by_text(re.compile(r"Tiếp tục bằng Meta|Continue with Meta", re.I))
                if meta_btn.count() > 0 and meta_btn.first.is_visible():
                    _emit(on_progress, step="meta_login", detail="Thấy nút đăng nhập bằng Meta — bấm vào…")
                    meta_btn.first.click()
                    page.wait_for_timeout(2000)
                    continue
                pw = page.locator('input[type="password"]')
                if pw.count() > 0 and pw.first.is_visible():
                    _emit(on_progress, step="meta_password", detail="Nhập mật khẩu Meta…")
                    pw.first.fill(password)
                    if not _click_enabled_button(page, BTN_NEXT):
                        raise RuntimeError("Không bấm được Tiếp tục sau khi nhập mật khẩu Meta")
                    page.wait_for_timeout(2000)
                    continue
                otp = page.locator('input[autocomplete="one-time-code"]')
                if otp.count() > 0 and otp.first.is_visible():
                    if wait_for_code is None:
                        from mail_provider import (
                            client as mail_client,
                            provider_for_email,
                            requests_proxies,
                        )

                        _mail_proxies = requests_proxies(proxy)
                        _prov = provider_for_email(email, proxies=_mail_proxies)
                        _mc = mail_client(_prov)
                        wait_for_code = _mc.wait_for_code
                        try:
                            mail_token = _mc.login_mailbox(email, password, proxies=_mail_proxies)
                        except Exception:
                            mail_token = email
                    _emit(on_progress, step="wait_code", detail="Chờ mã đăng nhập gửi về mail…")
                    code = wait_for_code(mail_token or email, timeout=120, proxies=_mail_proxies)
                    otp.first.fill(code)
                    if not _click_enabled_button(page, BTN_NEXT):
                        raise RuntimeError("Không bấm được Tiếp tục sau khi nhập mã")
                    page.wait_for_timeout(2000)
                    continue
                page.wait_for_timeout(2500)
            if not _logged_in_now(page):
                raise RuntimeError("Đăng nhập xong nhưng chưa vào được Muse")
            cookies = page.context.cookies()
            record = save_account_cookies(email, cookies, cookie_source="meta-login")
            result = LoginProgress(
                step="done",
                detail=f"Đã đăng nhập Muse bằng TK Meta {email}",
                cookies_count=int(record.get("count") or 0),
                final_url=page.url,
            )
            _emit(on_progress, **result.__dict__)
            return result
        except Exception as exc:
            message = str(exc).strip() or "Không đăng nhập được Muse bằng TK Meta"
            err = LoginProgress(step="error", error=message, final_url=page.url if page else None)
            _emit(on_progress, **err.__dict__)
            return err
        finally:
            try:
                browser.close()
            except Exception:
                pass
