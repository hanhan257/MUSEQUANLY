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
- "genemail": generator.email (them tu v117). Khong can tao tai khoan/
  dang nhap — dia chi la token giong tempmail.plus. Domain xoay (lay live
  tu /api/domains.php moi lan tao mail), doc inbox bang trang HTML
  /<domain>/<user> + cookie inbox_ctx (xem generator_email_client.py).
- "guerrilla": Guerrilla Mail (them tu v128). API cong khai, khong can
  dang ky/key — session theo cookie PHPSESSID, dat ten han + 9 so qua
  set_email_user (xem guerrilla_mail_client.py). Mail chi song 1 gio.
- "gmail": Gmail that cua user (them tu v130). KHONG tao moi — lay Gmail
  chua dung trong Kho Gmail (gmail_store.py), 1 Gmail = 1 TK Muse; doc
  ma OTP qua IMAP bang mat khau ung dung 16 ky tu. Mat khau ung dung chi
  nam trong gmail_accounts.json (da gitignore), khong ghi vao kho TK.
- "yahoo": Yahoo Mail that cua user (them tu v131). KHONG tao moi — lay Yahoo
  chua dung trong Kho Yahoo (yahoo_store.py), 1 Yahoo = 1 TK Muse; doc
  ma OTP qua IMAP bang mat khau ung dung 16 ky tu (Yahoo -> Bao mat tai
  khoan -> Ket noi ben ngoai -> Tao mat khau ung dung). Mat khau ung dung
  chi nam trong yahoo_accounts.json (da gitignore), khong ghi vao kho TK.
- "tempemail": Tempemail.cc (them tu v131). API cong khai, khong can dang
  ky/key — POST /api/accounts (can UA trinh duyet + mat khau co 1 chu
  hoa), doc qua GET /api/messages + JWT. Token JWT chi co luc tao, luu
  vao tempemail_tokens.json (da gitignore) de dang nhap lai.
- "tenminutemail": 10 Minute Mail (them tu v131). Khong co API cong khai
  — dung 2 endpoint AJAX cu cua 10minutemail.net, doc hop thu can cung
  session luc tao dia chi. Dia chi chi song 10 phut, khong dang nhap
  lai duoc.
