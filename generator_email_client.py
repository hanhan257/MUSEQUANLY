"""Tao hop thu tam tren https://generator.email/ va doc ma trong thu.

generator.email khong co API tao tai khoan/dang nhap chinh thuc, nhung
co che giong tempmail.plus: bat ky dia chi nao o ten mien dang hoat dong
cung nhan duoc thu, dia chi chinh la token, ai biet dia chi cung doc duoc
inbox nen dia chi phai ngau nhien, khong doan duoc.

Cach doc mail (rut tu JS cua trang, khong phai API cong khai):
- Danh sach domain: GET /api/domains.php, kem header X-API-Token lay tu
  the <meta name="api-token"> tren trang chu + Referer. Danh sach xoay
  (moi lan goi tra 15 domain khac nhau) nen PHAI lay live moi lan tao
  mail, khong hardcode domain.
- Kiem tra domain con hoat dong: GET /search.php?key=<domain> tra
  ["<domain>"] neu con hoat dong, [] neu khong.
- Doc inbox: GET /<domain>/<user> (trang HTML). Moi dong thu la mot div
  co onclick loadInboxClientSide('/<domain>/<user>/<id>') — id la doan
  thu 3 (bo qua page_is_N la phan trang, bo qua link chi co 2 doan la
  lich su mailbox). Thu moi nhat nam dau danh sach.
- Doc noi dung thu: dat cookie inbox_ctx=<domain>/<user>/<id> (URL-encode)
  roi GET /inbox1/ — server render noi dung thu theo cookie do. Du phong:
  GET truc tiep link /<domain>/<user>/<id> roi regex ma trong text.

Tat ca request deu di qua proxy khi co proxies (giong v115) vi may user
co the loi DNS truc tiep.
"""

from __future__ import annotations

import random
import re
import secrets
import string
import threading
import time
from urllib.parse import quote

import requests

BASE = "https://generator.email"
# Domain du phong neu khong lay duoc danh sach live (danh sach xoay nhanh
# nen cac domain nay co the het han — chi dung khi live fetch loi, va
# _open_mailbox se kiem tra inbox mo duoc moi chap nhan).
FALLBACK_DOMAINS = (
    "fdger88.cc",
    "gdqoe.net",
    "xirlotamqen.fun",
    "reteme.online",
    "mutudev.com",
)
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_LINK = re.compile(r"""loadInboxClientSide\(['"]([^'"]+)['"]\)""")
_PAGE_IS = re.compile(r"^page_is_\d+$")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

_pace_lock = threading.Lock()
_next_slot = 0.0
_domains_lock = threading.Lock()
_domains_cache: tuple[float, list[str]] = (0.0, [])


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


def _send(method: str, url: str, proxies=None, **kwargs) -> requests.Response:
    """Goi generator.email. proxies la dict requests (xem
    mail_provider.requests_proxies): doc mail di qua proxy de proxy phan
    giai DNS thay may local. Proxy hong/chet thi thu truc tiep 1 lan; ca
    hai deu loi thi bao loi ngan gon (khong kem URL/email)."""
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("headers", {})
    kwargs["headers"].setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    )
    last_status = 429
    via = "proxy" if proxies else "trực tiếp"
    tried: list[str] = []
    for _ in range(8):
        _slot()
        try:
            response = requests.request(method, url, proxies=proxies, **kwargs)
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
            raise GeneratorEmailError(
                f"Không gọi được generator.email ({where}: {_describe_request_error(exc)})"
            ) from exc
        if response.status_code in (429, 503) or response.status_code >= 500:
            last_status = response.status_code
            _hold(6)
            time.sleep(6)
            continue
        return response
    raise GeneratorEmailError(f"generator.email bận ({last_status})")


class GeneratorEmailError(RuntimeError):
    pass


def _api_token(proxies=None, timeout: float = 30) -> str:
    """Token cho /api/domains.php: lay tu the meta tren trang chu (moi lan
    tai trang la mot token moi)."""
    response = _send("GET", f"{BASE}/", proxies=proxies, timeout=timeout)
    if response.status_code != 200:
        raise GeneratorEmailError(
            f"generator.email lỗi {response.status_code} khi mở trang chủ"
        )
    match = re.search(r'<meta name="api-token" content="([^"]+)"', response.text)
    if not match:
        raise GeneratorEmailError("Không lấy được api-token của generator.email")
    return match.group(1)


