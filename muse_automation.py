"""Playwright automation: Muse AI login with Lay2FA OTP."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from playwright.sync_api import Page

from cookie_store import cookies_to_header, filter_muse_cookies, save_account_cookies, update_account_meta
from lay2fa_client import meta_message_ids, wait_for_new_muse_code

MUSE_URL = "https://muse.ai/"
CONNECTED = re.compile(r"Connected|Đã kết nối|kết nối", re.I)
EMAIL_PLACEHOLDER = re.compile(r"email|di động|mobile", re.I)
BTN_CONTINUE = re.compile(r"Continue|Tiếp", re.I)
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


def _emit(cb: ProgressCallback | None, **kwargs: object) -> None:
    if cb:
        cb(LoginProgress(**kwargs))  # type: ignore[arg-type]


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


def _refresh_and_get_status_cookie(page: Page) -> str | None:
    """F5 → chờ https://muse.ai/api/consent/status → Cookie header (DevTools)."""

    def try_reload_with_expect() -> str | None:
        try:
            with page.expect_response(
                lambda r: _is_consent_status_url(r.url) and r.status == 200,
                timeout=90000,
            ) as resp_info:
                page.reload(wait_until="domcontentloaded", timeout=90000)
            return _cookie_from_consent_status_response(resp_info.value)
        except Exception:
            return None

    header = try_reload_with_expect()
    if header:
        return header

    captured: dict[str, str | None] = {"cookie": None}

    def on_response(response) -> None:
        if captured["cookie"]:
            return
        captured["cookie"] = _cookie_from_consent_status_response(response)

    page.on("response", on_response)
    try:
        page.reload(wait_until="domcontentloaded", timeout=90000)
        for _ in range(30):
            if captured["cookie"]:
                return captured["cookie"]
            page.wait_for_timeout(1000)
        header = try_reload_with_expect()
        if header:
            return header
        try:
            page.evaluate(
                "fetch('/api/consent/status', { credentials: 'include', cache: 'no-store' })"
            )
            for _ in range(15):
                if captured["cookie"]:
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


def parse_proxy(raw: str | None) -> dict[str, str] | None:
    """Turn ip:port, ip:port:user:pass, or scheme://user:pass@host:port into a Playwright proxy."""
    text = (raw or "").strip()
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
BTN_NEXT = re.compile(r"^Tiếp$|^Tiếp tục$|^Continue$", re.I)


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


def _select_recovery_code(page: Page) -> bool:
    """Chọn radio Mã khôi phục rồi bấm Tiếp. Quét cả iframe."""
    clicked = False
    for frame in _frames(page):
        try:
            picked = frame.evaluate(
                """() => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const nodes = [...document.querySelectorAll('label, div, span, p')];
                  const hit = nodes.find((n) => norm(n.innerText) === 'Mã khôi phục' || norm(n.innerText) === 'Recovery code');
                  if (!hit) return false;
                  const row = hit.closest('label, [role="radio"], li, div') || hit;
                  const radio = row.querySelector('input[type="radio"]') || hit.parentElement?.querySelector('input[type="radio"]');
                  if (radio) {
                    radio.click();
                    radio.checked = true;
                    radio.dispatchEvent(new Event('input', { bubbles: true }));
                    radio.dispatchEvent(new Event('change', { bubbles: true }));
                  } else {
                    hit.click();
                  }
                  return true;
                }"""
            )
            if not picked:
                radio = frame.get_by_role("radio", name=RECOVERY_OPTION)
                if radio.count() == 0:
                    continue
                radio.first.click(force=True)
            clicked = True
            page.wait_for_timeout(500)
            button = frame.get_by_role("button", name=BTN_NEXT)
            if button.count() == 0:
                button = frame.get_by_text(BTN_NEXT)
            if button.count() > 0:
                button.first.click(force=True)
            break
        except Exception:
            continue
    if not clicked:
        return False
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    return True


def login_muse(
    email: str,
    *,
    headless: bool = False,
    close_after: bool = False,
    save_cookies: bool = True,
    proxy: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> LoginProgress:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return LoginProgress(step="error", error=f"Chưa cài Playwright: {exc}")

    result = LoginProgress(step="starting")
    _emit(on_progress, step="starting", detail="Mở trình duyệt…")

    known_ids = meta_message_ids(email)

    with sync_playwright() as p:
        launch_kwargs: dict = {"headless": headless}
        proxy_cfg = parse_proxy(proxy)
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
            _emit(on_progress, step="proxy", detail=f"Dùng proxy {proxy_cfg['server']}")
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            locale="vi-VN",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            _emit(on_progress, step="open_muse", detail="Tải muse.ai…")
            page.goto(MUSE_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(1500)

            _emit(on_progress, step="email", detail=f"Nhập email {email}")
            email_input = page.get_by_placeholder(EMAIL_PLACEHOLDER)
            email_input.wait_for(state="visible", timeout=30000)
            email_input.fill(email)
            page.get_by_role("button", name=BTN_CONTINUE).click(no_wait_after=True)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=45000)
            except Exception:
                pass

            _emit(on_progress, step="wait_otp_mail", detail="Chờ mã hoặc màn hình xác nhận…")
            otp_input = page.locator('input[autocomplete="one-time-code"]')
            for _ in range(4):
                try:
                    otp_input.or_(page.get_by_text(CONFIRM_SCREEN)).first.wait_for(state="visible", timeout=20000)
                except Exception:
                    pass
                if not _confirm_screen_visible(page):
                    break
                _emit(on_progress, step="recovery", detail="Thấy màn xác nhận Meta — chọn Mã khôi phục")
                _select_recovery_code(page)
                page.wait_for_timeout(2000)
            if _confirm_screen_visible(page):
                raise RuntimeError("Kẹt ở màn Xác nhận Tài khoản Meta — chưa chọn được Mã khôi phục, cookie lúc này tool khác sẽ báo die.")
            try:
                otp_input.wait_for(state="visible", timeout=45000)
            except Exception:
                page.get_by_text(CODE_SCREEN).wait_for(timeout=15000)
                otp_input = page.locator('input[autocomplete="one-time-code"]')

            _emit(on_progress, step="fetch_otp", detail="Lấy mã từ Lay2FA…")
            otp_result = wait_for_new_muse_code(
                email,
                exclude_ids=known_ids,
                timeout=120,
                interval=3,
            )
            if not otp_result.code:
                raise RuntimeError(otp_result.error or "Không lấy được mã OTP")

            _emit(
                on_progress,
                step="enter_otp",
                detail=f"Mã {otp_result.code}",
                code=otp_result.code,
            )
            _submit_otp(page, otp_input, otp_result.code)

            _emit(
                on_progress,
                step="submit",
                detail="OTP xong — trang có thể đứng im vài giây (bình thường)…",
            )
            _wait_logged_in(page, timeout_sec=45)
            page.wait_for_timeout(5000)

            _emit(
                on_progress,
                step="refresh_status",
                detail="F5 + chờ muse.ai/api/consent/status…",
            )
            status_header = _refresh_and_get_status_cookie(page)
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
                code=otp_result.code,
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
            message = str(exc)
            status = "die" if DIE_TEXT.search(message) else "lỗi"
            try:
                update_account_meta(email, login_error=message, account_status=status)
            except Exception:
                pass
            err = LoginProgress(step="error", error=message, final_url=page.url)
            _emit(on_progress, **err.__dict__)
            if headless or close_after:
                browser.close()
            elif not headless:
                page.wait_for_timeout(30_000)
            return err
