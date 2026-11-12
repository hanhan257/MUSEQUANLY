"""Doc ma OTP Muse tu 10 Minute Mail qua endpoint AJAX cu (khong key).

Cung giao dien nhu cac provider mail temp (create_han_mailbox,
create_mailbox, login_mailbox, message_ids, wait_for_code, latest_code)
nen doi provider chi la doi module (xem mail_provider.py).

Dac diem da kiem chung live tu VM ngay 2026-11-12:
- 10 Minute Mail KHONG co API cong khai co tai lieu. Chi con 2 endpoint
  AJAX cu cua 10minutemail.net (khong tai lieu, co the chet bat cu luc
  nao): GET /address.api.php tra dia chi moi; GET /mailbox.api.php
  ?address=<email> tra hop thu dang JSON. Endpoint doc hop thu can
  CUNG session (cookie TENMM_G1_SESSION) voi luc tao dia chi — goi
  khong cookie tra 404.
- Dia chi chi song 10 PHUT (mail_get_duetime). Dang nhap lai sau do
  vo nghia: login_mailbox chi tra token khi session con trong tien
  trinh va chua het han, khong thi bao loi ro.
- Session (requests.Session + key + han) duoc giu trong tien trinh theo
  email, giong _SESSIONS cua Guerrilla Mail — tool chay lau nen session
  song trong suot lan tao TK.
"""
from __future__ import annotations

import re
import threading
import time

import requests

BASE = "https://10minutemail.net"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()


class TenMinuteMailError(RuntimeError):
    pass


def _session_for(email: str, proxies=None) -> dict | None:
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get((email or "").strip().lower())
        if not entry:
            return None
        if entry.get("due") and time.time() > float(entry["due"]):
            return None
        return entry


def _create(proxies=None) -> tuple[str, str, str]:
    session = requests.Session()
    session.headers.update(UA)
    if proxies:
        session.proxies.update(proxies)
    try:
        resp = session.get(BASE + "/address.api.php", timeout=30)
    except requests.RequestException as exc:
        raise TenMinuteMailError(f"Khong noi duoc 10minutemail.net ({exc})")
    try:
        data = resp.json()
    except Exception:
        raise TenMinuteMailError(f"10 Minute Mail tra ve khong phai JSON (HTTP {resp.status_code})")
    email = str((data or {}).get("mail_get_mail") or "").strip()
    if not email or "@" not in email:
        raise TenMinuteMailError("10 Minute Mail khong tra ve dia chi email")
    key = str((data or {}).get("mail_get_key") or "")
    try:
        due = float((data or {}).get("mail_get_duetime") or 0)
    except Exception:
        due = 0.0
    with _SESSIONS_LOCK:
        _SESSIONS[email.lower()] = {"session": session, "key": key, "due": due}
    return email, due


def _mailbox(email: str, proxies=None) -> list[dict]:
    entry = _session_for(email, proxies=proxies)
    if not entry:
        raise TenMinuteMailError(
            "Phien 10 Minute Mail cua " + (email or "") + " da het (dia chi chi song 10 phut) — tao hop thu moi."
        )
    session = entry["session"]
    try:
        resp = session.get(BASE + "/mailbox.api.php", params={"address": email}, timeout=30)
    except requests.RequestException as exc:
        raise TenMinuteMailError(f"Khong doc duoc hop thu 10 Minute Mail ({exc})")
    if resp.status_code != 200:
        raise TenMinuteMailError(f"10 Minute Mail tra HTTP {resp.status_code} khi doc hop thu")
    try:
        data = resp.json()
    except Exception:
        raise TenMinuteMailError("10 Minute Mail tra ve hop thu khong phai JSON")
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        items = data.get("messages") or data.get("mails") or data.get("items") or []
        if isinstance(items, list):
            return [m for m in items if isinstance(m, dict)]
    return []


def _item_text(item: dict) -> str:
    parts = []
    for key in ("body", "text", "textBody", "snippet", "preview", "subject", "from", "sender"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def _code_from_messages(messages: list[dict], exclude_ids=None) -> str:
    seen = {str(i) for i in (exclude_ids or ())}
    muse_first: list[dict] = []
    others: list[dict] = []
    for item in messages:
        mid = str(item.get("id") or item.get("mail_id") or item.get("messageId") or "")
        if mid and mid in seen:
            continue
        hay = f"{item.get('from','')} {item.get('sender','')} {item.get('subject','')}".lower()
        (muse_first if ("muse" in hay or "meta" in hay) else others).append(item)
    for item in muse_first + others:
        match = _CODE_RE.search(_item_text(item))
        if match:
            return match.group(1)
    return ""


def _poll_once(email: str, proxies=None, exclude_ids=None) -> str:
    return _code_from_messages(_mailbox(email, proxies=proxies), exclude_ids=exclude_ids)


def wait_for_code(
    token: str,
    *,
    timeout: float = 120,
    interval: float = 3,
    exclude_ids: set[str] | None = None,
    proxies=None,
) -> str:
    """Cho thu moi va lay ma 6 so. token la dia chi email (session giu trong
    tien trinh)."""
    email = (token or "").strip()
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            code = _poll_once(email, proxies=proxies, exclude_ids=exclude_ids)
        except TenMinuteMailError as exc:
            last_error = str(exc)
            if "het" in last_error and "10 phut" in last_error:
                raise
            time.sleep(interval)
            continue
        if code:
            return code
        time.sleep(interval)
    raise TenMinuteMailError(
        "Het gio chua thay ma moi trong hop 10 Minute Mail" + (f" ({last_error})" if last_error else "")
    )


def message_ids(token: str, proxies=None) -> set[str]:
    """ID cua cac thu hien co — de lan dang nhap sau biet thu nao la cu."""
    email = (token or "").strip()
    return {
        str(m.get("id") or m.get("mail_id") or m.get("messageId") or "")
        for m in _mailbox(email, proxies=proxies)
    } - {""}


def latest_code(address: str, password: str = "", proxies=None) -> str:
    """Ma moi nhat dang nam trong hop thu. Tra '' neu khong co ma nao
    (hoac phien da het)."""
    try:
        return _poll_once((address or "").strip(), proxies=proxies)
    except TenMinuteMailError:
        return ""


def login_mailbox(address: str, password: str = "", proxies=None) -> str:
    """Chi mo lai duoc khi session tao dia chi con trong tien trinh va chua
    het 10 phut — khong thi bao loi ro (dia chi 10 Minute Mail khong dang
    nhap lai duoc)."""
    address = (address or "").strip()
    entry = _session_for(address, proxies=proxies)
    if not entry:
        raise TenMinuteMailError(
            "Dia chi 10 Minute Mail chi song 10 phut — khong dang nhap lai duoc, tao hop thu moi."
        )
    return address


def create_han_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    # 10 Minute Mail tu chon dia chi ngau nhien, khong dat ten han + 9 so.
    email, _due = _create(proxies=proxies)
    return email, password, email


def create_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    email, _due = _create(proxies=proxies)
    return email, password, email
