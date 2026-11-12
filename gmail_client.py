"""Doc ma OTP Muse tu Gmail qua IMAP (chi doc, khong gui mail).

Cung giao dien nhu cac provider mail temp (create_han_mailbox,
create_mailbox, login_mailbox, message_ids, wait_for_code, latest_code)
nen doi provider chi la doi module (xem mail_provider.py).

Khac biet so voi mail temp:
- Gmail KHONG tao moi duoc — lay Gmail chua dung trong Kho Gmail
  (gmail_store.take_gmail_account), 1 Gmail = 1 tai khoan Muse.
- Token la "email|mat-khau-ung-dung": ham nao can ca email + mat khau
  deu tach tu token nay (message_ids/wait_for_code chi nhan token).
- Ham create_* tra ve (email, password, token) voi password LA MAT KHAU
  DUOC TRUYEN VAO (ACCOUNT_PASSWORD chung) — giong mail temp: mat khau
  nay vua luu vao kho tai khoan vua lam mat khau tai khoan Muse. Mat
  khau ung dung Gmail THAT chi nam trong token + file gmail_accounts.json,
  khong ghi vao kho tai khoan (xem gmail_store.py).
- Doc mail di TRUC TIEP (khong qua proxy dang ky): chi trinh duyet moi
  can di qua proxy, Muse khong nhin thay ket noi IMAP cua tool.
- Can bat "Xac minh 2 buoc" tren tai khoan Google va tao "Mat khau ung
  dung" 16 ky tu (Tai khoan Google -> Bao mat -> Mat khau ung dung).
  Chi dung stdlib (imaplib + email), khong them thu vien moi.
"""
from __future__ import annotations

import email as email_lib
import imaplib
import re
import socket
import time
from email.header import decode_header

DOMAINS = ("gmail.com",)
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_GMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@gmail\.com$", re.I)


class GmailError(RuntimeError):
    pass


def _split_token(token: str) -> tuple[str, str]:
    text = (token or "").strip()
    if "|" not in text:
        raise GmailError("Token Gmail khong dung dang email|mat-khau-ung-dung")
    email, _, app_password = text.partition("|")
    email = email.strip()
    app_password = re.sub(r"\s+", "", app_password.strip())
    if not _GMAIL_RE.match(email) or len(app_password) < 16:
        raise GmailError("Token Gmail khong dung dang email|mat-khau-ung-dung")
    return email, app_password


def _connect(email: str, app_password: str) -> imaplib.IMAP4_SSL:
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    except (OSError, socket.timeout) as exc:
        raise GmailError(f"Khong noi duoc {IMAP_HOST}:{IMAP_PORT} — kiem tra mang ({exc})")
    try:
        mail.login(email, app_password)
    except imaplib.IMAP4.error:
        try:
            mail.logout()
        except Exception:
            pass
        raise GmailError(
            "Gmail bao sai dia chi hoac mat khau ung dung — kiem tra lai mat khau "
            "ung dung 16 ky tu (Tai khoan Google -> Bao mat -> Xac minh 2 buoc -> "
            "Mat khau ung dung). Mat khau tai khoan Google thuong KHONG dung duoc o day."
        )
    return mail


def _decode_value(value) -> str:
    if value is None:
        return ""
    try:
        parts = decode_header(str(value))
    except Exception:
        return str(value)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            except Exception:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _body_text(msg) -> str:
    if msg.is_multipart():
        plain = ""
        html = ""
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if ctype == "text/plain" and not plain:
                try:
                    plain = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    plain = ""
            elif ctype == "text/html" and not html:
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    html = ""
        return plain or re.sub(r"<[^>]+>", " ", html or "")
    try:
        payload = msg.get_payload(decode=True)
        if payload is None:
            return str(msg.get_payload() or "")
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return ""