"""
import importlib
from urllib.parse import quote, unquote, urlparse

PROVIDERS = {
    "tempmail": ("tempmail_plus_client", "TempMailPlusError", "tempmail.plus"),
    "mailtm": ("mailtm_client", "MailTmError", "mail.tm"),
    "genemail": ("generator_email_client", "GeneratorEmailError", "generator.email"),
    "guerrilla": ("guerrilla_mail_client", "GuerrillaMailError", "guerrilla-mail"),
    "gmail": ("gmail_client", "GmailError", "gmail"),
    "yahoo": ("yahoo_client", "YahooError", "yahoo"),
    "tempemail": ("tempemail_client", "TempEmailError", "tempemail.cc"),
    "tenminutemail": ("tenminutemail_client", "TenMinuteMailError", "10minutemail"),
}


def requests_proxies(proxy) -> dict | None:
    """Chuyen proxy cua tool sang dang requests {'http':..., 'https':...} de
    viec doc mail (tao hop thu, cho ma, lay ma) di qua proxy giong Chrome.
    Proxy se phan giai DNS thay may local — may nao khong phan giai duoc
    tempmail.plus thi nho vay van doc duoc mail. Chap nhan chuoi
    'ip:port' / 'ip:port:user:pass' / 'scheme://user:pass@host:port' hoac
    dict Playwright {'server':..., 'username':..., 'password':...}.
    Tra ve None neu khong co proxy -> goi truc tiep nhu cu."""
    server = username = password = ""
    if isinstance(proxy, dict):
        server = (proxy.get("server") or "").strip()
        username = proxy.get("username") or ""
        password = proxy.get("password") or ""
    elif isinstance(proxy, str):
        text = proxy.strip()
        if not text:
            return None
        if "://" in text:
            parsed = urlparse(text)
            if not parsed.hostname:
                return None
            server = f"{parsed.scheme}://{parsed.hostname}"
            if parsed.port:
                server += f":{parsed.port}"
            username = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
        else:
            parts = text.split(":")
            if len(parts) >= 2 and parts[1].isdigit():
                # host:port[:user:pass] — mat khau co the chua @ hoac :
                # (kiem tra dang nay TRUOC dang user:pass@host:port vi
                # mat khau cung co the chua @).
                server = f"http://{parts[0]}:{parts[1]}"
                if len(parts) >= 4:
                    username, password = parts[2], ":".join(parts[3:])
            elif "@" in text:
                auth, hostport = text.rsplit("@", 1)
                username, _, password = auth.partition(":")
                server = f"http://{hostport}"
            else:
                return None
    else:
        return None
    if not server:
        return None
    if username:
        auth = quote(username, safe="") + ":" + quote(password, safe="") + "@"
        server = server.replace("://", f"://{auth}", 1)
    return {"http": server, "https": server}


def normalize_provider(value) -> str:
    """'mailtm'/'mail.tm' -> 'mailtm', 'genemail'/'generator.email' ->
    'genemail', 'guerrilla'/'guerrilla-mail' -> 'guerrilla', moi thu khac
    -> 'tempmail' (mac dinh)."""
    v = (value or "").strip().lower().replace(".", "").replace("-", "").replace(" ", "")
    if v in ("mailtm",):
        return "mailtm"
    if v in ("genemail", "generatoremail"):
        return "genemail"
    if v in ("guerrilla", "guerrillamail"):
        return "guerrilla"
    if v in ("gmail",):
        return "gmail"
    if v in ("yahoo",):
        return "yahoo"
    if v in ("tempemail", "tempemailcc"):
        return "tempemail"
    if v in ("tenminutemail", "10minutemail", "10mm"):
        return "tenminutemail"
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


def provider_for_email(email, proxies=None) -> str:
    """Suy luan provider tu email (dung khi dang nhap lai TK cu, luc do
    khong co lua chon nao duoc luu theo TK).
    Thu tu: (1) TK da tao co luu mail_provider trong kho thi lay thang do
    (chinh xac nhat, khong can goi mang); (2) domain trong danh sach
    tempmail.plus -> tempmail; (3) fldubai.com (domain duy nhat dang hoat
    dong cua mail.tm) -> mailtm; (4) domain con hoat dong tren
    generator.email (kiem tra live qua /search.php) -> genemail; (5) mac
    dinh mailtm nhu cu (cho TK import khong ro loai)."""
    email = (email or "").strip().lower()
    try:
        from warehouse_store import find_created_account

        row = find_created_account(email) or {}
        stored = (row.get("mail_provider") or "").strip().lower()
        if stored in PROVIDERS:
            return stored
    except Exception:
        pass
    from tempmail_plus_client import DOMAINS as TEMPMAIL_DOMAINS

    domain = email.rsplit("@", 1)[-1].strip().lower()
    if domain in {d.lower() for d in TEMPMAIL_DOMAINS}:
        return "tempmail"
    if domain == "fldubai.com":
        return "mailtm"
    try:
        from guerrilla_mail_client import DOMAINS as GUERRILLA_DOMAINS

        if domain in {d.lower() for d in GUERRILLA_DOMAINS}:
            return "guerrilla"
    except Exception:
        pass
    if domain in ("yahoo.com", "ymail.com"):
        return "yahoo"
    if domain == "gmail.com":
        return "gmail"
    if domain == "icmans.com":
        return "tempemail"
    try:
        from generator_email_client import is_active_domain

        if is_active_domain(domain, proxies=proxies, timeout=10):
            return "genemail"
    except Exception:
        pass
    return "mailtm"


def create_mailbox_for(provider, password: str, mode: str, proxies=None) -> tuple[str, str, str]:
    """Tao hop thu moi: mode 'password' -> han + 9 so, 'plain' -> ngau nhien.
    Tra ve (email, mat khau hop thu, token de cho ma). proxies la dict
    requests (xem requests_proxies) — doc mail se di qua proxy do."""
    mod = client(provider)
    provider = normalize_provider(provider)
    if provider == "tempmail":
        if mode == "password":
            return mod.create_han_mailbox(password, "mailto.plus", proxies=proxies)
        return mod.create_mailbox(password, proxies=proxies)
    if provider == "genemail":
        # Domain xoay — client tu lay danh sach live, khong truyen domain.
        if mode == "password":
            return mod.create_han_mailbox(password, proxies=proxies)
        return mod.create_mailbox(password, proxies=proxies)
    if provider == "guerrilla":
        # Domain do server chon theo session, khong truyen domain.
        if mode == "password":
            return mod.create_han_mailbox(password, proxies=proxies)
        return mod.create_mailbox(password, proxies=proxies)
    if provider == "yahoo":
        # Yahoo khong tao moi — lay Yahoo chua dung trong Kho Yahoo.
        # Tra ve (email, password truyen vao, token 'email|mat-khau-ung-dung'):
        # password la ACCOUNT_PASSWORD chung (lam mat khau TK Muse + luu
        # kho nhu cac provider khac); mat khau ung dung that chi nam trong
        # token va file yahoo_accounts.json.
        return mod.create_mailbox(password, proxies=proxies)
    if provider == "gmail":
        # Gmail khong tao moi — lay Gmail chua dung trong Kho Gmail.
        # Tra ve (email, password truyen vao, token 'email|mat-khau-ung-dung'):
        # password la ACCOUNT_PASSWORD chung (lam mat khau TK Muse + luu
        # kho nhu cac provider khac); mat khau ung dung that chi nam trong
        # token va file gmail_accounts.json.
        return mod.create_mailbox(password, proxies=proxies)
    if provider == "tempemail":
        # Domain lay live tu /api/domains, khong truyen domain.
        if mode == "password":
            return mod.create_han_mailbox(password, proxies=proxies)
        return mod.create_mailbox(password, proxies=proxies)
    if provider == "tenminutemail":
        # Dia chi ngau nhien do server chon, chi song 10 phut.
        if mode == "password":
            return mod.create_han_mailbox(password, proxies=proxies)
        return mod.create_mailbox(password, proxies=proxies)
    if mode == "password":
        return mod.create_han_mailbox(password, "fldubai.com", proxies=proxies)
    return mod.create_mailbox(password, "fldubai.com", proxies=proxies)
