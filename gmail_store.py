"""Kho Gmail: cac tai khoan Gmail cua user dung de tao tai khoan Muse.

Moi Gmail chi tao duoc 1 tai khoan Muse (email la danh tinh cua TK), nen
kho nay la kho hang: lay Gmail chua dung ra tao TK, xong danh dau da dung.
Doc ma OTP qua IMAP bang "mat khau ung dung" 16 ky tu cua Google (Tai
khoan Google -> Bao mat -> Xac minh 2 buoc -> Mat khau ung dung).

BAO MAT: mat khau ung dung cho quyen doc TOAN BO hop thu Gmail, nen chi
luu trong file rieng gmail_accounts.json, file nay da them vao .gitignore
va KHONG BAO GIO duoc commit/push len git. Cac ham list tra ve khong bao
gom mat khau. Khoa tai khoan tao (warehouses.json) chi luu ACCOUNT_PASSWORD
chung nhu cac provider khac — luc dang nhap lai, mat khau ung dung that
duoc lay lai tu kho nay theo email (xem login_muse / api_otp).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from typing import Any

from app_paths import DATA_DIR

GMAIL_FILE = DATA_DIR / "gmail_accounts.json"
_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@gmail\.com$", re.I)


def _load() -> dict[str, Any]:
    if not GMAIL_FILE.exists():
        return {"accounts": []}
    try:
        raw = GMAIL_FILE.read_text(encoding="utf-8")
    except OSError:
        return {"accounts": []}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"accounts": []}
    if not isinstance(data, dict):
        return {"accounts": []}
    rows = data.get("accounts")
    if not isinstance(rows, list):
        rows = []
    return {"accounts": [r for r in rows if isinstance(r, dict)]}


def _save(data: dict[str, Any]) -> None:
    tmp = GMAIL_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GMAIL_FILE)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    # Khong bao gio tra mat khau ung dung ra ngoai (API/UI/log).
    return {
        "id": (row.get("email") or "").strip().lower(),
        "email": (row.get("email") or "").strip(),
        "status": (row.get("status") or "ready").strip() or "ready",
        "uses": int(row.get("uses") or 0),
    }


def list_gmail_accounts() -> list[dict[str, Any]]:
    with _LOCK:
        return [_public(r) for r in _load().get("accounts", [])]


def gmail_summary() -> dict[str, Any]:
    rows = list_gmail_accounts()
    return {
        "gmail": rows,
        "total": len(rows),
        "ready": sum(1 for r in rows if r["status"] == "ready"),
        "used": sum(1 for r in rows if r["status"] != "ready"),
    }


def get_app_password(email: str) -> str:
    """Mat khau ung dung cua 1 Gmail trong kho (chi dung noi bo de doc
    IMAP luc dang nhap lai / lay ma). Tra '' neu khong co trong kho."""
    key = (email or "").strip().lower()
    if not key:
        return ""
    with _LOCK:
        for row in _load().get("accounts", []):
            if (row.get("email") or "").strip().lower() == key:
                return (row.get("app_password") or "").strip()
    return ""


def add_gmail_accounts(text: str) -> dict[str, Any]:
    """Them nhieu Gmail: moi dong `email|mat-khau-ung-dung` (chap nhan
    Tab, dau phay, dau | hoac khoang trang lam dau phan cach; mat khau
    16 ky tu cua Google thuong co khoang trang giua cac nhom 4 ky tu, nen
    khoang trang ben trong mat khau duoc giu lai). Trung email thi bo qua.
    Tra ve giong dang them the/proxy: added / duplicate / errors."""
    with _LOCK:
        data = _load()
        rows = data.get("accounts", [])
        existing = {(r.get("email") or "").strip().lower() for r in rows}
        added = 0
        duplicate = 0
        errors: list[int] = []
        for lineno, raw in enumerate((text or "").splitlines(), 1):
            line = (raw or "").strip()
            if not line:
                continue
            # Tach email (tu dau tien co @) khoi phan con lai (mat khau).
            parts = re.split(r"[\t|,;]+", line, maxsplit=1)
            if len(parts) < 2:
                parts = line.split(None, 1)
            if len(parts) < 2:
                errors.append(lineno)
                continue
            email = parts[0].strip()
            app_password = re.sub(r"\s+", "", parts[1].strip())
            if not _EMAIL_RE.match(email) or len(app_password) < 16:
                errors.append(lineno)
                continue
            key = email.lower()
            if key in existing:
                duplicate += 1
                continue
            rows.append(
                {
                    "email": email,
                    "app_password": app_password,
                    "status": "ready",
                    "uses": 0,
                    "added": datetime.now().strftime("%d/%m/%Y %H:%M"),
                }
            )
            existing.add(key)
            added += 1
        data["accounts"] = rows
        _save(data)
    return {"added": added, "duplicate": duplicate, "errors": errors, **gmail_summary()}


def take_gmail_account() -> dict[str, Any] | None:
    """Lay Gmail san sang dau tien, danh dau da dung (1 Gmail = 1 TK Muse).
    Tra ve dict co email + app_password, hoac None neu kho het."""
    with _LOCK:
        data = _load()
        for row in data.get("accounts", []):
            if (row.get("status") or "ready") == "ready":
                row["status"] = "used"
                row["uses"] = int(row.get("uses") or 0) + 1
                _save(data)
                return {"email": row.get("email") or "", "app_password": row.get("app_password") or ""}
    return None


def delete_gmail_accounts(ids: list[str]) -> int:
    keys = {(i or "").strip().lower() for i in (ids or []) if (i or "").strip()}
    if not keys:
        return 0
    with _LOCK:
        data = _load()
        before = len(data.get("accounts", []))
        data["accounts"] = [
            r for r in data.get("accounts", []) if (r.get("email") or "").strip().lower() not in keys
        ]
        removed = before - len(data["accounts"])
        if removed:
            _save(data)
        return removed


def reset_gmail_accounts(ids: list[str]) -> int:
    """Mo lai Gmail da dung ve san sang (dung khi muon dung lai email do,
    vi du lan tao truoc loi giua chung va Muse chua khoa email)."""
    keys = {(i or "").strip().lower() for i in (ids or []) if (i or "").strip()}
    if not keys:
        return 0
    with _LOCK:
        data = _load()
        reset = 0
        for row in data.get("accounts", []):
            if (row.get("email") or "").strip().lower() in keys and (row.get("status") or "") != "ready":
                row["status"] = "ready"
                row["uses"] = 0
                reset += 1
        if reset:
            _save(data)
        return reset