def _recent_messages(mail: imaplib.IMAP4_SSL, limit: int = 10) -> list[dict]:
    """10 thu moi nhat trong INBOX (moi nhat dung dau). Chi doc, khong
    danh dau da doc (readonly)."""
    typ, _ = mail.select("INBOX", readonly=True)
    if typ != "OK":
        raise GmailError("Khong mo duoc hop thu Gmail (INBOX)")
    typ, data = mail.search(None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()
    items: list[dict] = []
    for raw_uid in reversed(uids[-limit:]):
        uid = raw_uid.decode("ascii", errors="replace")
        try:
            typ, fetched = mail.fetch(raw_uid, "(RFC822)")
        except imaplib.IMAP4.error:
            continue
        if typ != "OK" or not fetched:
            continue
        raw = fetched[0][1] if isinstance(fetched[0], tuple) else None
        if not raw:
            continue
        try:
            msg = email_lib.message_from_bytes(raw)
        except Exception:
            continue
        items.append(
            {
                "uid": uid,
                "from": _decode_value(msg.get("From")),
                "subject": _decode_value(msg.get("Subject")),
                "body": _body_text(msg),
            }
        )
    return items


def _code_from_messages(messages: list[dict], exclude_ids=None) -> str:
    """Lay ma 6 so tu thu moi nhat. Uu tien thu cua Muse/Meta (From hoac
    Subject co 'muse'/'meta'), neu khong thi thu moi nhat co ma 6 so."""
    seen = {str(i) for i in (exclude_ids or ())}
    muse_first: list[dict] = []
    others: list[dict] = []
    for item in messages:
        if str(item.get("uid") or "") in seen:
            continue
        hay = f"{item.get('from','')} {item.get('subject','')}".lower()
        (muse_first if ("muse" in hay or "meta" in hay) else others).append(item)
    for item in muse_first + others:
        match = _CODE_RE.search(item.get("body") or "")
        if match:
            return match.group(1)
    return ""


def _poll_once(email: str, app_password: str, exclude_ids=None) -> str:
    mail = _connect(email, app_password)
    try:
        return _code_from_messages(_recent_messages(mail), exclude_ids=exclude_ids)
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def wait_for_code(
    token: str,
    *,
    timeout: float = 120,
    interval: float = 5,
    exclude_ids: set[str] | None = None,
    proxies=None,
) -> str:
    """Cho thu moi va lay ma 6 so. token la 'email|mat-khau-ung-dung'.
    proxies bi bo qua: doc mail IMAP di truc tiep, khong qua proxy."""
    email, app_password = _split_token(token)
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            code = _poll_once(email, app_password, exclude_ids=exclude_ids)
        except GmailError as exc:
            last_error = str(exc)
            time.sleep(interval)
            continue
        if code:
            return code
        time.sleep(interval)
    raise GmailError(
        "Het gio chua thay ma moi trong hop Gmail"
        + (f" ({last_error})" if last_error else "")
    )


def message_ids(token: str, proxies=None) -> set[str]:
    """UID cua cac thu hien co — de lan dang nhap sau biet thu nao la cu."""
    email, app_password = _split_token(token)
    mail = _connect(email, app_password)
    try:
        return {str(item.get("uid") or "") for item in _recent_messages(mail)}
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def latest_code(address: str, password: str = "", proxies=None) -> str:
    """Ma moi nhat dang nam trong hop thu (dung lai cho lan dang nhap sau).
    Tra '' neu hop thu khong co ma nao."""
    address = (address or "").strip()
    app_password = re.sub(r"\s+", "", (password or "").strip())
    if not _GMAIL_RE.match(address) or len(app_password) < 16:
        return ""
    try:
        return _poll_once(address, app_password)
    except GmailError:
        return ""


def login_mailbox(address: str, password: str = "", proxies=None) -> str:
    """Kiem tra dang nhap IMAP duoc thi tra token 'email|mat-khau-ung-dung'."""
    address = (address or "").strip()
    app_password = re.sub(r"\s+", "", (password or "").strip())
    if not _GMAIL_RE.match(address):
        raise GmailError("Dia chi email khong phai Gmail (@gmail.com)")
    if len(app_password) < 16:
        raise GmailError(
            "Chua co mat khau ung dung Gmail (16 ky tu) — tao o Tai khoan Google -> "
            "Bao mat -> Xac minh 2 buoc -> Mat khau ung dung."
        )
    mail = _connect(address, app_password)
    try:
        mail.select("INBOX", readonly=True)
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    return f"{address}|{app_password}"


def _take_from_pool(password: str) -> tuple[str, str, str]:
    """Lay Gmail chua dung trong Kho Gmail. Tra ve (email, password truyen
    vao, token) — password tra ve la ACCOUNT_PASSWORD chung, giong mail
    temp, de lam mat khau tai khoan Muse + luu kho; mat khau ung dung that
    chi nam trong token."""
    from gmail_store import take_gmail_account

    row = take_gmail_account()
    if not row:
        raise GmailError(
            "Kho Gmail da het: khong con Gmail nao san sang — dan them Gmail "
            "(email|mat-khau-ung-dung) vao Kho Gmail roi tao lai."
        )
    email = row["email"]
    return email, password, f"{email}|{row['app_password']}"


def create_han_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    # Gmail khong tao dia chi han + 9 so — lay Gmail co san trong kho.
    return _take_from_pool(password)


def create_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    return _take_from_pool(password)
