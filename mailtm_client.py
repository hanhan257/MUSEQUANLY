"""Tạo hộp thư tạm trên https://api.mail.tm/ và đọc mã trong thư."""

from __future__ import annotations

import re
import secrets
import string
import threading
import time

import requests

API = "https://api.mail.tm"
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_pace_lock = threading.Lock()
_next_slot = 0.0
_domains_at = 0.0
_domains: list[str] = []


def _slot() -> None:
    """mail.tm chỉ chịu khoảng 8 request/giây. Mọi luồng xếp hàng, cách nhau một nhịp."""
    global _next_slot
    with _pace_lock:
        now = time.monotonic()
        delay = _next_slot - now
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        _next_slot = now + 0.45


def _hold(seconds: float) -> None:
    global _next_slot
    with _pace_lock:
        _next_slot = max(_next_slot, time.monotonic() + seconds)


def _send(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 30)
    last_status = 429
    for _ in range(8):
        _slot()
        response = requests.request(method, url, **kwargs)
        if response.status_code in (429, 503) or response.status_code >= 500:
            last_status = response.status_code
            _hold(6)
            time.sleep(6)
            continue
        return response
    raise MailTmError(f"mail.tm bận ({last_status})")


class MailTmError(RuntimeError):
    pass


def _json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except Exception as exc:
        raise MailTmError(f"mail.tm trả về không phải JSON ({response.status_code})") from exc
    if response.status_code >= 400:
        detail = data.get("detail") or data.get("message") or data.get("hydra:description") or response.text[:180]
        raise MailTmError(f"mail.tm lỗi {response.status_code}: {detail}")
    return data if isinstance(data, dict) else {}


def _token_for(address: str, password: str) -> str:
    """Đăng nhập hộp thư đã có. Token hết hạn thì gọi lại, không cần tạo mail mới."""
    last_error = "mail.tm không trả token"
    for _ in range(4):
        response = _send(
            "POST",
            f"{API}/token",
            json={"address": address, "password": password},
        )
        if response.status_code == 401:
            detail = ""
            try:
                detail = str(response.json().get("message") or response.text[:180])
            except Exception:
                detail = response.text[:180]
            raise MailTmError(detail or "mail.tm không đăng nhập được hộp thư này")
        token = _json(response).get("token") or ""
        if not token:
            raise MailTmError("mail.tm không trả token")
        return token
    raise MailTmError(last_error)


def login_mailbox(address: str, password: str) -> str:
    return _token_for(address, password)


