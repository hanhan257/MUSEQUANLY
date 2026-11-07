"""Persist Muse session cookies per account (local file)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app_paths import DATA_DIR

COOKIES_FILE = DATA_DIR / "cookies.json"
COOKIES_EXPORT_TXT = DATA_DIR / "cookies_export.txt"

MUSE_DOMAIN_PARTS = ("muse.ai", "meta.com", "facebook.com", "fb.com", "instagram.com")
_io_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Cache giữ trong bộ nhớ: cookies.json đã ~4MB, đọc + parse lại mỗi lần
# get_account_cookies() gọi là quá chậm (một lần chuyển kho gọi ~200 lần).
# Khóa cache là (mtime, size) của file: file đổi (ghi mới / git pull từ máy
# khác) thì đọc lại, không thì dùng bản đã parse. Mọi ghi đều qua _save_raw
# nên cache luôn được cập nhật sau khi ghi.
_cache: dict[str, Any] | None = None
_cache_key: tuple[int, int] | None = None


def _load_raw() -> dict[str, Any]:
    global _cache, _cache_key
    with _io_lock:
        if not COOKIES_FILE.exists():
            _cache, _cache_key = {}, None
            return {}
        try:
            st = COOKIES_FILE.stat()
        except OSError:
            return _cache if _cache is not None else {}
        key = (st.st_mtime_ns, st.st_size)
        if _cache is not None and _cache_key == key:
            return _cache
        try:
            data = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _cache if _cache is not None else {}
        _cache, _cache_key = data, key
        return data


def _write_export_txt(data: dict[str, Any]) -> None:
    lines: list[str] = []
    for key in sorted(data.keys()):
        rec = data[key]
        header = rec.get("cookie_header") or rec.get("muse_cookie_header") or ""
        email = rec.get("email") or key
        if header:
            lines.append(f"{email}|{header}")
    COOKIES_EXPORT_TXT.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _save_raw(data: dict[str, Any]) -> None:
    global _cache, _cache_key
    with _io_lock:
        COOKIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_export_txt(data)
        try:
            st = COOKIES_FILE.stat()
            _cache, _cache_key = data, (st.st_mtime_ns, st.st_size)
        except OSError:
            _cache, _cache_key = data, None


def token_from_cookie_header(header: str) -> str:
    for part in (header or "").split(";"):
        piece = part.strip()
        if piece.startswith("hatch_sess="):
            return piece.split("=", 1)[1].strip()
    return ""


def normalize_email_key(email: str) -> str:
    return email.strip().lower()
    return email.strip().lower()


def filter_muse_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in cookies:
        domain = (c.get("domain") or "").lower()
        if any(part in domain for part in MUSE_DOMAIN_PARTS):
            out.append(c)
    return out


def cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for c in cookies:
        name = c.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={c.get('value', '')}")
    return "; ".join(parts)


def save_account_cookies(
    email: str,
    playwright_cookies: list[dict[str, Any]],
    *,
    cookie_header_override: str | None = None,
    cookie_source: str = "browser",
    muse_token: str = "",
    account_status: str = "",
    login_error: str = "",
) -> dict[str, Any]:
    relevant = filter_muse_cookies(playwright_cookies)
    muse_only = [c for c in relevant if "muse.ai" in (c.get("domain") or "").lower()]
    header = cookies_to_header(relevant)
    if cookie_header_override and cookie_header_override.strip():
        header = cookie_header_override.strip()
    record = {
        "email": email.strip(),
        "saved_at": _now_iso(),
        "cookie_header": header,
        "muse_cookie_header": cookies_to_header(muse_only) if muse_only else header,
        "count": len(relevant),
        "cookies": relevant,
        "cookie_source": cookie_source,
        "muse_token": muse_token,
        "account_status": account_status,
        "login_error": login_error,
    }
    with _io_lock:
        data = _load_raw()
        data[normalize_email_key(email)] = record
        _save_raw(data)
    return record


def update_account_meta(email: str, **fields: Any) -> dict[str, Any]:
    """Ghi lỗi đăng nhập / trạng thái mà không xóa cookie đang có."""
    key = normalize_email_key(email)
    with _io_lock:
        data = _load_raw()
        rec = data.get(key) or {"email": email.strip()}
        for name, value in fields.items():
            rec[name] = value
        rec["email"] = rec.get("email") or email.strip()
        data[key] = rec
        _save_raw(data)
    return rec


def get_account_cookies(email: str) -> dict[str, Any] | None:
    return _load_raw().get(normalize_email_key(email))


def delete_account_cookies(email: str) -> bool:
    return delete_account_cookies_many([email]) > 0


def delete_account_cookies_many(emails) -> int:
    data = _load_raw()
    removed = 0
    for email in emails or []:
        key = normalize_email_key(email or "")
        if key and key in data:
            del data[key]
            removed += 1
    if removed:
        _save_raw(data)
    return removed


def list_all_cookies() -> dict[str, Any]:
    return _load_raw()


def export_lines() -> list[str]:
    lines: list[str] = []
    for key, rec in sorted(_load_raw().items()):
        header = rec.get("cookie_header") or rec.get("muse_cookie_header") or ""
        email = rec.get("email") or key
        if header:
            lines.append(f"{email}|{header}")
    return lines


def export_all_records() -> list[dict[str, Any]]:
    return [v for _, v in sorted(_load_raw().items()) if v.get("cookie_header") or v.get("cookies")]


def export_json_document() -> dict[str, Any]:
    accounts = []
    for rec in export_all_records():
        accounts.append(
            {
                "email": rec.get("email"),
                "saved_at": rec.get("saved_at"),
                "cookie_header": rec.get("cookie_header"),
                "muse_cookie_header": rec.get("muse_cookie_header"),
                "cookies": rec.get("cookies", []),
            }
        )
    return {"exported_at": _now_iso(), "source": "muse-quanly", "accounts": accounts}


def cookies_to_netscape(cookies: list[dict[str, Any]]) -> str:
    import time

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by Muse Account Manager — import vào curl, HTTPCookie, một số extension",
        "",
    ]
    now = int(time.time())
    for c in cookies:
        domain = (c.get("domain") or "").strip()
        if not domain:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        if not domain.startswith(".") and include_subdomains == "TRUE":
            pass
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires", -1)
        if not expires or expires < 0:
            expires = now + 86400 * 180
        name = c.get("name") or ""
        value = c.get("value") or ""
        if not name:
            continue
        lines.append(
            f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{int(expires)}\t{name}\t{value}"
        )
    return "\n".join(lines) + "\n"


def export_netscape_all() -> str:
    chunks = [
        "# Netscape HTTP Cookie File",
        "# Muse AI — tất cả tài khoản (gộp cookie, phân biệt theo comment email)",
        "",
    ]
    for rec in export_all_records():
        email = rec.get("email") or "unknown"
        chunks.append(f"# --- {email} ---")
        body = cookies_to_netscape(rec.get("cookies") or [])
        for line in body.splitlines():
            if line.startswith("# Netscape") or line.startswith("# Generated") or not line.strip():
                continue
            chunks.append(line)
        chunks.append("")
    return "\n".join(chunks)


def export_curl_snippets() -> str:
    lines = ["# Muse AI — dán Cookie header vào tool HTTP của bạn", ""]
    for rec in export_all_records():
        email = rec.get("email") or ""
        header = rec.get("cookie_header") or rec.get("muse_cookie_header") or ""
        if not header:
            continue
        escaped = header.replace('"', '\\"')
        lines.append(f"# {email}")
        lines.append(f'curl "https://muse.ai/" -H "Cookie: {escaped}"')
        lines.append("")
    return "\n".join(lines)


def export_for_email(email: str, fmt: str) -> tuple[str, str, str] | None:
    """Return (content, filename, mimetype) or None if no cookie."""
    rec = get_account_cookies(email)
    if not rec:
        return None
    safe_name = normalize_email_key(email).replace("@", "_at_")
    header = rec.get("cookie_header") or rec.get("muse_cookie_header") or ""
    cookies = rec.get("cookies") or []

    if fmt in ("txt", "header", "pipe"):
        if not header:
            return None
        return f"{rec.get('email', email)}|{header}\n", f"muse-cookie-{safe_name}.txt", "text/plain; charset=utf-8"
    if fmt == "json":
        payload = {
            "email": rec.get("email", email),
            "saved_at": rec.get("saved_at"),
            "cookie_header": rec.get("cookie_header"),
            "muse_cookie_header": rec.get("muse_cookie_header"),
            "cookies": cookies,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2), f"muse-cookie-{safe_name}.json", "application/json"
    if fmt == "netscape":
        if not cookies:
            return None
        return cookies_to_netscape(cookies), f"muse-cookie-{safe_name}.txt", "text/plain; charset=utf-8"
    if fmt == "curl":
        if not header:
            return None
        escaped = header.replace('"', '\\"')
        body = f'# {rec.get("email", email)}\ncurl "https://muse.ai/" -H "Cookie: {escaped}"\n'
        return body, f"muse-curl-{safe_name}.sh", "text/plain; charset=utf-8"
    return None


def export_clipboard_text() -> str:
    return "\n".join(export_lines())


def export_filtered_lines(
    *,
    filter_mode: str = "has",
    search: str = "",
    limit: int = 100,
    scope_emails: list[str] | None = None,
) -> list[str]:
    """filter_mode: all | has | missing — search matches email substring."""
    search_l = search.strip().lower()
    limit = max(1, min(limit, 500))
    scope = {e.strip().lower() for e in scope_emails} if scope_emails else None
    lines: list[str] = []

    if filter_mode == "missing" and scope is not None:
        raw = _load_raw()
        for email in sorted(scope):
            if search_l and search_l not in email:
                continue
            rec = raw.get(normalize_email_key(email))
            header = (rec or {}).get("cookie_header") or (rec or {}).get("muse_cookie_header") or ""
            if not header:
                lines.append(email)
            if len(lines) >= limit:
                break
        return lines[:limit]

    for key, rec in sorted(_load_raw().items()):
        email = str(rec.get("email") or key)
        header = rec.get("cookie_header") or rec.get("muse_cookie_header") or ""
        has = bool(header)
        if scope is not None and email.lower() not in scope:
            continue
        if search_l and search_l not in email.lower():
            continue
        if filter_mode == "missing":
            if has:
                continue
            lines.append(email)
        elif filter_mode == "has":
            if not has:
                continue
            lines.append(f"{email}|{header}")
        else:
            if not has:
                continue
            lines.append(f"{email}|{header}")
        if len(lines) >= limit:
            break
    return lines[:limit]


def export_filtered_text(**kwargs: object) -> str:
    return "\n".join(export_filtered_lines(**kwargs))  # type: ignore[arg-type]


def export_bulk(fmt: str) -> tuple[str, str, str]:
    fmt = (fmt or "txt").lower()
    if fmt in ("txt", "header", "pipe"):
        text = "\n".join(export_lines())
        return text, "muse-cookies-all.txt", "text/plain; charset=utf-8"
    if fmt == "json":
        text = json.dumps(export_json_document(), ensure_ascii=False, indent=2)
        return text, "muse-cookies-all.json", "application/json"
    if fmt == "netscape":
        return export_netscape_all(), "muse-cookies-all.txt", "text/plain; charset=utf-8"
    if fmt == "curl":
        return export_curl_snippets(), "muse-cookies-curl.sh", "text/plain; charset=utf-8"
    text = "\n".join(export_lines())
    return text, "muse-cookies-all.txt", "text/plain; charset=utf-8"
