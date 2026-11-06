"""Tạo hộp thư tạm trên https://tempmail.plus/ và đọc mã trong thư.

Khác mail.tm ở chỗ tempmail.plus không có khái niệm tạo tài khoản hay
đăng nhập: bất kỳ địa chỉ nào ở tên miền hỗ trợ cũng nhận được thư, chỉ
cần gọi API list là hộp thư được mở. Vì vậy "token" ở đây chính là địa
chỉ email, và mật khẩu chỉ giữ lại cho tương thích chữ ký hàm cũ, không
dùng để đăng nhập. Ai biết địa chỉ thì đọc được thư của hộp đó, nên địa
chỉ phải ngẫu nhiên, không đoán được.
"""

from __future__ import annotations

import re
import secrets
import string
import threading
import time

import requests

API = "https://tempmail.plus"
DEFAULT_DOMAIN = "mailto.plus"
# Tên miền tempmail.plus từng công bố (không có endpoint liệt kê domain).
DOMAINS = (
    "mailto.plus",
    "fexpost.com",
    "fexbox.org",
    "fexbox.ru",
    "mailbox.in.ua",
    "rover.info",
    "inpwa.com",
    "intopwa.com",
    "tofeat.com",
    "chitthi.in",
    "fextemp.com",
    "any.pink",
    "merepost.com",
)
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_pace_lock = threading.Lock()
_next_slot = 0.0


def _slot() -> None:
    """Xếp hàng các request, cách nhau một nhịp để khỏi bị chặn."""
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
    raise TempMailPlusError(f"tempmail.plus bận ({last_status})")


class TempMailPlusError(RuntimeError):
    pass


def _json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except Exception as exc:
        raise TempMailPlusError(
            f"tempmail.plus trả về không phải JSON ({response.status_code})"
        ) from exc
    if response.status_code >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error") or data.get("message") or "")[:180]
        raise TempMailPlusError(
            f"tempmail.plus lỗi {response.status_code}: {detail or response.text[:180]}"
        )
    if isinstance(data, dict) and data.get("result") is False:
        detail = str(data.get("error") or data.get("message") or "")[:180]
        raise TempMailPlusError(f"tempmail.plus lỗi: {detail or data}")
    return data if isinstance(data, dict) else {}


def _list(email: str, limit: int = 20) -> dict:
    response = _send(
        "GET",
        f"{API}/api/mails",
        params={"email": email, "limit": limit, "epin": ""},
    )
    return _json(response)


def _detail(email: str, mail_id: int | str) -> dict:
    response = _send(
        "GET",
        f"{API}/api/mails/{mail_id}",
        params={"email": email, "epin": ""},
    )
    return _json(response)


def login_mailbox(address: str, password: str = "") -> str:
    """Mở hộp thư đã có. tempmail.plus không cần mật khẩu, token là địa chỉ."""
    _list(address)
    return address


def confirm_mailbox_login(address: str, password: str = "") -> str:
    """Mở lại hộp thư và đọc đúng địa chỉ. Token tạo lúc nãy không tính."""
    data = _list(address)
    if not data.get("result", True):
        raise TempMailPlusError("Đã tạo mail nhưng mở lại hộp thư không được")
    return address


def message_ids(token: str) -> set[str]:
    listing = _list(token)
    return {
        str(item.get("mail_id") or "")
        for item in (listing.get("mail_list") or [])
        if item.get("mail_id") is not None
    }


def _code_from_message(full: dict) -> str:
    blob = " ".join(str(full.get(key) or "") for key in ("subject", "text", "html"))
    match = _CODE.search(blob)
    return match.group(1) if match else ""


def _open_mailbox(address: str, password: str) -> tuple[str, str, str]:
    """Địa chỉ mới là dùng được ngay. Gọi list một lần để chắc hộp thư mở
    được và chưa có thư lạ (địa chỉ ngẫu nhiên bị trùng thì đổi số)."""
    data = _list(address)
    if (data.get("mail_list") or []) or int(data.get("count") or 0) > 0:
        raise TempMailPlusError(f"{address} bị trùng")
    return address, password, confirm_mailbox_login(address, password)


def create_han_mailbox(password: str, domain: str = DEFAULT_DOMAIN) -> tuple[str, str, str]:
    """Mail han + 9 số @mailto.plus, có mật khẩu (giữ lại cho tương thích,
    tempmail.plus không dùng mật khẩu). Trùng thì đổi số. Trả về
    (email, password, token) với token là chính địa chỉ email."""
    last_error = "Không tạo được mail"
    for _ in range(12):
        local = f"han{secrets.randbelow(1_000_000_000):09d}"
        address = f"{local}@{domain}"
        try:
            return _open_mailbox(address, password)
        except TempMailPlusError as exc:
            text = str(exc)
            if "bị trùng" in text or "bận" in text or "không phải JSON" in text:
                last_error = text
                if "bận" in text or "không phải JSON" in text:
                    time.sleep(6)
                continue
            raise
    raise TempMailPlusError(last_error)


def create_mailbox(password: str, domain: str = DEFAULT_DOMAIN) -> tuple[str, str, str]:
    """Mail ngẫu nhiên @mailto.plus, có mật khẩu để giữ tương thích hàm cũ."""
    alphabet = string.ascii_lowercase + string.digits
    last_error = "Không tạo được mail"
    for _ in range(12):
        local = "".join(secrets.choice(alphabet) for _ in range(12))
        address = f"{local}@{domain}"
        try:
            return _open_mailbox(address, password)
        except TempMailPlusError as exc:
            text = str(exc)
            if "bị trùng" in text or "bận" in text or "không phải JSON" in text:
                last_error = text
                if "bận" in text or "không phải JSON" in text:
                    time.sleep(6)
                continue
            raise
    raise TempMailPlusError(last_error)


def wait_for_code(
    token: str,
    *,
    timeout: float = 120,
    interval: float = 3,
    exclude_ids: set[str] | None = None,
) -> str:
    """Chờ thư mới và lấy mã 6 số. token là địa chỉ email. exclude_ids là
    thư cũ, không lấy lại mã lần trước."""
    deadline = time.time() + timeout
    seen: set[str] = set(exclude_ids or ())
    while time.time() < deadline:
        try:
            listing = _list(token)
        except TempMailPlusError as exc:
            if "bận" in str(exc):
                time.sleep(6)
                continue
            raise
        for item in listing.get("mail_list") or []:
            message_id = str(item.get("mail_id") or "")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            full = _detail(token, message_id)
            code = _code_from_message(full)
            if code:
                return code
        time.sleep(interval)
    raise TempMailPlusError("Hết giờ chưa thấy mã mới trong hộp tempmail.plus")


def latest_code(address: str, password: str = "") -> str:
    """Mã mới nhất đang nằm trong hộp thư, dùng lại cho lần đăng nhập sau."""
    login_mailbox(address, password)
    listing = _list(address)
    for item in listing.get("mail_list") or []:
        message_id = str(item.get("mail_id") or "")
        if not message_id:
            continue
        code = _code_from_message(_detail(address, message_id))
        if code:
            return code
    raise TempMailPlusError("Hộp tempmail.plus chưa có mã xác minh")