def _auth_get(token: str, url: str) -> dict:
    response = _send("GET", url, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 401:
        raise MailTmError("mail.tm hết phiên, đăng nhập lại hộp thư")
    if response.status_code in (429, 503) or response.status_code >= 500:
        raise MailTmError(f"mail.tm bận ({response.status_code})")
    return _json(response)


def message_ids(token: str) -> set[str]:
    listing = _auth_get(token, f"{API}/messages")
    return {str(item.get("id") or "") for item in (listing.get("hydra:member") or []) if item.get("id")}


def _code_from_message(full: dict) -> str:
    blob = " ".join(str(full.get(key) or "") for key in ("subject", "intro", "text"))
    html = full.get("html") or ""
    if isinstance(html, list):
        html = " ".join(str(part) for part in html)
    match = _CODE.search(f"{blob} {html}")
    return match.group(1) if match else ""


def _active_domains() -> list[str]:
    global _domains_at, _domains
    if _domains and time.monotonic() - _domains_at < 60:
        return list(_domains)
    response = _send("GET", f"{API}/domains", headers={"Accept": "application/ld+json"})
    try:
        data = response.json()
    except Exception as exc:
        raise MailTmError(f"mail.tm trả về không phải JSON ({response.status_code})") from exc
    if response.status_code >= 400:
        raise MailTmError(f"mail.tm lỗi {response.status_code}: {str(data)[:180]}")
    members = data.get("hydra:member") if isinstance(data, dict) else data
    names = [
        item.get("domain")
        for item in (members or [])
        if isinstance(item, dict) and item.get("isActive", True) and item.get("domain")
    ]
    if not names:
        raise MailTmError("mail.tm không có tên miền đang mở")
    _domains = names
    _domains_at = time.monotonic()
    return list(names)


def confirm_mailbox_login(address: str, password: str) -> str:
    """Đăng nhập lại bằng mật khẩu đã lưu và đọc đúng hộp thư. Token tạo lúc nãy không tính."""
    token = _token_for(address, password)
    me = _auth_get(token, f"{API}/me")
    got = str(me.get("address") or "").strip().lower()
    if got != address.strip().lower():
        raise MailTmError("Đã tạo mail nhưng đăng nhập lại không vào đúng hộp thư")
    _auth_get(token, f"{API}/messages")
    return token


def _open_mailbox(address: str, password: str) -> tuple[str, str, str]:
    """Tạo hộp thư đúng form mail.tm: email @ tên miền và mật khẩu, rồi đăng nhập lại được."""
    created = _send(
        "POST",
        f"{API}/accounts",
        headers={"Accept": "application/ld+json"},
        json={"address": address, "password": password},
    )
    if created.status_code == 422:
        try:
            detail = str(created.json()).lower()
        except Exception:
            detail = created.text.lower()
        if "password" in detail and "address" not in detail and "email" not in detail:
            raise MailTmError(f"mail.tm từ chối mật khẩu: {created.text[:180]}")
        raise MailTmError(f"{address} bị trùng")
    _json(created)
    return address, password, confirm_mailbox_login(address, password)


def create_han_mailbox(password: str, domain: str = "fldubai.com") -> tuple[str, str, str]:
    """Mail han + 9 số @fldubai.com, có mật khẩu. Trùng thì đổi số. Trả về (email, password, token)."""
    names = _active_domains()
    if domain not in names:
        raise MailTmError(f"mail.tm không còn tên miền {domain}. Đang có: {', '.join(names)}")
    last_error = "Không tạo được mail"
    for _ in range(12):
        local = f"han{secrets.randbelow(1_000_000_000):09d}"
        address = f"{local}@{domain}"
        try:
            return _open_mailbox(address, password)
        except MailTmError as exc:
            text = str(exc)
            if "từ chối mật khẩu" in text:
                raise
            if "bị trùng" in text or "bận" in text or "không phải JSON" in text:
                last_error = text
                if "bận" in text or "không phải JSON" in text:
                    time.sleep(6)
                continue
            raise
    raise MailTmError(last_error)


def create_mailbox(password: str, domain: str = "fldubai.com") -> tuple[str, str, str]:
    """Mail ngẫu nhiên @fldubai.com, có mật khẩu để đăng nhập mail.tm lần sau."""
    names = _active_domains()
    if domain not in names:
        raise MailTmError(f"mail.tm không còn tên miền {domain}. Đang có: {', '.join(names)}")
    alphabet = string.ascii_lowercase + string.digits
    last_error = "Không tạo được mail"
    for _ in range(12):
        local = "".join(secrets.choice(alphabet) for _ in range(12))
        address = f"{local}@{domain}"
        try:
            return _open_mailbox(address, password)
        except MailTmError as exc:
            text = str(exc)
            if "từ chối mật khẩu" in text:
                raise
            if "bị trùng" in text or "bận" in text or "không phải JSON" in text:
                last_error = text
                if "bận" in text or "không phải JSON" in text:
                    time.sleep(6)
                continue
            raise
    raise MailTmError(last_error)


def wait_for_code(
    token: str,
    *,
    timeout: float = 120,
    interval: float = 3,
    exclude_ids: set[str] | None = None,
) -> str:
    """Chờ thư mới và lấy mã 6 số. exclude_ids là thư cũ, không lấy lại mã lần trước."""
    deadline = time.time() + timeout
    seen: set[str] = set(exclude_ids or ())
    while time.time() < deadline:
        try:
            listing = _auth_get(token, f"{API}/messages")
        except MailTmError as exc:
            if "bận" in str(exc):
                time.sleep(6)
                continue
            raise
        for item in listing.get("hydra:member") or []:
            message_id = str(item.get("id") or "")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            full = _auth_get(token, f"{API}/messages/{message_id}")
            code = _code_from_message(full)
            if code:
                return code
        time.sleep(interval)
    raise MailTmError("Hết giờ chưa thấy mã mới trong hộp mail.tm")


def latest_code(address: str, password: str) -> str:
    """Mã mới nhất đang nằm trong hộp thư, dùng lại cho lần đăng nhập sau."""
    token = login_mailbox(address, password)
    listing = _auth_get(token, f"{API}/messages")
    members = list(listing.get("hydra:member") or [])
    members.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
    for item in members:
        message_id = str(item.get("id") or "")
        if not message_id:
            continue
        code = _code_from_message(_auth_get(token, f"{API}/messages/{message_id}"))
        if code:
            return code
    raise MailTmError("Hộp mail.tm chưa có mã xác minh")
