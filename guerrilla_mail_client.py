"""Tao hop thu tam tren Guerrilla Mail (https://www.guerrillamail.com/) va doc ma.

API cong khai: GET https://api.guerrillamail.com/ajax.php (khong can dang
ky/key). Kiem chung song 2026-11-12:
- Tao hop thu: GET ?f=get_email_address&lang=en (server dat cookie
  PHPSESSID cho session) roi GET ?f=set_email_user&email_user=hanNNNNNNNNN
  de dat ten han + 9 so — tra ve dia chi day du (vd
  han123456789@guerrillamailblock.com), dung ngay khong can dang nhap.
- Doc inbox: GET ?f=check_email&seq=0 (cung session cookie) -> JSON co
  list cac thu (mail_id, mail_from, mail_subject, mail_excerpt...).
- Doc noi dung: GET ?f=fetch_email&email_id=<id> -> JSON co mail_body
  (HTML), regex ma 6 so trong text (giong cac client khac).

Session theo cookie: moi hop thu giu 1 requests.Session trong process
(_SESSIONS theo email). Token tra ve chinh la dia chi email (giong
tempmail.plus/generator.email) — cac ham message_ids / wait_for_code tim
session theo email, khong co thi mo lai bang login_mailbox
(get_email_address + set_email_user cung local part).

GIOI HAN: Guerrilla Mail xoa mail sau 1 gio ("All Emails are deleted
after 1 hour") — hop thu tao xong phai dung ngay trong luc tao TK; dang
nhap lai sau 1h se khong con thu de doc ma. Khong co link inbox cong khai
nen _code_link_for tra "" (lay ma bang nut "Ma mail" trong tool).

Tat ca request deu di qua proxy khi co proxies (giong v115) vi may user
co the loi DNS truc tiep.
"""

from __future__ import annotations

import html as _html
import random
import re
import secrets
import string
import threading
import time
from urllib.parse import quote

import requests

BASE = "https://api.guerrillamail.com/ajax.php"
# Domain Guerrilla Mail da biet (de suy luan provider tu email cu). Danh
# sach cac domain nay xuat hien tren trang chu/API; domain tra ve khi tao
# mail co the la bat ky domain nao trong so nay.
DOMAINS = (
    "guerrillamail.com",
    "guerrillamail.net",
    "guerrillamail.org",
    "guerrillamailblock.com",
    "sharklasers.com",
    "pokemail.net",
    "spam4.me",
    "grr.la",
)
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_pace_lock = threading.Lock()
_next_slot = 0.0
_sessions_lock = threading.Lock()
_SESSIONS: dict[str, requests.Session] = {}
_INITIAL_IDS: dict[str, set[str]] = {}


def _slot() -> None:
    """Xep hang cac request, cach nhau mot nhip de khoi bi chan."""
    global _next_slot
    with _pace_lock:
        now = time.monotonic()
        delay = _next_slot - now
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        _next_slot = now + 0.5


def _hold(seconds: float) -> None:
    global _next_slot
    with _pace_lock:
        _next_slot = max(_next_slot, time.monotonic() + seconds)


def _describe_request_error(exc: BaseException) -> str:
    """Mo ta ngan gon loi mang, khong kem URL/email/mat khau proxy.
    Uu tien loi proxy (neu co) roi moi den nguyen nhan sau cung."""
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and len(chain) < 6:
        chain.append(cur)
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    if any("Proxy" in type(item).__name__ for item in chain):
        return "không kết nối được proxy"
    name = type(chain[-1]).__name__ if chain else type(exc).__name__
    if "NameResolution" in name or "gaierror" in name:
        return "lỗi DNS (không phân giải được tên miền)"
    if "ConnectTimeout" in name or "ReadTimeout" in name or "Timeout" in name:
        return "hết thời gian chờ"
    if "SSLError" in name or "SSL" in name:
        return "lỗi SSL"
    if "Connection" in name:
        return "không kết nối được máy chủ"
    return name


class GuerrillaMailError(RuntimeError):
    pass