def list_domains(proxies=None) -> list[str]:
    """Danh sach domain dang hoat dong (live, xoay theo lan goi). Cache
    10 phut trong process de khoi goi lien tuc khi tao nhieu TK."""
    global _domains_cache
    with _domains_lock:
        ts, cached = _domains_cache
        if cached and time.monotonic() - ts < 600:
            return list(cached)
    token = _api_token(proxies=proxies)
    response = _send(
        "GET",
        f"{BASE}/api/domains.php",
        headers={"X-API-Token": token, "Referer": f"{BASE}/"},
        proxies=proxies,
    )
    if response.status_code == 403:
        raise GeneratorEmailError("generator.email từ chối liệt kê domain (cần Referer/token mới)")
    if response.status_code != 200:
        raise GeneratorEmailError(
            f"generator.email lỗi {response.status_code} khi liệt kê domain"
        )
    try:
        data = response.json()
    except Exception as exc:
        raise GeneratorEmailError("generator.email trả về danh sách domain không phải JSON") from exc
    domains = [str(item.get("ascii") or "") for item in (data if isinstance(data, list) else [])]
    domains = [d for d in domains if d and "." in d]
    if not domains:
        raise GeneratorEmailError("generator.email trả về danh sách domain rỗng")
    with _domains_lock:
        _domains_cache = (time.monotonic(), domains)
    return domains


def is_active_domain(domain: str, proxies=None, timeout: float = 10) -> bool:
    """Domain co dang hoat dong tren generator.email khong (dung de suy
    luan provider tu email cu). Tra False neu goi loi — khong raise.
    Timeout ngan vi chi la du phong suy luan (TK tao bang tool da luu
    provider trong kho, khong can goi ham nay)."""
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return False
    try:
        token = _api_token(proxies=proxies, timeout=timeout)
        response = _send(
            "GET",
            f"{BASE}/search.php",
            params={"key": domain},
            headers={"X-API-Token": token, "Referer": f"{BASE}/"},
            proxies=proxies,
            timeout=timeout,
        )
        if response.status_code != 200:
            return False
        found = response.json()
        return isinstance(found, list) and domain in [str(x).lower() for x in found]
    except Exception:
        return False


def _pick_domain(proxies=None) -> str:
    """Chon ngau nhien 1 domain trong danh sach live; live fetch loi thi
    dung domain du phong (se duoc _open_mailbox kiem tra lai)."""
    try:
        domains = list_domains(proxies=proxies)
        return random.choice(domains)
    except GeneratorEmailError:
        return random.choice(FALLBACK_DOMAINS)


def _homepage_domain(proxies=None) -> str:
    """Domain dang hoat dong, lay tu dia chi tu dong tren trang chu.
    Mo https://generator.email/ la trang tu sinh 1 dia chi (vd
    mgdias37@capcutmeflo.shop): o userName chi chua phan local, domain
    nam rieng o input domainName2 — ghep lai la dia chi day du tren 1
    domain dang chay. Lay domain do thay vi goi API /api/domains.php
    (can api-token + Referer, de loi hon). Proxy tra trang sai (khong
    co domainName2) thi thu truc tiep 1 lan. Raise neu ca 2 duong deu
    khong lay duoc domain."""
    def _domain_from(html: str) -> str:
        tag = re.search(r'<input[^>]*id="domainName2"[^>]*>', html or "")
        if not tag:
            return ""
        value = re.search(r'value="([^"]*)"', tag.group(0))
        domain = (value.group(1) if value else "").strip().lower()
        return domain if domain and "." in domain else ""

    domain = ""
    try:
        response = _send("GET", f"{BASE}/", proxies=proxies)
        if response.status_code == 200:
            domain = _domain_from(response.text)
    except GeneratorEmailError:
        domain = ""
    if not domain and proxies:
        try:
            response = _send("GET", f"{BASE}/", proxies=None)
            if response.status_code == 200:
                domain = _domain_from(response.text)
        except GeneratorEmailError:
            domain = ""
    if not domain:
        raise GeneratorEmailError("Không lấy được địa chỉ tự động từ trang chủ generator.email")
    return domain


def _pick_domain_for_create(proxies=None) -> str:
    """Domain de tao mail moi: uu tien domain tu dia chi tu dong tren
    trang chu (domain dang chay that, giong cach web tu sinh dia chi),
    roi moi den danh sach API xoay."""
    try:
        return _homepage_domain(proxies=proxies)
    except GeneratorEmailError:
        return _pick_domain(proxies=proxies)


def _split_email(email: str) -> tuple[str, str]:
    local, _, domain = (email or "").strip().partition("@")
    return local.strip().lower(), domain.strip().lower()


def _page_address(html: str) -> str:
    """Dia chi dang hien trong o userName cua trang inbox ('' neu khong
    co o nay — tuc la trang tra ve khong phai trang inbox)."""
    match = re.search(r'id="userName"[^>]*value="([^"]*)"', html or "")
    return (match.group(1) if match else "").strip().lower()


def _page_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html or "", re.S)
    return _WS.sub(" ", match.group(1)).strip()[:80] if match else ""


_CHALLENGE_HINTS = (
    "security checking",
    "just a moment",
    "attention required",
    "access denied",
    "verify you are human",
    "are you a robot",
    "please verify",
    "checking your browser",
)


