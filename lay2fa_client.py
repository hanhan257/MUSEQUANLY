"""Lay2FA temp-mail client for Muse / Meta login codes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
LAY2FA_BASE = "https://lay2fa.xyz"
META_SENDERS = ("meta.com", "facebook.com", "fb.com", "messenger.com")


@dataclass
class MailMessage:
    id: str
    sender: str
    subject: str
    date: str
    body: str

    @property
    def is_meta_login(self) -> bool:
        s = (self.sender + self.subject + self.body).lower()
        if any(dom in self.sender.lower() for dom in META_SENDERS):
            return True
        return "meta" in s or "mã đăng nhập" in s or "login code" in s


@dataclass
class OtpResult:
    code: str | None
    message: MailMessage | None
    error: str | None = None


def normalize_lay2fa_url(raw: str, email: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return f"{LAY2FA_BASE}/{email}"
    if not raw.startswith("http"):
        raw = "https://" + raw.lstrip("/")
    return raw


def email_from_lay2fa_url(url: str) -> str | None:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if "@" in path and "." in path.split("@")[-1]:
        return path
    return None


def parse_account_line(line: str) -> tuple[str, str, str, str] | None:
    """Hỗ trợ cả định dạng cũ email|lay2fa|mã 2FA và mới email|mật khẩu|mã 2FA/ghi chú,
    và định dạng tool xuất: email|mật khẩu/link lay2fa|mã 2FA|mật khẩu hộp thư.
    Trả về (email, mật khẩu/link lay2fa, mã 2FA, mật khẩu hộp thư)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [part.strip() for part in line.split("|")]
    if not parts or not parts[0]:
        return None

    email = parts[0]
    second = parts[1] if len(parts) > 1 else ""
    secret = parts[2] if len(parts) > 2 else ""
    mail_password = parts[3] if len(parts) > 3 else ""

    if second and not second.lower().startswith(("http://", "https://")) and "://" not in second:
        url = second
    else:
        url = normalize_lay2fa_url(second, email)

    if "@" not in email:
        inferred = email_from_lay2fa_url(url)
        if inferred:
            email = inferred
        else:
            return None
    return email, url, secret, mail_password


def fetch_2fa_live_code(secret: str) -> str:
    """Cùng việc dán mã lên https://2fa.live/ rồi bấm Submit: GET /tok/<secret>."""
    compact = re.sub(r"\s+", "", (secret or "").strip()).upper()
    if not compact:
        raise RuntimeError("Chưa có mã 2FA trên dòng tài khoản.")
    resp = _session().get(f"https://2fa.live/tok/{quote(compact, safe='')}", timeout=25)
    resp.raise_for_status()
    token = str((resp.json() or {}).get("token") or "").strip()
    if not re.fullmatch(r"\d{6}", token):
        raise RuntimeError("2fa.live không trả mã 6 số.")
    return token


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def fetch_inbox(email: str, limit: int = 15) -> list[MailMessage]:
    local, domain = email.rsplit("@", 1)
    url = (
        f"{LAY2FA_BASE}/api/tempmail/inbox/"
        f"{quote(domain, safe='')}/{quote(local, safe='')}?limit={limit}"
    )
    resp = _session().get(url, timeout=25)
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    out: list[MailMessage] = []
    for item in data.get("emails") or []:
        out.append(
            MailMessage(
                id=str(item.get("id", "")),
                sender=str(item.get("sender", "")),
                subject=str(item.get("subject", "")),
                date=str(item.get("date", "")),
                body=str(item.get("body", "") or item.get("html_body", "")),
            )
        )
    return out


def extract_login_code(msg: MailMessage) -> str | None:
    text = f"{msg.subject}\n{msg.body}"
    patterns = [
        r"[Mm]ã\s*đăng nhập[^\d]*(\d{6})",
        r"[Dd]ùng\s*mã\s*(\d{6})",
        r"login\s*code[^\d]*(\d{6})",
        r"\b(\d{6})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1)
    return None


def get_latest_muse_code(
    email: str,
    *,
    after_subject: str | None = None,
    prefer_meta: bool = True,
) -> OtpResult:
    try:
        messages = fetch_inbox(email)
    except requests.RequestException as exc:
        return OtpResult(code=None, message=None, error=str(exc))

    if not messages:
        return OtpResult(code=None, message=None, error="Chưa có thư trong hộp thư.")

    ordered = messages
    if prefer_meta:
        meta = [m for m in messages if m.is_meta_login]
        if meta:
            ordered = meta + [m for m in messages if m not in meta]

    for msg in ordered:
        if after_subject and msg.subject == after_subject:
            continue
        code = extract_login_code(msg)
        if code:
            return OtpResult(code=code, message=msg, error=None)

    return OtpResult(
        code=None,
        message=messages[0],
        error="Không tìm thấy mã 6 số trong thư mới nhất.",
    )


def meta_message_ids(email: str) -> set[str]:
    try:
        messages = fetch_inbox(email)
    except requests.RequestException:
        return set()
    return {m.id for m in messages if m.is_meta_login and m.id}


def wait_for_new_muse_code(
    email: str,
    *,
    exclude_ids: set[str] | None = None,
    timeout: float = 120,
    interval: float = 3,
) -> OtpResult:
    import time

    exclude = exclude_ids or set()
    deadline = time.time() + timeout
    last_error: str | None = None
    while time.time() < deadline:
        try:
            messages = fetch_inbox(email)
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(interval)
            continue
        for msg in messages:
            if not msg.is_meta_login or msg.id in exclude:
                continue
            code = extract_login_code(msg)
            if code:
                return OtpResult(code=code, message=msg, error=None)
        last_error = "Chưa có thư Meta mới."
        time.sleep(interval)
    return OtpResult(code=None, message=None, error=last_error or "Hết thời gian chờ mã OTP.")