def _call(session: requests.Session, params: dict, proxies=None) -> dict:
    """Goi API Guerrilla Mail bang session (giu cookie PHPSESSID).
    proxies la dict requests: doc mail di qua proxy de proxy phan giai DNS
    thay may local. Proxy hong/chet thi thu truc tiep 1 lan; ca hai deu
    loi thi bao loi ngan gon (khong kem URL/email)."""
    via = "proxy" if proxies else "trực tiếp"
    tried: list[str] = []
    last_status = 429
    for _ in range(8):
        _slot()
        try:
            response = session.get(
                BASE,
                params={**params, "lang": "en"},
                proxies=proxies,
                timeout=30,
            )
        except requests.exceptions.RequestException as exc:
            tried.append(via)
            if proxies:
                proxies = None
                via = "trực tiếp"
                continue
            if len(tried) > 1:
                where = "proxy và trực tiếp đều lỗi"
            else:
                where = f"{tried[0]} lỗi"
            raise GuerrillaMailError(
                f"Không gọi được Guerrilla Mail ({where}: {_describe_request_error(exc)})"
            ) from exc
        if response.status_code in (429, 503) or response.status_code >= 500:
            last_status = response.status_code
            _hold(6)
            time.sleep(6)
            continue
        if response.status_code != 200:
            raise GuerrillaMailError(
                f"Guerrilla Mail lỗi {response.status_code} khi gọi API"
            )
        try:
            data = response.json()
        except Exception as exc:
            raise GuerrillaMailError(
                "Guerrilla Mail trả về dữ liệu không phải JSON"
            ) from exc
        if not isinstance(data, dict):
            raise GuerrillaMailError(
                "Guerrilla Mail trả về dữ liệu không đúng định dạng"
            )
        return data
    raise GuerrillaMailError(f"Guerrilla Mail bận ({last_status})")


def _split_email(email: str) -> tuple[str, str]:
    local, _, domain = (email or "").strip().partition("@")
    return local.strip().lower(), domain.strip().lower()


def _store_session(email: str, session: requests.Session) -> None:
    with _sessions_lock:
        _SESSIONS[(email or "").strip().lower()] = session


def _get_session(email: str) -> requests.Session | None:
    with _sessions_lock:
        return _SESSIONS.get((email or "").strip().lower())


def _open_session(local: str, proxies=None) -> tuple[requests.Session, str]:
    """Session moi + dat ten local: tra ve (session, dia chi day du)."""
    session = requests.Session()
    session.headers.update({"User-Agent": _UA})
    _call(session, {"f": "get_email_address"}, proxies=proxies)
    data = _call(
        session, {"f": "set_email_user", "email_user": local}, proxies=proxies
    )
    if (data.get("alias_error") or "").strip():
        raise GuerrillaMailError(
            f"Guerrilla Mail không đặt được tên {local} ({data.get('alias_error')})"
        )
    address = (data.get("email_addr") or "").strip().lower()
    if not address or "@" not in address:
        raise GuerrillaMailError("Guerrilla Mail không trả về địa chỉ email")
    got_local, _ = _split_email(address)
    if got_local != local.lower():
        raise GuerrillaMailError(
            f"Guerrilla Mail trả về địa chỉ khác ({address})"
        )
    return session, address


def _check_email(session: requests.Session, proxies=None) -> list[dict]:
    """Danh sach thu trong inbox, moi nhat truoc."""
    data = _call(session, {"f": "check_email", "seq": 0}, proxies=proxies)
    items = data.get("list") or []
    if not isinstance(items, list):
        raise GuerrillaMailError("Guerrilla Mail trả về danh sách thư sai định dạng")
    return [item for item in items if isinstance(item, dict)]


def _session_for(email: str, proxies=None) -> requests.Session:
    """Session cua hop thu: co san thi dung, khong co thi mo lai."""
    session = _get_session(email)
    if session is not None:
        return session
    login_mailbox(email, proxies=proxies)
    session = _get_session(email)
    if session is None:
        raise GuerrillaMailError(f"Không mở lại được hộp thư {email}")
    return session


def _text_of(html_text: str) -> str:
    """Bo tag HTML, giai entity, gom khoang trang — de regex ma 6 so."""
    text = _TAG.sub(" ", html_text or "")
    text = _html.unescape(text)
    return _WS.sub(" ", text).strip()


def _message_body(session: requests.Session, message_id: str, proxies=None) -> str:
    """Noi dung thu (text) theo mail_id."""
    data = _call(
        session, {"f": "fetch_email", "email_id": str(message_id)}, proxies=proxies
    )
    body = data.get("mail_body") or ""
    if not body:
        raise GuerrillaMailError("Guerrilla Mail trả về thư rỗng")
    return _text_of(body)


def _code_from_text(text: str) -> str:
    match = _CODE.search(text or "")
    return match.group(1) if match else ""


def login_mailbox(address: str, password: str = "", proxies=None) -> str:
    """Mo hop thu da co: mo session moi va dat lai cung local part.
    Guerrilla Mail khong can mat khau; token la dia chi. Hop thu chi
    song 1h nen mo lai sau 1h se khong con thu cu."""
    local, _ = _split_email(address)
    if not local:
        raise GuerrillaMailError("Địa chỉ email không đúng định dạng")
    session, opened = _open_session(local, proxies=proxies)
    if opened != address.strip().lower():
        raise GuerrillaMailError(f"Không mở lại được hộp thư {address}")
    _store_session(address, session)
    return address