def _is_challenge_page(html: str) -> bool:
    """Trang chan/bot-check tra ve 200 nhung khong phai inbox (can thu
    lai sau vai giay thay vi bao loi dia chi)."""
    low = (html or "").lower()
    return any(hint in low for hint in _CHALLENGE_HINTS) and "email-table" not in low


def _check_inbox_page(html: str, email: str) -> None:
    """Kiem tra trang inbox mo dung dia chi yeu cau chua.
    - Trang chan -> loi 'ban' (vong tao se cho 6s roi thu lai).
    - Trang tra ve dia chi khac (vd trang chu voi dia chi ngau nhien) ->
      loi 'mo khong dung dia chi' kem dia chi tren trang + tieu de trang,
      de biet chinh xac may nhan duoc trang gi (proxy tra trang sai, DNS
      tra ve may khac...)."""
    local, _ = _split_email(email)
    if _is_challenge_page(html):
        raise GeneratorEmailError("generator.email bận (trang chặn/bot-check, thử lại sau)")
    shown = _page_address(html)
    if shown == local:
        return
    if not shown and local in (html or ""):
        return
    title = _page_title(html)
    detail = f"trang trả về: {title or 'không rõ tiêu đề'}"
    if shown:
        detail += f", địa chỉ trên trang: {shown}"
    raise GeneratorEmailError(f"{email} mở không đúng địa chỉ ({detail})")


def _inbox_page(email: str, proxies=None) -> str:
    """HTML trang inbox /<domain>/<user>. Raise neu khong mo duoc."""
    local, domain = _split_email(email)
    if not local or not domain:
        raise GeneratorEmailError("Địa chỉ email không đúng định dạng")
    response = _send("GET", f"{BASE}/{domain}/{local}", proxies=proxies)
    if response.status_code != 200:
        raise GeneratorEmailError(
            f"generator.email lỗi {response.status_code} khi mở hộp thư"
        )
    return response.text


def _inbox_page_checked(email: str, proxies=None) -> str:
    """HTML trang inbox, da kiem tra dung dia chi. Neu proxy tra ve
    trang sai (200 nhung khong phai inbox, vd trang 'No restrictions'
    cua proxy) thi thu truc tiep 1 lan truoc khi bao loi — vi loi nay
    la do duong proxy chu khong phai do domain, doi domain khac cung
    nhan lai trang sai do. Trang chan ('ban') thi khong doi duong, de
    vong tao thu lai sau 6s. Truc tiep cung loi thi bao loi goc (qua
    proxy) de user thay duoc tieu de trang proxy tra ve."""
    html = _inbox_page(email, proxies=proxies)
    try:
        _check_inbox_page(html, email)
        return html
    except GeneratorEmailError as exc:
        if proxies and "mở không đúng địa chỉ" in str(exc):
            try:
                direct_html = _inbox_page(email, proxies=None)
                _check_inbox_page(direct_html, email)
                return direct_html
            except GeneratorEmailError:
                pass
        raise


def _message_links(html: str) -> list[str]:
    """Danh sach id thu trong trang inbox, thu moi nhat truoc. Bo qua
    phan trang (page_is_N) va link lich su mailbox (chi co 2 doan)."""
    ids: list[str] = []
    for link in _LINK.findall(html):
        parts = [p for p in link.split("/") if p]
        if len(parts) < 3:
            continue
        seg3 = parts[2]
        if _PAGE_IS.match(seg3):
            continue
        if seg3 not in ids:
            ids.append(seg3)
    return ids


def _text_of(html: str) -> str:
    """Bo tag HTML, gom khoang trang — de regex ma 6 so trong noi dung."""
    text = _TAG.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return _WS.sub(" ", text)


def _message_body(email: str, message_id: str, proxies=None) -> str:
    """Noi dung thu (text). Cach chinh: dat cookie inbox_ctx roi GET
    /inbox1/. Du phong: GET truc tiep link /<domain>/<user>/<id>."""
    local, domain = _split_email(email)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        }
    )
    cookie_value = quote(f"{domain}/{local}/{message_id}", safe="")
    # Cookie phai di kem moi request trong session nay; _send khong nhan
    # session nen goi requests truc tiep o day (van qua proxy neu co).
    last_error = ""
    for url in (f"{BASE}/inbox1/", f"{BASE}/{domain}/{local}/{message_id}"):
        try:
            _slot()
            response = session.get(
                url,
                proxies=proxies,
                timeout=30,
                cookies={"inbox_ctx": cookie_value},
                headers={"Referer": f"{BASE}/{domain}/{local}"},
            )
            if response.status_code == 200 and response.text:
                return _text_of(response.text)
            last_error = f"HTTP {response.status_code}"
        except requests.exceptions.RequestException as exc:
            last_error = _describe_request_error(exc)
            if proxies:
                proxies = None
                continue
    raise GeneratorEmailError(f"Không đọc được thư trên generator.email ({last_error})")


