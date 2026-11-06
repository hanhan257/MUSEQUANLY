"""Warehouse (kho) storage — max 50 accounts per kho."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from app_paths import DATA_DIR
from cookie_store import delete_account_cookies_many, get_account_cookies, token_from_cookie_header
from lay2fa_client import parse_account_line

WAREHOUSES_FILE = DATA_DIR / "warehouses.json"
ACCOUNTS_FILE = DATA_DIR / "accounts.txt"
ACCOUNTS_PER_WAREHOUSE = 50
MIN_ACCOUNTS_PER_WAREHOUSE = 1
MAX_ACCOUNTS_PER_WAREHOUSE = 1000


def get_accounts_per_warehouse() -> int:
    """Số tài khoản tối đa mỗi kho — người dùng chỉnh được, mặc định 50."""
    data = ensure_initialized()
    try:
        value = int(data.get("accounts_per_warehouse"))
    except (TypeError, ValueError):
        value = ACCOUNTS_PER_WAREHOUSE
    return max(MIN_ACCOUNTS_PER_WAREHOUSE, min(MAX_ACCOUNTS_PER_WAREHOUSE, value))


def set_accounts_per_warehouse(value: int) -> int:
    data = ensure_initialized()
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = ACCOUNTS_PER_WAREHOUSE
    value = max(MIN_ACCOUNTS_PER_WAREHOUSE, min(MAX_ACCOUNTS_PER_WAREHOUSE, value))
    data["accounts_per_warehouse"] = value
    _save(data)
    return value
_CREATED_LOCK = threading.Lock()


def _default_state() -> dict[str, Any]:
    return {"active_warehouse_id": None, "warehouses": []}


def _load() -> dict[str, Any]:
    if not WAREHOUSES_FILE.exists():
        return _default_state()
    try:
        raw = WAREHOUSES_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("warehouses.json đang hỏng, không ghi đè file kho.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("warehouses.json đang hỏng, không ghi đè file kho.")
    data.setdefault("warehouses", [])
    return data


def _save(data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = WAREHOUSES_FILE.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(WAREHOUSES_FILE)
    _sync_accounts_txt(data)


def _sync_accounts_txt(data: dict[str, Any]) -> None:
    wh = get_warehouse(data.get("active_warehouse_id"), data)
    if not wh:
        ACCOUNTS_FILE.write_text("", encoding="utf-8")
        return
    lines = [_account_line(a) for a in wh.get("accounts", [])]
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


def _account_line(account: dict[str, str]) -> str:
    line = f"{account['email']}|{account.get('lay2fa_url', '')}"
    secret = (account.get("totp_secret") or "").strip()
    if secret:
        line += f"|{secret}"
    return line


def _parse_text(raw: str, proxies_raw: str = "") -> list[dict[str, str]]:
    proxy_lines = (proxies_raw or "").splitlines()
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    index = 0
    for line in raw.splitlines():
        parsed = parse_account_line(line)
        if not parsed:
            continue
        email, lay2fa_url, totp_secret = parsed
        key = email.lower()
        proxy = proxy_lines[index].strip() if index < len(proxy_lines) else ""
        index += 1
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {"email": email, "lay2fa_url": lay2fa_url, "totp_secret": totp_secret, "proxy": proxy}
        )
    return rows


def _accounts_to_warehouses(accounts: list[dict[str, str]], start_name: int = 1) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not accounts:
        out.append({"id": _new_id(), "name": "Kho 1", "accounts": []})
        return out
    chunk_idx = 0
    for i in range(0, len(accounts), get_accounts_per_warehouse()):
        chunk = accounts[i : i + get_accounts_per_warehouse()]
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
    if _detach_created_accounts(data):
        changed = True
    if _pack_created_warehouses(data):
        changed = True
    if _split_created_by_kind(data):
        changed = True
    if changed:
        _save(data)
    return _load()


def _detach_created_accounts(data: dict[str, Any]) -> bool:
    """Mail tạo bằng tool không nằm trong kho thường."""
    created = data.setdefault("created_accounts", [])
    known = {(acc.get("email") or "").strip().lower() for acc in created}
    changed = False
    for wh in data.get("warehouses") or []:
        kept = []
        for acc in wh.get("accounts") or []:
            if acc.get("mailtm_password"):
                key = (acc.get("email") or "").strip().lower()
                if key and key not in known:
                    created.append(acc)
                    known.add(key)
                changed = True
                continue
            kept.append(acc)
        if len(kept) != len(wh.get("accounts") or []):
            wh["accounts"] = kept
    return changed


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
                "max": None if is_error else get_accounts_per_warehouse(),
                "active": wh["id"] == data.get("active_warehouse_id"),
            }
        )
    return {
        "active_warehouse_id": data.get("active_warehouse_id"),
        "max_per_warehouse": get_accounts_per_warehouse(),
        "warehouses": items,
    }


def get_accounts_for_warehouse(warehouse_id: str | None = None) -> list[dict[str, str]]:
    if warehouse_id:
        wh = get_warehouse(warehouse_id)
        return list(wh.get("accounts", [])) if wh else []
    return list(get_active_warehouse().get("accounts", []))


def _mailtm_passwords(data: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for wh in data.get("warehouses") or []:
        for acc in wh.get("accounts") or []:
            key = (acc.get("email") or "").strip().lower()
            password = acc.get("mailtm_password") or ""
            if key and password:
                found[key] = password
    return found


def _keep_mailtm_password(row: dict[str, str], known: dict[str, str]) -> dict[str, str]:
    item = dict(row)
    key = (item.get("email") or "").strip().lower()
    if not item.get("mailtm_password") and known.get(key):
        item["mailtm_password"] = known[key]
    return item


def save_active_accounts(rows: list[dict[str, str]]) -> None:
    data = ensure_initialized()
    wh_id = data.get("active_warehouse_id")
    wh = get_warehouse(wh_id, data)
    if not wh:
        return
    if not _is_error_warehouse(wh) and len(rows) > get_accounts_per_warehouse():
        raise ValueError(f"Mỗi kho tối đa {get_accounts_per_warehouse()} tài khoản.")
    known = _mailtm_passwords(data)
    wh["accounts"] = [_keep_mailtm_password(row, known) for row in rows]
    _save(data)


def import_split_replace(raw: str, proxies_raw: str = "") -> dict[str, Any]:
    """Parse all lines and rebuild warehouses (50 accounts each). Kho lỗi được giữ lại."""
    accounts = _parse_text(raw, proxies_raw)
    if not accounts:
        raise ValueError("Không có tài khoản trong nội dung dán. Kho hiện tại được giữ nguyên.")
    data = ensure_initialized()
    known = _mailtm_passwords(data)
    accounts = [_keep_mailtm_password(row, known) for row in accounts]
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
            if len(accs) < get_accounts_per_warehouse():
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


def _renumber_named_warehouses(warehouses: list[dict[str, Any]]) -> None:
    """Danh lai Kho 1..N theo thu tu cho cac kho ten 'Kho <so>' (bo qua kho ten khac va Kho loi)."""
    number = 0
    for wh in warehouses:
        parts = (wh.get("name") or "").split()
        if len(parts) == 2 and parts[0] == "Kho" and parts[1].isdigit():
            number += 1
            wh["name"] = f"Kho {number}"


def delete_warehouse(warehouse_id: str) -> dict[str, Any] | None:
    """Xoa kho thuong: xoa kho + tai khoan trong kho + cookie cua chung, danh lai Kho 1..N.
    Kho loi khong xoa duoc. Tra None neu khong thay kho."""
    with _CREATED_LOCK:
        data = ensure_initialized()
        target = get_warehouse(warehouse_id, data)
        if target is None or _is_error_warehouse(target):
            return None
        emails = [(acc.get("email") or "") for acc in target.get("accounts") or []]
        name = target.get("name") or "Kho"
        data["warehouses"] = [wh for wh in data.get("warehouses") or [] if wh.get("id") != warehouse_id]
        _renumber_named_warehouses(data.get("warehouses") or [])
        if data.get("active_warehouse_id") == warehouse_id:
            regular = _regular_warehouses(data)
            data["active_warehouse_id"] = regular[0]["id"] if regular else None
        _save(data)
    delete_account_cookies_many(emails)
    return {"deleted": name, "count": len(emails), **list_warehouses_summary()}


def _created_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for wh in data.get("created_warehouses") or []:
        rows.extend(wh.get("accounts") or [])
    rows.extend(data.get("created_accounts") or [])
    return rows


def _account_kind(acc: dict[str, Any]) -> str:
    marked = acc.get("kind")
    if marked in ("plain", "password"):
        return marked
    local = (acc.get("email") or "").split("@", 1)[0]
    password = acc.get("mailtm_password") or ""
    if len(local) == 12 and local.startswith("han") and local[3:].isdigit() and password == "Congphu21@":
        return "password"
    return "plain"


def _append_created_row(data: dict[str, Any], row: dict[str, Any], kind: str = "plain") -> dict[str, Any]:
    kind = "password" if kind == "password" else "plain"
    warehouses = data.setdefault("created_warehouses", [])
    same = [wh for wh in warehouses if wh.get("kind") == kind]
    active_key = "active_created_password_id" if kind == "password" else "active_created_plain_id"
    active = next((wh for wh in same if wh.get("id") == data.get(active_key)), None)
    if active is not None and len(active.get("accounts") or []) < get_accounts_per_warehouse():
        target = active
    else:
        target = next((wh for wh in same if len(wh.get("accounts") or []) < get_accounts_per_warehouse()), None)
    if target is None:
        target = {"id": _new_id(), "name": f"Kho {len(same) + 1}", "kind": kind, "accounts": []}
        warehouses.append(target)
    row["kind"] = kind
    target.setdefault("accounts", []).append(row)
    data[active_key] = target["id"]
    return target


def create_created_warehouse(kind: str = "plain", name: str | None = None) -> dict[str, Any]:
    """Kho trống riêng cho mail không mật khẩu hoặc mail có mật khẩu."""
    kind = "password" if kind == "password" else "plain"
    with _CREATED_LOCK:
        data = ensure_initialized()
        warehouses = data.setdefault("created_warehouses", [])
        same = [wh for wh in warehouses if wh.get("kind") == kind]
        numbers = []
        for wh in same:
            parts = (wh.get("name") or "").split()
            if len(parts) == 2 and parts[0] == "Kho" and parts[1].isdigit():
                numbers.append(int(parts[1]))
        number = (max(numbers) if numbers else len(same)) + 1
        wh = {
            "id": _new_id(),
            "name": (name or "").strip() or f"Kho {number}",
            "kind": kind,
            "accounts": [],
        }
        warehouses.append(wh)
        key = "active_created_password_id" if kind == "password" else "active_created_plain_id"
        data[key] = wh["id"]
        _save(data)
        return wh


def _pack_created_warehouses(data: dict[str, Any]) -> bool:
    """Gom mail đã tạo thành kho riêng, 50 tài khoản một kho."""
    changed = False
    warehouses = data.get("created_warehouses")
    if warehouses is None:
        data["created_warehouses"] = []
        warehouses = data["created_warehouses"]
        changed = True
    known = {
        (acc.get("email") or "").strip().lower()
        for wh in warehouses
        for acc in (wh.get("accounts") or [])
    }
    for acc in list(data.get("created_accounts") or []):
        key = (acc.get("email") or "").strip().lower()
        if not key or key in known:
            continue
        _append_created_row(data, acc, _account_kind(acc))
        known.add(key)
        changed = True
    if data.get("created_accounts"):
        data["created_accounts"] = []
        changed = True
    if not data["created_warehouses"]:
        data["created_warehouses"].append({"id": _new_id(), "name": "Kho 1", "accounts": []})
        changed = True
    if not data.get("active_created_warehouse_id"):
        data["active_created_warehouse_id"] = data["created_warehouses"][0]["id"]
        changed = True
    return changed


def _created_account_view(acc: dict[str, Any]) -> dict[str, Any]:
    email = acc.get("email") or ""
    rec = get_account_cookies(email) or {}
    header = rec.get("cookie_header") or rec.get("muse_cookie_header") or ""
    return {
        "email": email,
        "created_at": acc.get("created_at") or "",
        "password": acc.get("mailtm_password") or "",
        "proxy": acc.get("proxy") or "",
        "invite_code": acc.get("invite_code") or "",
        "redeemed_invite_code": acc.get("redeemed_invite_code") or "",
        "has_cookie": bool(header),
        "cookie_header": header,
        "cookie_saved_at": rec.get("saved_at") or "",
        "muse_token": rec.get("muse_token") or token_from_cookie_header(header),
        "account_status": rec.get("account_status") or "",
        "login_error": rec.get("login_error") or "",
        "token_used_pct": rec.get("token_used_pct") or "",
        "token_left": rec.get("token_left") or "",
        "token_reset": rec.get("token_reset") or "",
        "token_checked_at": rec.get("token_checked_at") or "",
        "token_error": rec.get("token_error") or "",
    }


def find_created_account(email: str) -> dict[str, Any] | None:
    """Mail đã tạo, kèm mật khẩu hộp thư để lần sau vẫn lấy được mã."""
    data = ensure_initialized()
    key = (email or "").strip().lower()
    if not key:
        return None
    for acc in _created_rows(data):
        if (acc.get("email") or "").strip().lower() == key:
            return acc
    return None


def _split_created_by_kind(data: dict[str, Any]) -> bool:
    """Kho có mật khẩu và kho không mật khẩu không dùng chung danh sách."""
    warehouses = data.setdefault("created_warehouses", [])
    mixed = False
    for wh in warehouses:
        accounts = wh.get("accounts") or []
        kinds = {_account_kind(acc) for acc in accounts}
        if wh.get("kind") not in ("plain", "password") or len(kinds) > 1:
            mixed = True
            break
        if accounts and wh.get("kind") not in kinds:
            mixed = True
            break
    changed = False
    if mixed:
        grouped: dict[str, list[dict[str, Any]]] = {"plain": [], "password": []}
        for wh in warehouses:
            for acc in wh.get("accounts") or []:
                kind = _account_kind(acc)
                acc["kind"] = kind
                grouped[kind].append(acc)

        def _chunks(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
            if not rows:
                return [{"id": _new_id(), "name": "Kho 1", "kind": kind, "accounts": []}]
            out = []
            for index in range(0, len(rows), get_accounts_per_warehouse()):
                number = index // get_accounts_per_warehouse() + 1
                out.append(
                    {
                        "id": _new_id(),
                        "name": f"Kho {number}",
                        "kind": kind,
                        "accounts": rows[index : index + get_accounts_per_warehouse()],
                    }
                )
            return out

        data["created_warehouses"] = _chunks(grouped["plain"], "plain") + _chunks(grouped["password"], "password")
        warehouses = data["created_warehouses"]
        changed = True
    for kind in ("plain", "password"):
        if not any(wh.get("kind") == kind for wh in warehouses):
            warehouses.append({"id": _new_id(), "name": "Kho 1", "kind": kind, "accounts": []})
            changed = True
    for kind, key in (("plain", "active_created_plain_id"), ("password", "active_created_password_id")):
        group = [wh for wh in warehouses if wh.get("kind") == kind]
        if data.get(key) not in {wh.get("id") for wh in group}:
            data[key] = group[0]["id"]
            changed = True
    return changed


def list_created_accounts(warehouse_id: str | None = None, kind: str = "plain") -> dict[str, Any]:
    data = ensure_initialized()
    kind = "password" if kind == "password" else "plain"
    warehouses = [wh for wh in data.get("created_warehouses") or [] if wh.get("kind") == kind]
    active_key = "active_created_password_id" if kind == "password" else "active_created_plain_id"
    active_id = warehouse_id or data.get(active_key)
    current = next((wh for wh in warehouses if wh.get("id") == active_id), None)
    if current is None and warehouses:
        current = warehouses[0]
    summary = [
        {
            "id": wh.get("id"),
            "name": wh.get("name") or "Kho",
            "count": len(wh.get("accounts") or []),
            "max": get_accounts_per_warehouse(),
            "kind": kind,
            "active": current is not None and wh.get("id") == current.get("id"),
        }
        for wh in warehouses
    ]
    accounts = [_created_account_view(acc) for acc in (current.get("accounts") or [])] if current else []
    return {
        "active_id": current.get("id") if current else None,
        "kind": kind,
        "warehouses": summary,
        "accounts": accounts,
    }


def set_active_created_warehouse(warehouse_id: str) -> bool:
    data = ensure_initialized()
    match = next((wh for wh in data.get("created_warehouses") or [] if wh.get("id") == warehouse_id), None)
    if not match:
        return False
    key = "active_created_password_id" if match.get("kind") == "password" else "active_created_plain_id"
    data[key] = warehouse_id
    _save(data)
    return True


def delete_created_warehouse(warehouse_id: str) -> dict[str, Any] | None:
    """Xoa kho da tao: xoa kho + tai khoan trong kho + cookie cua chung, danh lai Kho 1..N trong cung loai (plain/password).
    Tra None neu khong thay kho."""
    with _CREATED_LOCK:
        data = ensure_initialized()
        target = next((wh for wh in data.get("created_warehouses") or [] if wh.get("id") == warehouse_id), None)
        if target is None:
            return None
        kind = target.get("kind") or "plain"
        emails = [(acc.get("email") or "") for acc in target.get("accounts") or []]
        name = target.get("name") or "Kho"
        data["created_warehouses"] = [wh for wh in data.get("created_warehouses") or [] if wh.get("id") != warehouse_id]
        _renumber_named_warehouses([wh for wh in data.get("created_warehouses") or [] if (wh.get("kind") or "plain") == kind])
        for key in ("active_created_warehouse_id", "active_created_plain_id", "active_created_password_id"):
            if data.get(key) == warehouse_id:
                same = [wh for wh in data.get("created_warehouses") or [] if (wh.get("kind") or "plain") == kind]
                data[key] = same[0]["id"] if same else None
        _save(data)
    delete_account_cookies_many(emails)
    return {"deleted": name, "kind": kind, "count": len(emails)}


def add_created_account(email: str, *, mailtm_password: str = "", kind: str = "plain", proxy: str = "") -> dict[str, Any]:
    """Lưu mail vào kho tạo riêng. Đủ 50 thì sang kho tiếp theo."""
    from datetime import datetime

    with _CREATED_LOCK:
        data = ensure_initialized()
        key = email.strip().lower()
        for acc in _created_rows(data):
            if (acc.get("email") or "").strip().lower() == key:
                changed = False
                if mailtm_password and not acc.get("mailtm_password"):
                    acc["mailtm_password"] = mailtm_password
                    changed = True
                if proxy and not acc.get("proxy"):
                    acc["proxy"] = proxy
                    changed = True
                if changed:
                    _save(data)
                return {"email": acc.get("email") or email.strip(), "already": True}
        kind = "password" if kind == "password" else "plain"
        row = {
            "email": email.strip(),
            "mailtm_password": mailtm_password,
            "created_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "kind": kind,
            "proxy": (proxy or "").strip(),
        }
        target = _append_created_row(data, row, kind)
        _save(data)
        return {
            "email": row["email"],
            "already": False,
            "warehouse_id": target.get("id"),
            "warehouse_name": target.get("name") or "Kho",
        }


def set_created_invite_code(email: str, code: str) -> None:
    """Ghi mã lời mời lên dòng mail vừa tạo."""
    code = (code or "").strip().upper()
    if not code:
        return
    with _CREATED_LOCK:
        data = ensure_initialized()
        key = email.strip().lower()
        for acc in _created_rows(data):
            if (acc.get("email") or "").strip().lower() == key:
                acc["invite_code"] = code
                _save(data)
                return


def set_redeemed_invite_code(email: str, code: str) -> None:
    """Ghi mã lời mời tài khoản này đã nhập (mã của tài khoản trước nó)."""
    code = (code or "").strip().upper()
    if not code:
        return
    with _CREATED_LOCK:
        data = ensure_initialized()
        key = email.strip().lower()
        for acc in _created_rows(data):
            if (acc.get("email") or "").strip().lower() == key:
                acc["redeemed_invite_code"] = code
                _save(data)
                return


def delete_created_account(email: str) -> bool:
    """Xóa dòng tài khoản đã tạo khỏi kho (kèm cookie của nó). Tài khoản tạo lỗi thì bỏ qua, không lưu vào kho."""
    key = (email or "").strip().lower()
    if not key:
        return False
    with _CREATED_LOCK:
        data = ensure_initialized()
        removed = False
        for wh in data.get("created_warehouses") or []:
            before = len(wh.get("accounts") or [])
            wh["accounts"] = [acc for acc in (wh.get("accounts") or []) if (acc.get("email") or "").strip().lower() != key]
            removed = removed or len(wh["accounts"]) != before
        before = len(data.get("created_accounts") or [])
        data["created_accounts"] = [acc for acc in (data.get("created_accounts") or []) if (acc.get("email") or "").strip().lower() != key]
        removed = removed or len(data["created_accounts"]) != before
        if removed:
            _save(data)
    if removed:
        delete_account_cookies_many([key])
    return removed


def latest_invite_code() -> str:
    """Mã của tài khoản cuối chuỗi đã tạo xong."""
    data = ensure_initialized()
    return (data.get("invite_chain_code") or "").strip().upper()


def warehouse_invite_code(exclude_email: str = "", *, kind: str | None = None, exclude_codes: tuple[str, ...] = ()) -> str:
    """Lấy 1 mã mời trong kho đã tạo (mã mới nhất, khác tài khoản hiện tại và khác các mã đã thử).
    Dùng làm phương án dự phòng khi không lấy được mã của tài khoản trước, để tài khoản nào cũng có mã nhập."""
    data = ensure_initialized()
    exclude = (exclude_email or "").strip().lower()
    skip = {(code or "").strip().upper() for code in exclude_codes}
    for acc in reversed(_created_rows(data)):
        if kind is not None and _account_kind(acc) != kind:
            continue
        if (acc.get("email") or "").strip().lower() == exclude:
            continue
        code = (acc.get("invite_code") or "").strip().upper()
        if code and code not in skip:
            return code
    return ""


def set_invite_chain_code(code: str) -> None:
    code = (code or "").strip().upper()
    if not code:
        return
    with _CREATED_LOCK:
        data = ensure_initialized()
        data["invite_chain_code"] = code
        _save(data)


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
        "totp_secret": acc.get("totp_secret", ""),
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
    if not target or _is_error_warehouse(target) or len(target.get("accounts") or []) >= get_accounts_per_warehouse():
        target = next(
            (
                wh
                for wh in _regular_warehouses(data)
                if len(wh.get("accounts") or []) < get_accounts_per_warehouse()
            ),
            None,
        )
    if not target:
        n = len(_regular_warehouses(data)) + 1
        target = {"id": _new_id(), "name": f"Kho {n}", "accounts": []}
        data["warehouses"].insert(data["warehouses"].index(error), target)
    target.setdefault("accounts", []).append(
        {
            "email": acc["email"],
            "lay2fa_url": acc.get("lay2fa_url", ""),
            "totp_secret": acc.get("totp_secret", ""),
            "proxy": acc.get("proxy", ""),
        }
    )
    _save(data)
    return {"email": acc["email"], "warehouse_id": target["id"], "warehouse_name": target.get("name")}


def get_proxy_for_email(email: str) -> str:
    data = ensure_initialized()
    found = _find_account(data, email)
    if not found:
        return ""
    return (found[1].get("proxy") or "").strip()


def get_account_password_for_email(email: str) -> str:
    data = ensure_initialized()
    found = _find_account(data, email)
    if not found:
        return ""
    raw = (found[1].get("lay2fa_url") or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower.startswith(("http://", "https://")) or "://" in lower or lower.startswith("lay2fa"):
        return ""
    return raw


def get_totp_secret_for_email(email: str) -> str:
    data = ensure_initialized()
    found = _find_account(data, email)
    if not found:
        return ""
    return (found[1].get("totp_secret") or "").strip()


def warehouse_emails(warehouse_id: str | None = None) -> list[str]:
    return [a["email"] for a in get_accounts_for_warehouse(warehouse_id)]


def _card_digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


CARD_USE_LIMIT = 10


def _normalize_expiry(value: str) -> str:
    """Nhan MM/YY (11/29, 1129) hoac MM/YYYY (11/2029, 112029): nam 4 so tu cat ve 2 so."""
    digits = _card_digits(value)
    if len(digits) == 4:
        year2 = digits[2:]
    elif len(digits) == 6:
        year2 = digits[4:]
    else:
        raise ValueError("Ngày thẻ phải là MM/YY hoặc MM/YYYY")
    month = int(digits[:2])
    if month < 1 or month > 12:
        raise ValueError("Ngày thẻ phải là MM/YY")
    return f"{digits[:2]}/{year2}"


def list_card_warehouses() -> dict[str, Any]:
    data = ensure_initialized()
    rows = []
    for wh in data.get("card_warehouses") or []:
        card = wh.get("card") or {}
        rows.append(
            {
                "id": wh.get("id"),
                "name": wh.get("name") or "Kho",
                "number": card.get("number") or "",
                "expiry": card.get("expiry") or "",
                "cvv": card.get("cvv") or "",
                "status": wh.get("status") or "",
                "uses": int(wh.get("uses") or 0),
                "use_limit": CARD_USE_LIMIT,
            }
        )
    return {"warehouses": rows}


def usable_card_warehouses() -> list[dict[str, Any]]:
    return [
        row
        for row in list_card_warehouses()["warehouses"]
        if row.get("number") and row.get("status") not in ("error", "used") and row.get("uses", 0) < CARD_USE_LIMIT
    ]


def add_card_warehouse(number: str, expiry: str, cvv: str) -> dict[str, Any]:
    """Mỗi kho chỉ giữ một thẻ do người dùng tự nhập."""
    pan = _card_digits(number)
    code = _card_digits(cvv)
    if len(pan) < 13 or len(pan) > 19:
        raise ValueError("Số thẻ chưa đúng")
    if len(code) not in (3, 4):
        raise ValueError("CVV phải có 3 hoặc 4 số")
    exp = _normalize_expiry(expiry)
    with _CREATED_LOCK:
        data = ensure_initialized()
        warehouses = data.setdefault("card_warehouses", [])
        numbers = []
        for wh in warehouses:
            parts = (wh.get("name") or "").split()
            if len(parts) == 2 and parts[0] == "Kho" and parts[1].isdigit():
                numbers.append(int(parts[1]))
        name = f"Kho {(max(numbers) if numbers else 0) + 1}"
        wh = {
            "id": _new_id(),
            "name": name,
            "status": "",
            "uses": 0,
            "card": {"number": pan, "expiry": exp, "cvv": code},
        }
        warehouses.append(wh)
        _save(data)
        return {"id": wh["id"], "name": name, "number": pan, "expiry": exp, "cvv": code, "status": ""}


def mark_card_warehouse_bad(warehouse_id: str) -> None:
    with _CREATED_LOCK:
        data = ensure_initialized()
        for wh in data.get("card_warehouses") or []:
            if wh.get("id") == warehouse_id:
                wh["status"] = "error"
                _save(data)
                return


def bump_card_warehouse_use(warehouse_id: str, limit: int = CARD_USE_LIMIT) -> dict[str, Any] | None:
    """Ghi nhan 1 lan dung the thanh cong. Du limit luot thi khoa kho (status 'used') de tool doi sang the khac."""
    with _CREATED_LOCK:
        data = ensure_initialized()
        for wh in data.get("card_warehouses") or []:
            if wh.get("id") == warehouse_id:
                uses = int(wh.get("uses") or 0) + 1
                wh["uses"] = uses
                if uses >= limit:
                    wh["status"] = "used"
                _save(data)
                card = wh.get("card") or {}
                return {
                    "id": wh.get("id"),
                    "name": wh.get("name") or "Kho",
                    "number": card.get("number") or "",
                    "status": wh.get("status") or "",
                    "uses": uses,
                    "use_limit": limit,
                }
        return None


def delete_card_warehouse(warehouse_id: str) -> bool:
    with _CREATED_LOCK:
        data = ensure_initialized()
        rows = data.get("card_warehouses") or []
        kept = [wh for wh in rows if wh.get("id") != warehouse_id]
        if len(kept) == len(rows):
            return False
        data["card_warehouses"] = kept
        _save(data)
        return True


def delete_card_warehouses(ids: list[str]) -> int:
    """Xóa nhiều kho thẻ một lúc (ví dụ: lọc thẻ lỗi rồi xóa hết). Trả số kho đã xóa."""
    wanted = {(item or "").strip() for item in ids or [] if (item or "").strip()}
    if not wanted:
        return 0
    with _CREATED_LOCK:
        data = ensure_initialized()
        rows = data.get("card_warehouses") or []
        kept = [wh for wh in rows if (wh.get("id") or "") not in wanted]
        removed = len(rows) - len(kept)
        if removed:
            data["card_warehouses"] = kept
            _save(data)
        return removed
