"""Doc ma OTP Muse tu Tempemail.cc qua API (chi doc, khong gui mail).

Cung giao dien nhu cac provider mail temp (create_han_mailbox,
create_mailbox, login_mailbox, message_ids, wait_for_code, latest_code)
nen doi provider chi la doi module (xem mail_provider.py).

Dac diem da kiem chung live tu VM ngay 2026-11-12:
- Base https://www.tempemail.cc; API tra HTTP 200 ca khi loi, phai doc
  truong "code" trong JSON (200 = ok, 400/403 = loi) — khong duoc chi
  nhin HTTP status.
- POST /api/accounts TU CHOI User-Agent mac dinh cua requests
  ("Access denied. Invalid User-Agent") — client bat buoc gui UA trinh
  duyet. Mat khau tao TK phai co it nhat 1 chu hoa.
- Tao TK: POST /api/accounts {email, password} (domain lay live tu
  GET /api/domains) -> data {id, name, token (JWT)}. Gioi han 1 dang ky
  / IP / 30 giay — tao qua nhanh bi chan, client doi 31s thu lai 1 lan.
- Doc thu: GET /api/messages?limit=10 (limit phai 10-50) + header
  Authorization: Bearer <token>; tung thu lay them qua
  GET /api/messages/:id/raw.
- Token JWT CHI CO LUC TAO (POST lai cung email bi "email already
  exists", khong co endpoint dang nhap lai) — token luu vao file rieng
  tempemail_tokens.json (da gitignore, khong bao gio commit), login
  lai doc tu file nay. Mat khau hop thu chi dung luc tao, khong can
  nho lai.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time

import requests
from app_paths import DATA_DIR

BASE = "https://www.tempemail.cc"
DOMAINS_FALLBACK = ("icmans.com",)
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_RATE_LIMIT_WAIT = 31
_TOKEN_FILE = DATA_DIR / "tempemail_tokens.json"
_TOKEN_LOCK = threading.Lock()


class TempEmailError(RuntimeError):
    pass


def _call(method: str, path: str, token: str | None = None, json_body: dict | None = None, proxies=None, timeout: int = 30):
    """Goi API tempemail.cc, tra JSON data. Loi mang / loi API deu thanh
    TempEmailError. Luu y: API tra HTTP 200 ca khi loi — phai kiem tra
    truong code trong JSON."""
    headers = dict(UA)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.request(method, BASE + path, headers=headers, json=json_body, proxies=proxies, timeout=timeout)
    except requests.RequestException as exc:
        raise TempEmailError(f"Khong noi duoc tempemail.cc ({exc})")
    try:
        body = resp.json()
    except Exception:
        raise TempEmailError(f"tempemail.cc tra ve khong phai JSON (HTTP {resp.status_code})")
    if not isinstance(body, dict):
        raise TempEmailError(f"tempemail.cc tra ve sai dinh dang (HTTP {resp.status_code})")
    code = body.get("code")
    if resp.status_code != 200 or (code is not None and code != 200):
        raise TempEmailError(f"tempemail.cc: {body.get('message') or body} (code {code})")
    return body.get("data")


def _load_tokens() -> dict:
    if not _TOKEN_FILE.exists():
        return {}
    try:
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_token(email: str, token: str) -> None:
    with _TOKEN_LOCK:
        data = _load_tokens()
        data[(email or "").strip().lower()] = token
        tmp = _TOKEN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_TOKEN_FILE)


def _load_token(email: str) -> str:
    with _TOKEN_LOCK:
        return (_load_tokens().get((email or "").strip().lower()) or "").strip()


def _domains(proxies=None) -> list[str]:
    try:
        data = _call("GET", "/api/domains", proxies=proxies)
        if isinstance(data, list) and data:
            return [str(d) for d in data if str(d).strip()]
    except TempEmailError:
        pass
    return list(DOMAINS_FALLBACK)


def _creation_password(password: str) -> str:
    """Mat khau dung de tao TK tempemail.cc: phai co it nhat 1 chu hoa.
    Dung mat khau truyen vao neu co chu hoa, khong thi chen 'A' vao dau
    (mat khau nay chi dung luc tao, ve sau doc ma bang token JWT)."""
    pw = (password or "").strip()
    if any(c.isupper() for c in pw):
        return pw
    return "A" + pw if pw else "Aa" + str(random.randint(100000, 999999))


def _create(local: str, password: str, proxies=None) -> tuple[str, str, str]:
    domain = _domains(proxies=proxies)[0]
    email = f"{local}@{domain}"
    creation_pw = _creation_password(password)
    last_error: TempEmailError | None = None
    for attempt in range(2):
        try:
            data = _call(
                "POST", "/api/accounts",
                json_body={"email": email, "password": creation_pw},
                proxies=proxies,
            )
            break
        except TempEmailError as exc:
            last_error = exc
            msg = str(exc).lower()
            if attempt == 0 and ("rate" in msg or "limit" in msg or "429" in msg or "too many" in msg):
                time.sleep(_RATE_LIMIT_WAIT)
                continue
            raise
    else:  # pragma: no cover - chi khi ca 2 lan deu loi gioi han
        raise last_error or TempEmailError("Khong tao duoc hop thu tempemail.cc")
    if not isinstance(data, dict) or not data.get("token"):
        raise TempEmailError("tempemail.cc khong tra ve token khi tao hop thu")
    token = str(data["token"])
    _save_token(email, token)
    return email, password, token


def _han_local() -> str:
    return "han" + "".join(str(random.randint(0, 9)) for _ in range(9))


def create_han_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    return _create(_han_local(), password, proxies=proxies)


def create_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    import secrets

    return _create("tmp" + secrets.token_hex(6), password, proxies=proxies)


def _list_messages(token: str, proxies=None) -> list[dict]:
    data = _call("GET", "/api/messages?limit=10", token=token, proxies=proxies)
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        items = data.get("messages") or data.get("items") or []
        if isinstance(items, list):
            return [m for m in items if isinstance(m, dict)]
    return []


def _message_text(item: dict, token: str, proxies=None) -> str:
    parts = []
    for key in ("body", "text", "textBody", "snippet", "preview", "subject", "from", "sender"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    text = "\n".join(parts)
    message_id = item.get("id") or item.get("_id") or item.get("messageId")
    if message_id:
        try:
            raw = _call("GET", f"/api/messages/{message_id}/raw", token=token, proxies=proxies)
            if isinstance(raw, str) and raw.strip():
                text += "\n" + raw
            elif isinstance(raw, dict):
                for key in ("body", "text", "raw"):
                    value = raw.get(key)
                    if isinstance(value, str) and value.strip():
                        text += "\n" + value
        except TempEmailError:
            pass
    return text


def _code_from_messages(messages: list[dict], token: str, proxies=None, exclude_ids=None) -> str:
    seen = {str(i) for i in (exclude_ids or ())}
    muse_first: list[dict] = []
    others: list[dict] = []
    for item in messages:
        mid = str(item.get("id") or item.get("_id") or item.get("messageId") or "")
        if not mid or mid in seen:
            continue
        hay = f"{item.get('from','')} {item.get('sender','')} {item.get('subject','')}".lower()
        (muse_first if ("muse" in hay or "meta" in hay) else others).append(item)
    for item in muse_first + others:
        match = _CODE_RE.search(_message_text(item, token, proxies=proxies))
        if match:
            return match.group(1)
    return ""


def wait_for_code(
    token: str,
    *,
    timeout: float = 120,
    interval: float = 3,
    exclude_ids: set[str] | None = None,
    proxies=None,
) -> str:
    """Cho thu moi va lay ma 6 so. token la JWT lay luc tao hop thu."""
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            messages = _list_messages(token, proxies=proxies)
        except TempEmailError as exc:
            last_error = str(exc)
            time.sleep(interval)
            continue
        code = _code_from_messages(messages, token, proxies=proxies, exclude_ids=exclude_ids)
        if code:
            return code
        time.sleep(interval)
    raise TempEmailError(
        "Het gio chua thay ma moi trong hop tempemail.cc" + (f" ({last_error})" if last_error else "")
    )


def message_ids(token: str, proxies=None) -> set[str]:
    """ID cua cac thu hien co — de lan dang nhap sau biet thu nao la cu."""
    return {
        str(m.get("id") or m.get("_id") or m.get("messageId") or "")
        for m in _list_messages(token, proxies=proxies)
    } - {""}


def latest_code(address: str, password: str = "", proxies=None) -> str:
    """Ma moi nhat dang nam trong hop thu (dung lai cho lan dang nhap sau).
    Tra '' neu hop thu khong co ma nao."""
    token = _load_token(address)
    if not token:
        return ""
    try:
        return _code_from_messages(_list_messages(token, proxies=proxies), token, proxies=proxies)
    except TempEmailError:
        return ""


def login_mailbox(address: str, password: str = "", proxies=None) -> str:
    """Mo lai hop thu bang token JWT da luu luc tao (tempemail.cc khong co
    endpoint dang nhap lai — token chi co luc tao)."""
    address = (address or "").strip()
    token = _load_token(address)
    if not token:
        raise TempEmailError(
            "Khong con token hop thu tempemail.cc cua "
            + address
            + " (token chi co luc tao hop thu) — tao hop thu moi."
        )
    try:
        _list_messages(token, proxies=proxies)
    except TempEmailError as exc:
        raise TempEmailError(f"Token hop thu tempemail.cc khong dung duoc nua ({exc})")
    return token
