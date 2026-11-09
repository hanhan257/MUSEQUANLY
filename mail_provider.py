"""Chon nha cung cap mail tam cho viec tao tai khoan Muse.

Hai provider co cung giao dien (create_han_mailbox, create_mailbox,
login_mailbox, message_ids, wait_for_code, latest_code) nen doi provider
chi la doi module:
- "tempmail": tempmail.plus (mac dinh, dung tu v62). Khong can tao tai
  khoan/dang nhap — dia chi email chinh la token, ai biet dia chi cung doc
  duoc inbox nen dia chi phai ngau nhien.
- "mailtm": mail.tm (dung truoc v62, bo theo yeu cau luc do). Tao hop thu
  qua API, co mat khau + token JWT de dang nhap lai. Ten mien mac dinh
  fldubai.com van dang hoat dong (kiem tra 2026-11-09: tao + doc inbox OK).
"""
import importlib

PROVIDERS = {
    "tempmail": ("tempmail_plus_client", "TempMailPlusError", "tempmail.plus"),
    "mailtm": ("mailtm_client", "MailTmError", "mail.tm"),
}


def normalize_provider(value) -> str:
    """'mailtm'/'mail.tm' -> 'mailtm', moi thu khac -> 'tempmail' (mac dinh)."""
    v = (value or "").strip().lower().replace(".", "").replace("-", "")
    if v in ("mailtm",):
        return "mailtm"
    return "tempmail"


def client(provider):
    """Module client cua provider (co cac ham create/wait/login giong nhau)."""
    name, _, _ = PROVIDERS[normalize_provider(provider)]
    return importlib.import_module(name)


def error_class(provider):
    """Lop loi cua provider (de bat dung loi khi cho ma / mo hop thu)."""
    mod = client(provider)
    _, err_name, _ = PROVIDERS[normalize_provider(provider)]
    return getattr(mod, err_name)


def label(provider) -> str:
    """Ten hien thi cua provider (dung trong log/trang thai)."""
    return PROVIDERS[normalize_provider(provider)][2]


def provider_for_email(email) -> str:
    """Suy luan provider tu domain cua email (dung khi dang nhap lai TK cu,
    luc do khong co lua chon nao duoc luu theo TK)."""
    from tempmail_plus_client import DOMAINS as TEMPMAIL_DOMAINS

    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    return "tempmail" if domain in {d.lower() for d in TEMPMAIL_DOMAINS} else "mailtm"


def create_mailbox_for(provider, password: str, mode: str) -> tuple[str, str, str]:
    """Tao hop thu moi: mode 'password' -> han + 9 so, 'plain' -> ngau nhien.
    Tra ve (email, mat khau hop thu, token de cho ma)."""
    mod = client(provider)
    provider = normalize_provider(provider)
    if provider == "tempmail":
        if mode == "password":
            return mod.create_han_mailbox(password, "mailto.plus")
        return mod.create_mailbox(password)
    if mode == "password":
        return mod.create_han_mailbox(password, "fldubai.com")
    return mod.create_mailbox(password, "fldubai.com")