def _code_from_text(text: str) -> str:
    match = _CODE.search(text or "")
    return match.group(1) if match else ""


def login_mailbox(address: str, password: str = "", proxies=None) -> str:
    """Mo hop thu da co. generator.email khong can mat khau, token la dia
    chi. Kiem tra trang inbox mo duoc va dung dia chi."""
    _inbox_page_checked(address, proxies=proxies)
    return address


def confirm_mailbox_login(address: str, password: str = "", proxies=None) -> str:
    """Mo lai hop thu va doc dung dia chi. Token tao luc nay khong tinh."""
    return login_mailbox(address, password, proxies=proxies)


def message_ids(token: str, proxies=None) -> set[str]:
    html = _inbox_page_checked(token, proxies=proxies)
    return set(_message_links(html))


def _open_mailbox(address: str, password: str, proxies=None) -> tuple[str, str, str]:
    """Dia chi moi la dung duoc ngay. Goi inbox mot lan de chac hop thu
    mo duoc va chua co thu la (dia chi ngau nhien bi trung thi doi so)."""
    html = _inbox_page_checked(address, proxies=proxies)
    if _message_links(html):
        raise GeneratorEmailError(f"{address} bị trùng")
    return address, password, address


def create_han_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    """Mail han + 9 so @<domain generator.email xoay>, co mat khau (giu
    lai cho tuong thich, generator.email khong dung mat khau). Trung thi
    doi so. Tra ve (email, password, token) voi token la chinh dia chi
    email."""
    last_error = "Không tạo được mail"
    for _ in range(12):
        picked = (domain or "").strip().lower() or _pick_domain_for_create(proxies=proxies)
        local = f"han{secrets.randbelow(1_000_000_000):09d}"
        address = f"{local}@{picked}"
        try:
            return _open_mailbox(address, password, proxies=proxies)
        except GeneratorEmailError as exc:
            text = str(exc)
            # Trang tra ve sai dia chi co the do proxy tra trang cu/trang
            # chan nhat thoi — doi domain khac thu lai thay vi chet ngay.
            if "bị trùng" in text or "bận" in text or "không phải JSON" in text or "mở không đúng địa chỉ" in text:
                last_error = text
                if "bận" in text or "không phải JSON" in text:
                    time.sleep(6)
                elif "mở không đúng địa chỉ" in text:
                    time.sleep(2)
                continue
            raise
    raise GeneratorEmailError(last_error)


def create_mailbox(password: str, domain: str | None = None, proxies=None) -> tuple[str, str, str]:
    """Mail ngau nhien @<domain generator.email xoay>, co mat khau de giu
    tuong thich ham cu."""
    alphabet = string.ascii_lowercase + string.digits
    last_error = "Không tạo được mail"
    for _ in range(12):
        picked = (domain or "").strip().lower() or _pick_domain_for_create(proxies=proxies)
        local = "".join(secrets.choice(alphabet) for _ in range(12))
        address = f"{local}@{picked}"
        try:
            return _open_mailbox(address, password, proxies=proxies)
        except GeneratorEmailError as exc:
            text = str(exc)
            if "bị trùng" in text or "bận" in text or "không phải JSON" in text or "mở không đúng địa chỉ" in text:
                last_error = text
                if "bận" in text or "không phải JSON" in text:
                    time.sleep(6)
                elif "mở không đúng địa chỉ" in text:
                    time.sleep(2)
                continue
            raise
    raise GeneratorEmailError(last_error)


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
    seen: set[str] = set(exclude_ids or ())
    while time.time() < deadline:
        try:
            html = _inbox_page_checked(token, proxies=proxies)
        except GeneratorEmailError as exc:
            if "bận" in str(exc):
                time.sleep(6)
                continue
            raise
        for message_id in _message_links(html):
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            try:
                code = _code_from_text(_message_body(token, message_id, proxies=proxies))
            except GeneratorEmailError:
                continue
            if code:
                return code
        time.sleep(interval)
    raise GeneratorEmailError("Hết giờ chưa thấy mã mới trong hộp generator.email")


def latest_code(address: str, password: str = "", proxies=None) -> str:
    """Ma moi nhat dang nam trong hop thu, dung lai cho lan dang nhap sau."""
    login_mailbox(address, password, proxies=proxies)
    html = _inbox_page_checked(address, proxies=proxies)
    for message_id in _message_links(html):
        if not message_id:
            continue
        code = _code_from_text(_message_body(address, message_id, proxies=proxies))
        if code:
            return code
    raise GeneratorEmailError("Hộp generator.email chưa có mã xác minh")
