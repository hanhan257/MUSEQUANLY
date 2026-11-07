"""Kho proxy: lưu proxy dán vào, đếm lượt dùng, tự khóa khi đủ lượt hoặc lỗi.

Mỗi proxy là 1 dòng (ip:port hoặc ip:port:user:pass). Tool dùng theo thứ tự
đã dán: proxy nào đủ số lượt (Tài khoản / proxy) thì khóa, proxy nào lỗi
khi chạy thì khóa rồi đổi sang proxy sau.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app_paths import DATA_DIR

PROXIES_FILE = DATA_DIR / "proxies.json"
_io_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict[str, Any]:
    with _io_lock:
        if not PROXIES_FILE.exists():
            return {"proxies": []}
        try:
            data = json.loads(PROXIES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"proxies": []}
        if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
            return {"proxies": []}
        return data


def _save(data: dict[str, Any]) -> None:
    with _io_lock:
        PROXIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _new_id() -> str:
    return "px_" + uuid.uuid4().hex[:10]


def list_proxies() -> list[dict[str, Any]]:
    return [dict(p) for p in _load().get("proxies", [])]


def add_proxies(lines: list[str]) -> dict[str, Any]:
    """Thêm proxy mới (bỏ trùng theo đúng dòng proxy, bỏ dòng trống)."""
    from muse_automation import normalize_proxy_line

    data = _load()
    existing = {(p.get("proxy") or "").strip() for p in data.get("proxies", [])}
    added = 0
    duplicate = 0
    for raw in lines or []:
        line = normalize_proxy_line(raw)
        if not line:
            continue
        if line in existing:
            duplicate += 1
            continue
        data["proxies"].append(
            {
                "id": _new_id(),
                "proxy": line,
                "uses": 0,
                "status": "ready",  # ready | used | error
                "last_error": "",
                "created_at": _now_iso(),
            }
        )
        existing.add(line)
        added += 1
    if added:
        _save(data)
    return {"added": added, "duplicate": duplicate, "total": len(data["proxies"])}


def delete_proxies(ids: list[str]) -> int:
    data = _load()
    want = {str(i) for i in (ids or [])}
    before = len(data["proxies"])
    data["proxies"] = [p for p in data["proxies"] if str(p.get("id")) not in want]
    removed = before - len(data["proxies"])
    if removed:
        _save(data)
    return removed


def reset_proxy(proxy_id: str) -> dict[str, Any] | None:
    """Mở lại 1 proxy đã khóa (lượt dùng về 0, trạng thái sẵn sàng)."""
    data = _load()
    for p in data.get("proxies", []):
        if str(p.get("id")) == str(proxy_id):
            p["uses"] = 0
            p["status"] = "ready"
            p["last_error"] = ""
            _save(data)
            return dict(p)
    return None


def usable_proxies(limit: int) -> list[dict[str, Any]]:
    """Proxy còn dùng được: trạng thái ready và lượt dùng < giới hạn, theo thứ tự đã dán."""
    limit = max(1, int(limit or 1))
    return [
        dict(p)
        for p in _load().get("proxies", [])
        if (p.get("status") or "ready") == "ready" and int(p.get("uses") or 0) < limit
    ]


def bump_proxy_use(proxy_id: str, limit: int) -> dict[str, Any] | None:
    """+1 lượt dùng sau 1 tài khoản tạo thành công. Đủ giới hạn thì khóa (status used)."""
    data = _load()
    for p in data.get("proxies", []):
        if str(p.get("id")) == str(proxy_id):
            p["uses"] = int(p.get("uses") or 0) + 1
            if p["uses"] >= max(1, int(limit or 1)):
                p["status"] = "used"
            _save(data)
            return dict(p)
    return None


def mark_proxy_error(proxy_id: str, error: str = "") -> dict[str, Any] | None:
    """Khóa proxy vì lỗi khi chạy."""
    data = _load()
    for p in data.get("proxies", []):
        if str(p.get("id")) == str(proxy_id):
            p["status"] = "error"
            p["last_error"] = (error or "")[:200]
            _save(data)
            return dict(p)
    return None
