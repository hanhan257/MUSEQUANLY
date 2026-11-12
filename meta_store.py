"""Kho tài khoản Meta (META-MUSE): các TK Meta tạo bằng mail temp, dùng để
đăng nhập Muse sau.

Luồng 2 bước: bước 1 tạo TK Meta bằng IP của máy này (KHÔNG dùng proxy US —
IP Việt Nam thường không bị hỏi captcha) qua auth.meta.com, lưu vào kho này;
bước 2 dùng proxy US đăng nhập muse.ai bằng TK Meta đã lưu (xem
login_muse_with_meta trong muse_automation.py).

Mỗi TK Meta gồm email + mật khẩu cố định Congphu21@ (ACCOUNT_PASSWORD chung
của tool) + tên người dùng Meta tự sinh. Mật khẩu là hằng chung của tool
(không phải bí mật riêng của từng TK), nhưng file này vẫn nằm trong
.gitignore và KHÔNG BAO GIỜ được commit/push lên git. Các hàm list không
bao giờ trả mật khẩu ra ngoài (API/UI/log).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from typing import Any

from app_paths import DATA_DIR

META_FILE = DATA_DIR / "meta_accounts.json"
_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _load() -> dict[str, Any]:
    if not META_FILE.exists():
        return {"accounts": []}
    try:
        raw = META_FILE.read_text(encoding="utf-8")
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
    tmp = META_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(META_FILE)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    # Khong bao gio tra mat khau ra ngoai (API/UI/log).
    return {
        "id": (row.get("email") or "").strip().lower(),
        "email": (row.get("email") or "").strip(),
        "meta_username": (row.get("meta_username") or "").strip(),
        "status": (row.get("status") or "ready").strip() or "ready",
        "uses": int(row.get("uses") or 0),
    }


def list_meta_accounts() -> list[dict[str, Any]]:
    with _LOCK:
        return [_public(r) for r in _load().get("accounts", [])]


def meta_summary() -> dict[str, Any]:
    rows = list_meta_accounts()
    return {
        "meta": rows,
        "total": len(rows),
        "ready": sum(1 for r in rows if r["status"] == "ready"),
        "used": sum(1 for r in rows if r["status"] != "ready"),
    }


def get_meta_password(email: str) -> str:
    """Mat khau cua 1 TK Meta trong kho (chi dung noi bo luc dang nhap Muse).
    Tra '' neu khong co trong kho."""
    key = (email or "").strip().lower()
    if not key:
        return ""
    with _LOCK:
        for row in _load().get("accounts", []):
            if (row.get("email") or "").strip().lower() == key:
                return (row.get("password") or "").strip()
    return ""


def add_meta_account(email: str, password: str, meta_username: str = "") -> bool:
    """Luu 1 TK Meta vua tao xong vao kho. Trung email thi bo qua.
    Tra True neu them moi, False neu da co."""
    email = (email or "").strip()
    if not _EMAIL_RE.match(email):
        return False
    key = email.lower()
    with _LOCK:
        data = _load()
        rows = data.get("accounts", [])
        if any((r.get("email") or "").strip().lower() == key for r in rows):
            return False
        rows.append(
            {
                "email": email,
                "password": (password or "").strip(),
                "meta_username": (meta_username or "").strip(),
                "status": "ready",
                "uses": 0,
                "added": datetime.now().strftime("%d/%m/%Y %H:%M"),
            }
        )
        data["accounts"] = rows
        _save(data)
    return True


def take_meta_account() -> dict[str, Any] | None:
    """Lay TK Meta san sang dau tien, danh dau da dung (1 TK Meta = 1 lan
    dang nhap Muse). Tra ve dict co email + password + meta_username,
    hoac None neu kho het."""
    with _LOCK:
        data = _load()
        for row in data.get("accounts", []):
            if (row.get("status") or "ready") == "ready":
                row["status"] = "used"
                row["uses"] = int(row.get("uses") or 0) + 1
                _save(data)
                return {
                    "email": row.get("email") or "",
                    "password": row.get("password") or "",
                    "meta_username": row.get("meta_username") or "",
                }
    return None


def delete_meta_accounts(ids: list[str]) -> int:
    keys = {(i or "").strip().lower() for i in (ids or []) if (i or "").strip()}
    if not keys:
        return 0
    with _LOCK:
        data = _load()
        before = len(data.get("accounts", []))
        data["accounts"] = [
            r for r in data.get("accounts", []) if (r.get("email") or "").strip().lower() not in keys
        ]
        removed = before - len(data.get("accounts"))
        if removed:
            _save(data)
        return removed


def reset_meta_accounts(ids: list[str]) -> int:
    """Mo lai TK Meta da dung ve san sang (dung khi lan dang nhap truoc loi
    giua chung va TK Meta van dung duoc)."""
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