def confirm_mailbox_login(address: str, password: str = "", proxies=None) -> str:
    """Mo lai hop thu va doc dung dia chi. Token tao luc nay khong tinh."""
    return login_mailbox(address, password, proxies=proxies)


def message_ids(token: str, proxies=None) -> set[str]:
    session = _session_for(token, proxies=proxies)
    return {str(item.get("mail_id")) for item in _check_email(session, proxies=proxies) if item.get("mail_id")}


def _open_mailbox(address: str, password: str, proxies=None) -> tuple[str, str, str]:
    """Dia chi moi la dung duoc ngay. Kiem tra inbox mo duoc va ghi lai id
    cac thu co san tu dau (Guerrilla Mail gui san 1 thu chao mung khi tao
    hop thu) de wait_for_code khong nhan ma cu lam ma moi."""
    session = _get_session(address)
    if session is None:
        raise GuerrillaMailError(f"Chưa có phiên hộp thư cho {address}")
    initial = {
        str(item.get("mail_id"))
        for item in _check_email(session, proxies=proxies)
        if item.get("mail_id")
    }
    with _sessions_lock:
        _INITIAL_IDS[address.lower()] = set(initial)
    return address, password, address


def _create_mailbox(local: str, password: str, proxies=None) -> tuple[str, str, str]:
    session, address = _open_session(local, proxies=proxies)
    _store_session(address, session)
    try:
        return _open_mailbox(address, password, proxies=proxies)
    except GuerrillaMailError:
        with _sessions_lock:
            _SESSIONS.pop(address.lower(), None)
        raise


def create_han_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    """Mail han + 9 so @<domain Guerrilla Mail>, co mat khau (giu lai cho
    tuong thich, Guerrilla Mail khong dung mat khau). Domain do server
    chon theo session, khong truyen domain khi tao. Trung thi doi so.
    Tra ve (email, password, token) voi token la chinh dia chi email."""
    last_error = "Không tạo được mail"
    for _ in range(6):
        local = f"han{secrets.randbelow(1_000_000_000):09d}"
        try:
            return _create_mailbox(local, password, proxies=proxies)
        except GuerrillaMailError as exc:
            last_error = str(exc)
            if "bị trùng" in last_error or "không đặt được tên" in last_error:
                continue
            raise
    raise GuerrillaMailError(last_error)


def create_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    """Mail ngau nhien @<domain Guerrilla Mail>, co mat khau de giu tuong
    thich ham cu. Giong create_han_mailbox: toi da 6 lan."""
    alphabet = string.ascii_lowercase + string.digits
    last_error = "Không tạo được mail"
    for _ in range(6):
        local = "".join(secrets.choice(alphabet) for _ in range(12))
        try:
            return _create_mailbox(local, password, proxies=proxies)
        except GuerrillaMailError as exc:
            last_error = str(exc)
            if "bị trùng" in last_error or "không đặt được tên" in last_error:
                continue
            raise
    raise GuerrillaMailError(last_error)


def wait_for_code(
    token: str,
    *,
    timeout: float = 120,
    interval: float = 3,
    exclude_ids: set[str] | None = None,
    proxies=None,
) -> str:
    """Cho thu moi va lay ma 6 so. token la dia chi email. exclude_ids la
    thu cu, khong lay lai ma lan truoc."""
    deadline = time.time() + timeout
    with _sessions_lock:
        seeded = set(_INITIAL_IDS.get((token or "").strip().lower(), set()))
    seen: set[str] = set(exclude_ids or ()) | seeded
    while time.time() < deadline:
        session = _session_for(token, proxies=proxies)
        try:
            items = _check_email(session, proxies=proxies)
        except GuerrillaMailError:
            time.sleep(interval)
            continue
        for item in items:
            message_id = str(item.get("mail_id") or "")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            try:
                code = _code_from_text(_message_body(session, message_id, proxies=proxies))
            except GuerrillaMailError:
                continue
            if code:
                return code
        time.sleep(interval)
    raise GuerrillaMailError("Hết giờ chưa thấy mã mới trong hộp Guerrilla Mail")


def latest_code(address: str, password: str = "", proxies=None) -> str:
    """Ma moi nhat dang nam trong hop thu, dung lai cho lan dang nhap sau."""
    login_mailbox(address, password, proxies=proxies)
    session = _session_for(address, proxies=proxies)
    for item in _check_email(session, proxies=proxies):
        message_id = str(item.get("mail_id") or "")
        if not message_id:
            continue
        code = _code_from_text(_message_body(session, message_id, proxies=proxies))
        if code:
            return code
    raise GuerrillaMailError("Hộp Guerrilla Mail chưa có mã xác minh")
