"""Warehouse (kho) storage — max 50 accounts per kho."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from lay2fa_client import parse_account_line

ROOT = Path(__file__).resolve().parent
WAREHOUSES_FILE = ROOT / "warehouses.json"
ACCOUNTS_FILE = ROOT / "accounts.txt"
ACCOUNTS_PER_WAREHOUSE = 50


def _default_state() -> dict[str, Any]:
    return {"active_warehouse_id": None, "warehouses": []}


def _load() -> dict[str, Any]:
    if not WAREHOUSES_FILE.exists():
        return _default_state()
    try:
        data = json.loads(WAREHOUSES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        data.setdefault("warehouses", [])
        return data
    except (json.JSONDecodeError, OSError):
        return _default_state()


def _save(data: dict[str, Any]) -> None:
    WAREHOUSES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _sync_accounts_txt(data)


def _sync_accounts_txt(data: dict[str, Any]) -> None:
    wh = get_warehouse(data.get("active_warehouse_id"), data)
    if not wh:
        ACCOUNTS_FILE.write_text("", encoding="utf-8")
        return
    lines = [f"{a['email']}|{a['lay2fa_url']}" for a in wh.get("accounts", [])]
    ACCOUNTS_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def migrate_from_legacy_accounts_txt() -> None:
    data = _load()
    if data.get("warehouses"):
        return
    if not ACCOUNTS_FILE.exists():
        wh_id = _new_id()
        data["warehouses"] = [
            {"id": wh_id, "name": "Kho 1", "accounts": []},
        ]
        data["active_warehouse_id"] = wh_id
        _save(data)
        return
    accounts = _parse_text(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    data["warehouses"] = _accounts_to_warehouses(accounts, start_name=1)
    data["active_warehouse_id"] = data["warehouses"][0]["id"] if data["warehouses"] else None
    _save(data)


def _parse_text(raw: str, proxies_raw: str = "") -> list[dict[str, str]]:
    proxy_lines = (proxies_raw or "").splitlines()
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    index = 0
    for line in raw.splitlines():
        parsed = parse_account_line(line)
        if not parsed:
            continue
        email, lay2fa_url = parsed
        key = email.lower()
        proxy = proxy_lines[index].strip() if index < len(proxy_lines) else ""
        index += 1
        if key in seen:
            continue
        seen.add(key)
        rows.append({"email": email, "lay2fa_url": lay2fa_url, "proxy": proxy})
    return rows


def _accounts_to_warehouses(accounts: list[dict[str, str]], start_name: int = 1) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not accounts:
        out.append({"id": _new_id(), "name": "Kho 1", "accounts": []})
        return out
    chunk_idx = 0
    for i in range(0, len(accounts), ACCOUNTS_PER_WAREHOUSE):
        chunk = accounts[i : i + ACCOUNTS_PER_WAREHOUSE]
        chunk_idx += 1
        out.append(
            {
                "id": _new_id(),
                "name": f"Kho {start_name + chunk_idx - 1}",
                "accounts": chunk,
            }
        )
    return out


def _is_error_warehouse(wh: dict[str, Any] | None) -> bool:
    return bool(wh) and wh.get("kind") == "error"


def _regular_warehouses(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [wh for wh in data.get("warehouses", []) if not _is_error_warehouse(wh)]


def _find_error_warehouse(data: dict[str, Any]) -> dict[str, Any] | None:
    for wh in data.get("warehouses", []):
        if _is_error_warehouse(wh):
            return wh
    return None


def _ensure_error_warehouse(data: dict[str, Any]) -> dict[str, Any]:
    wh = _find_error_warehouse(data)
    if wh:
        wh.setdefault("name", "Kho lỗi")
        wh.setdefault("accounts", [])
        return wh
    wh = {"id": _new_id(), "name": "Kho lỗi", "kind": "error", "accounts": []}
    data.setdefault("warehouses", []).append(wh)
    return wh


def ensure_initialized() -> dict[str, Any]:
    migrate_from_legacy_accounts_txt()
    data = _load()
    changed = False
    if not _regular_warehouses(data):
        wh_id = _new_id()
        data.setdefault("warehouses", []).insert(0, {"id": wh_id, "name": "Kho 1", "accounts": []})
        data["active_warehouse_id"] = wh_id
        changed = True
    if not _find_error_warehouse(data):
        _ensure_error_warehouse(data)
        changed = True
    if not data.get("active_warehouse_id"):
        data["active_warehouse_id"] = _regular_warehouses(data)[0]["id"]
        changed = True
    if changed:
        _save(data)
    return _load()


def get_warehouse(warehouse_id: str | None, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    data = data or ensure_initialized()
    if not warehouse_id:
        return None
    for wh in data.get("warehouses", []):
        if wh.get("id") == warehouse_id:
            return wh
    return None


def get_active_warehouse() -> dict[str, Any]:
    data = ensure_initialized()
    wh = get_warehouse(data.get("active_warehouse_id"), data)
    if wh:
        return wh
    if data["warehouses"]:
        data["active_warehouse_id"] = data["warehouses"][0]["id"]
        _save(data)
        return data["warehouses"][0]
    wh_id = _new_id()
    empty = {"id": wh_id, "name": "Kho 1", "accounts": []}
    data["warehouses"] = [empty]
    data["active_warehouse_id"] = wh_id
    _save(data)
    return empty


def set_active_warehouse(warehouse_id: str) -> dict[str, Any] | None:
    data = ensure_initialized()
    if not get_warehouse(warehouse_id, data):
        return None
    data["active_warehouse_id"] = warehouse_id
    _save(data)
    return get_warehouse(warehouse_id, data)


def list_warehouses_summary() -> dict[str, Any]:
    data = ensure_initialized()
    ordered = _regular_warehouses(data)
    error = _find_error_warehouse(data)
    if error:
        ordered = ordered + [error]
    items = []
    for wh in ordered:
        acc = wh.get("accounts") or []
        is_error = _is_error_warehouse(wh)
        items.append(
            {
                "id": wh["id"],
                "name": wh.get("name", "Kho"),
                "kind": "error" if is_error else "normal",
                "count": len(acc),
                "max": None if is_error else ACCOUNTS_PER_WAREHOUSE,
                "active": wh["id"] == data.get("active_warehouse_id"),
            }
        )
    return {
        "active_warehouse_id": data.get("active_warehouse_id"),
        "max_per_warehouse": ACCOUNTS_PER_WAREHOUSE,
        "warehouses": items,
    }


def get_accounts_for_warehouse(warehouse_id: str | None = None) -> list[dict[str, str]]:
    if warehouse_id:
        wh = get_warehouse(warehouse_id)
        return list(wh.get("accounts", [])) if wh else []
    return list(get_active_warehouse().get("accounts", []))


def save_active_accounts(rows: list[dict[str, str]]) -> None:
    data = ensure_initialized()
    wh_id = data.get("active_warehouse_id")
    wh = get_warehouse(wh_id, data)
    if not wh:
        return
    if not _is_error_warehouse(wh) and len(rows) > ACCOUNTS_PER_WAREHOUSE:
        raise ValueError(f"Mỗi kho tối đa {ACCOUNTS_PER_WAREHOUSE} tài khoản.")
    wh["accounts"] = rows
    _save(data)


def import_split_replace(raw: str, proxies_raw: str = "") -> dict[str, Any]:
    """Parse all lines and rebuild warehouses (50 accounts each). Kho lỗi được giữ lại."""
    accounts = _parse_text(raw, proxies_raw)
    data = ensure_initialized()
    error = _ensure_error_warehouse(data)
    pasted = {a["email"].lower() for a in accounts}
    error["accounts"] = [
        a for a in (error.get("accounts") or []) if a.get("email", "").lower() not in pasted
    ]
    data["warehouses"] = _accounts_to_warehouses(accounts)
    data["warehouses"].append(error)
    data["active_warehouse_id"] = data["warehouses"][0]["id"] if data["warehouses"] else error["id"]
    _save(data)
    return list_warehouses_summary()


def import_split_append(raw: str, proxies_raw: str = "") -> dict[str, Any]:
    """Append accounts into warehouses, creating new kho when full."""
    new_accounts = _parse_text(raw, proxies_raw)
    data = ensure_initialized()
    _ensure_error_warehouse(data)
    warehouses: list[dict[str, Any]] = data.get("warehouses") or []
    if not _regular_warehouses(data):
        warehouses.insert(0, {"id": _new_id(), "name": "Kho 1", "accounts": []})

    def _already(email: str) -> bool:
        key = email.lower()
        for wh in warehouses:
            if any(a.get("email", "").lower() == key for a in wh.get("accounts") or []):
                return True
        return False

    for acc in new_accounts:
        if _already(acc["email"]):
            if acc.get("proxy"):
                for wh in warehouses:
                    for old in wh.get("accounts") or []:
                        if old.get("email", "").lower() == acc["email"].lower():
                            old["proxy"] = acc["proxy"]
            continue
        placed = False
        for wh in warehouses:
            if _is_error_warehouse(wh):
                continue
            accs = wh.setdefault("accounts", [])
            if len(accs) < ACCOUNTS_PER_WAREHOUSE:
                accs.append(acc)
                placed = True
                break
        if not placed:
            n = len(_regular_warehouses({"warehouses": warehouses})) + 1
            error = _find_error_warehouse({"warehouses": warehouses})
            insert_at = warehouses.index(error) if error in warehouses else len(warehouses)
            warehouses.insert(insert_at, {"id": _new_id(), "name": f"Kho {n}", "accounts": [acc]})

    data["warehouses"] = warehouses
    if not data.get("active_warehouse_id"):
        data["active_warehouse_id"] = warehouses[0]["id"]
    _save(data)
    return list_warehouses_summary()


def rename_warehouse(warehouse_id: str, name: str) -> bool:
    data = ensure_initialized()
    wh = get_warehouse(warehouse_id, data)
    if not wh:
        return False
    wh["name"] = name.strip() or wh.get("name", "Kho")
    _save(data)
    return True


def create_warehouse(name: str | None = None) -> dict[str, Any]:
    data = ensure_initialized()
    n = len(_regular_warehouses(data)) + 1
    wh = {"id": _new_id(), "name": name or f"Kho {n}", "accounts": []}
    error = _find_error_warehouse(data)
    if error:
        data["warehouses"].insert(data["warehouses"].index(error), wh)
    else:
        data.setdefault("warehouses", []).append(wh)
    _save(data)
    return wh


def _find_account(data: dict[str, Any], email: str) -> tuple[dict[str, Any], dict[str, str]] | None:
    key = email.strip().lower()
    for wh in data.get("warehouses", []):
        for acc in wh.get("accounts") or []:
            if acc.get("email", "").lower() == key:
                return wh, acc
    return None


def move_account_to_error(email: str) -> dict[str, Any]:
    """Move one account out of its current kho into Kho lỗi."""
    data = ensure_initialized()
    found = _find_account(data, email)
    if not found:
        raise ValueError("Không tìm thấy tài khoản.")
    source, acc = found
    error = _ensure_error_warehouse(data)
    if source.get("id") == error.get("id"):
        return {"email": acc["email"], "warehouse_id": error["id"], "already": True}
    source["accounts"] = [a for a in source.get("accounts") or [] if a.get("email", "").lower() != acc["email"].lower()]
    moved = {
        "email": acc["email"],
        "lay2fa_url": acc.get("lay2fa_url", ""),
        "proxy": acc.get("proxy", ""),
        "from_warehouse_id": source.get("id"),
        "from_warehouse_name": source.get("name") or "Kho",
    }
    error.setdefault("accounts", []).append(moved)
    _save(data)
    return {"email": moved["email"], "warehouse_id": error["id"], "from": moved["from_warehouse_name"]}


def restore_account_from_error(email: str) -> dict[str, Any]:
    """Move an account from Kho lỗi back to the kho it came from."""
    data = ensure_initialized()
    error = _ensure_error_warehouse(data)
    key = email.strip().lower()
    acc = next((a for a in error.get("accounts") or [] if a.get("email", "").lower() == key), None)
    if not acc:
        raise ValueError("Tài khoản không nằm trong kho lỗi.")
    error["accounts"] = [a for a in error.get("accounts") or [] if a.get("email", "").lower() != key]
    target = get_warehouse(acc.get("from_warehouse_id"), data)
    if not target or _is_error_warehouse(target) or len(target.get("accounts") or []) >= ACCOUNTS_PER_WAREHOUSE:
        target = next(
            (
                wh
                for wh in _regular_warehouses(data)
                if len(wh.get("accounts") or []) < ACCOUNTS_PER_WAREHOUSE
            ),
            None,
        )
    if not target:
        n = len(_regular_warehouses(data)) + 1
        target = {"id": _new_id(), "name": f"Kho {n}", "accounts": []}
        data["warehouses"].insert(data["warehouses"].index(error), target)
    target.setdefault("accounts", []).append(
        {"email": acc["email"], "lay2fa_url": acc.get("lay2fa_url", ""), "proxy": acc.get("proxy", "")}
    )
    _save(data)
    return {"email": acc["email"], "warehouse_id": target["id"], "warehouse_name": target.get("name")}


def get_proxy_for_email(email: str) -> str:
    data = ensure_initialized()
    found = _find_account(data, email)
    if not found:
        return ""
    return (found[1].get("proxy") or "").strip()


def warehouse_emails(warehouse_id: str | None = None) -> list[str]:
    return [a["email"] for a in get_accounts_for_warehouse(warehouse_id)]
