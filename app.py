"""Muse AI account manager — local web UI."""

from __future__ import annotations

import re
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from app_paths import BUNDLE_DIR, DATA_DIR, is_frozen
from cookie_store import (
    export_bulk,
    export_clipboard_text,
    export_filtered_text,
    export_for_email,
    get_account_cookies,
    list_all_cookies,
    normalize_email_key,
    token_from_cookie_header,
)
from lay2fa_client import get_latest_muse_code, parse_account_line
from muse_automation import (
    LoginProgress,
    check_token_usage,
    clear_stop,
    create_meta_account,
    create_muse_account,
    login_muse,
    login_muse_with_meta,
    normalize_browser,
    normalize_proxy_line,
    redeem_invite_for_account,
    request_stop,
    set_signup_turn_gap,
    stop_requested,
)
from proxy_store import (
    add_proxies,
    bump_proxy_use,
    delete_proxies,
    list_proxies,
    mark_proxy_error,
    reset_proxies,
    reset_proxy,
    usable_proxies,
)
from warehouse_store import (
    add_card_warehouse,
    add_card_warehouses_bulk,
    add_created_account,
    create_created_warehouse,
    delete_card_warehouse,
    delete_card_warehouses,
    delete_created_account,
    delete_created_warehouse,
    delete_warehouse,
    list_all_created_accounts,
    list_all_regular_accounts,
    list_card_warehouses,
    list_created_accounts,
    set_active_created_warehouse,
    create_warehouse,
    ensure_initialized,
    export_created_login_data,
    export_login_data,
    find_created_account,
    get_account_password_for_email,
    get_accounts_for_warehouse,
    get_accounts_per_warehouse,
    get_mailtm_password_for_email,
    get_active_warehouse,
    get_proxy_for_email,
    get_totp_secret_for_email,
    get_warehouse,
    import_split_append,
    import_split_replace,
    latest_invite_code,
    list_warehouses_summary,
    move_account_to_error,
    rename_warehouse,
    restore_account_from_error,
    save_active_accounts,
    usable_card_warehouses,
    warehouse_invite_code,
    set_accounts_per_warehouse,
    set_active_warehouse,
    set_created_invite_code,
    set_invite_chain_code,
    set_redeemed_invite_code,
    warehouse_emails,
)

ACCOUNTS_FILE = DATA_DIR / "accounts.txt"
MUSE_LOGIN_URL = "https://muse.ai/"
APP_BUILD = "signup-v149"
SIGNUP_COUNTS = (1, 5, 10, 50)
ACCOUNT_PASSWORD = "Congphu21@"
SIGNUP_MAX = 1000
BUFF_MAX_PER_CODE = 30  # 1 mã mời tối đa 30 lượt dùng (giới hạn của Muse)
BUFF_MAX_CODES = 20
BATCH_PARALLEL = 5
PROXY_PARALLEL = 5
MAX_WORKERS = 20

ensure_initialized()

app = Flask(
    __name__,
    template_folder=str(BUNDLE_DIR / "templates") if (BUNDLE_DIR / "templates").is_dir() else "templates",
)


@app.after_request
def _api_no_store(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

_login_jobs: dict[str, dict] = {}
_batch_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_batch_running = threading.Event()


def _enrich_accounts(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        email = row["email"]
        cookie_rec = get_account_cookies(email)
        out.append(
            {
                "email": email,
                "lay2fa_url": row["lay2fa_url"],
                "totp_secret": row.get("totp_secret") or "",
                "proxy": row.get("proxy") or "",
                "has_cookie": bool(cookie_rec and cookie_rec.get("cookie_header")),
                "cookie_saved_at": (cookie_rec or {}).get("saved_at"),
                "cookie_count": (cookie_rec or {}).get("count", 0),
            }
        )
    return out


def load_accounts(warehouse_id: str | None = None) -> list[dict[str, str]]:
    rows = get_accounts_for_warehouse(warehouse_id)
    return _enrich_accounts(rows)


def _resolve_warehouse_id(param: str | None) -> str | None:
    if param:
        return param
    return get_active_warehouse().get("id")


def _scope_emails(warehouse_id: str | None) -> list[str]:
    wid = _resolve_warehouse_id(warehouse_id)
    return warehouse_emails(wid) if wid else []


@app.get("/")
def index():
    return render_template("index.html", muse_url=MUSE_LOGIN_URL, app_build=APP_BUILD)


@app.get("/api/version")
def api_version():
    import os

    packaged = is_frozen()
    auto_reload = not packaged and os.environ.get("FLASK_RELOAD", "1") != "0"
    return jsonify(
        {
            "build": APP_BUILD,
            "auto_reload": auto_reload,
            "packaged": packaged,
            "data_dir": str(DATA_DIR),
            "consent_status": "https://muse.ai/api/consent/status",
        }
    )


@app.get("/api/warehouses")
def api_warehouses_list():
    return jsonify(list_warehouses_summary())


@app.post("/api/warehouses/active")
def api_warehouses_set_active():
    body = request.get_json(force=True, silent=True) or {}
    wh_id = (body.get("id") or "").strip()
    wh = set_active_warehouse(wh_id)
    if not wh:
        return jsonify({"ok": False, "error": "Không tìm thấy kho"}), 404
    return jsonify({"ok": True, "warehouse": wh, "summary": list_warehouses_summary()})


@app.post("/api/warehouses")
def api_warehouses_create():
    body = request.get_json(force=True, silent=True) or {}
    wh = create_warehouse(body.get("name"), max_accounts=body.get("max_accounts"))
    return jsonify({"ok": True, "warehouse": wh, "summary": list_warehouses_summary()})


@app.post("/api/warehouses/delete")
def api_warehouses_delete():
    body = request.get_json(force=True, silent=True) or {}
    result = delete_warehouse((body.get("id") or "").strip())
    if result is None:
        return jsonify({"ok": False, "error": "Không thấy kho hoặc không xóa được Kho lỗi"}), 404
    return jsonify({"ok": True, **result})


@app.patch("/api/warehouses/<warehouse_id>")
def api_warehouses_rename(warehouse_id: str):
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not rename_warehouse(warehouse_id, name):
        return jsonify({"ok": False, "error": "Không tìm thấy kho"}), 404
    return jsonify({"ok": True, "summary": list_warehouses_summary()})


@app.post("/api/settings/accounts-per-warehouse")
def api_settings_accounts_per_warehouse():
    body = request.get_json(force=True, silent=True) or {}
    value = set_accounts_per_warehouse(body.get("value"))
    return jsonify({"ok": True, "accounts_per_warehouse": value, "summary": list_warehouses_summary()})


@app.post("/api/warehouses/import-split")
def api_warehouses_import_split():
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("text", "")
    proxies_raw = body.get("proxies", "")
    mode = (body.get("mode") or "replace").strip().lower()
    if mode == "append":
        summary = import_split_append(raw, proxies_raw)
    else:
        try:
            summary = import_split_replace(raw, proxies_raw)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "summary": summary})


@app.post("/api/accounts/move-error")
def api_accounts_move_error():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip()
    action = (body.get("action") or "to-error").strip()
    if not email:
        return jsonify({"ok": False, "error": "Thiếu email"}), 400
    try:
        if action == "restore":
            result = restore_account_from_error(email)
        else:
            result = move_account_to_error(email)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result, "summary": list_warehouses_summary()})


@app.get("/api/accounts")
def api_list_accounts():
    wid = request.args.get("warehouse_id") or get_active_warehouse().get("id")
    wh = get_warehouse(wid) or get_active_warehouse()
    accounts = load_accounts(wh["id"])
    return jsonify(
        {
            "accounts": accounts,
            "warehouse": {
                "id": wh["id"],
                "name": wh.get("name"),
                "max": get_accounts_per_warehouse(),
                "count": len(accounts),
            },
        }
    )


@app.post("/api/accounts")
def api_save_accounts():
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("text", "")
    proxy_lines = (body.get("proxies") or "").splitlines()
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    index = 0
    for line in raw.splitlines():
        parsed = parse_account_line(line)
        if not parsed:
            continue
        email, lay2fa_url, totp_secret, mail_password, line_proxy = parsed
        # Proxy ở ô phía dưới (dán mới) thắng proxy có sẵn trong dòng.
        proxy = proxy_lines[index].strip() if index < len(proxy_lines) else ""
        if not proxy:
            proxy = line_proxy
        index += 1
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {"email": email, "lay2fa_url": lay2fa_url, "totp_secret": totp_secret, "proxy": proxy, "mailtm_password": mail_password}
        )
    active = get_active_warehouse()
    if active.get("kind") != "error" and len(rows) > get_accounts_per_warehouse():
        return jsonify(
            {"ok": False, "error": f"Mỗi kho tối đa {get_accounts_per_warehouse()} tài khoản. Dùng Dán tài khoản để chia nhiều kho."}
        ), 400
    save_active_accounts(rows)
    return jsonify({"ok": True, "count": len(rows), "summary": list_warehouses_summary()})


@app.post("/api/accounts/import")
def api_import_accounts():
    body = request.get_json(force=True, silent=True) or {}
    append_lines = body.get("lines", [])
    if isinstance(append_lines, str):
        append_lines = append_lines.splitlines()
    current = load_accounts()
    by_email = {r["email"].lower(): r for r in current}
    added = 0
    for line in append_lines:
        parsed = parse_account_line(str(line))
        if not parsed:
            continue
        email, lay2fa_url, totp_secret, mail_password, line_proxy = parsed
        key = email.lower()
        if key not in by_email:
            added += 1
        previous = by_email.get(key) or {}
        by_email[key] = {
            "email": email,
            "lay2fa_url": lay2fa_url,
            "totp_secret": totp_secret or previous.get("totp_secret", ""),
            "proxy": line_proxy or previous.get("proxy", ""),
            "mailtm_password": mail_password or previous.get("mailtm_password", ""),
        }
    merged = list(by_email.values())
    if len(merged) > get_accounts_per_warehouse():
        return jsonify({"ok": False, "error": f"Kho hiện tại tối đa {get_accounts_per_warehouse()} TK."}), 400
    save_active_accounts(merged)
    return jsonify({"ok": True, "added": added, "total": len(by_email)})


@app.post("/api/accounts/export-login")
def api_export_login_data():
    body = request.get_json(force=True, silent=True) or {}
    scope = (body.get("scope") or "regular").strip().lower()
    emails = body.get("emails") or []
    if isinstance(emails, str):
        emails = emails.splitlines()
    wanted = {(e or "").strip().lower() for e in emails if (e or "").strip()}
    wanted = wanted or None
    if scope == "created":
        kind = "password" if body.get("created_kind") == "password" else "plain"
        text, count = export_created_login_data(body.get("warehouse_id"), kind, wanted)
    else:
        text, count = export_login_data(body.get("warehouse_id"), wanted)
    return jsonify({"ok": True, "text": text, "count": count})


def _login_creds(email: str) -> dict:
    created = find_created_account(email) or {}
    proxy = (created.get("proxy") or "").strip() or get_proxy_for_email(email)
    return {
        "proxy": proxy or None,
        "totp_secret": get_totp_secret_for_email(email) or None,
        "mailtm_password": (created.get("mailtm_password") or "").strip() or get_mailtm_password_for_email(email) or None,
        "account_password": get_account_password_for_email(email) or None,
    }


@app.get("/api/otp/<path:email>")
def api_otp(email: str):
    email = email.strip()
    created = find_created_account(email) or {}
    mailbox_password = (created.get("mailtm_password") or "").strip() or get_mailtm_password_for_email(email)
    _mp = (created.get("mail_provider") or "").strip().lower()
    if _mp in ("gmail", "yahoo"):
        # Mat khau ung dung chi nam trong gmail_accounts.json /
        # yahoo_accounts.json (file rieng, gitignore) — kho TK chi luu
        # ACCOUNT_PASSWORD chung.
        try:
            _store = (
                __import__("gmail_store")
                if _mp == "gmail"
                else __import__("yahoo_store")
            )
            mailbox_password = _store.get_app_password(email) or ""
        except Exception:
            mailbox_password = ""
    if mailbox_password:
        from mail_provider import client as mail_client, error_class as mail_error_class, label as mail_label, provider_for_email, requests_proxies

        _proxies = requests_proxies(_login_creds(email).get("proxy"))
        _provider = provider_for_email(email, proxies=_proxies)
        _mc = mail_client(_provider)
        _MailError = mail_error_class(_provider)
        try:
            code = _mc.latest_code(email, mailbox_password, proxies=_proxies)
        except _MailError as exc:
            return jsonify({"code": None, "error": str(exc), "message": None})
        return jsonify({
            "code": code,
            "error": None,
            "message": {"subject": mail_label(_provider), "sender": mail_label(_provider), "date": ""},
        })
    result = get_latest_muse_code(email)
    payload = {
        "code": result.code,
        "error": result.error,
        "message": None,
    }
    if result.message:
        payload["message"] = {
            "subject": result.message.subject,
            "sender": result.message.sender,
            "date": result.message.date,
        }
    return jsonify(payload)


@app.post("/api/open-muse")
def api_open_muse():
    webbrowser.open(MUSE_LOGIN_URL)
    return jsonify({"ok": True, "url": MUSE_LOGIN_URL})


def _set_job(job_id: str, data: dict) -> None:
    with _jobs_lock:
        _login_jobs[job_id] = {**_login_jobs.get(job_id, {}), **data}


def _get_job(job_id: str) -> dict:
    with _jobs_lock:
        return dict(_login_jobs.get(job_id) or {})


def _run_login_job(
    job_id: str,
    email: str,
    headless: bool,
    *,
    close_after: bool,
    save_cookies: bool,
) -> None:
    def on_progress(p: LoginProgress) -> None:
        _set_job(
            job_id,
            {
                "status": "running" if p.step not in ("done", "error") else p.step,
                "step": p.step,
                "detail": p.detail,
                "code": p.code,
                "error": p.error,
                "final_url": p.final_url,
                "cookie_header": p.cookie_header,
                "cookies_count": p.cookies_count,
                "cookies_saved_at": p.cookies_saved_at,
            },
        )

    _set_job(job_id, {"status": "running", "step": "queued", "detail": "Đang chạy…"})
    try:
        creds = _login_creds(email)
        result = login_muse(
            email,
            headless=headless,
            close_after=close_after,
            save_cookies=save_cookies,
            proxy=creds["proxy"],
            totp_secret=creds["totp_secret"],
            mailtm_password=creds["mailtm_password"],
            account_password=creds["account_password"],
            on_progress=on_progress,
        )
    except Exception as exc:
        _set_job(job_id, {"status": "error", "step": "error", "error": str(exc)})
        return
    if result.step == "needs_user":
        _set_job(
            job_id,
            {
                "status": "needs_user",
                "step": "needs_user",
                "detail": result.detail,
                "final_url": result.final_url,
                "error": None,
            },
        )
        return
    if result.step == "done":
        _set_job(
            job_id,
            {
                "status": "done",
                "step": "done",
                "detail": result.detail,
                "code": result.code,
                "final_url": result.final_url,
                "cookie_header": result.cookie_header,
                "cookies_count": result.cookies_count,
                "cookies_saved_at": result.cookies_saved_at,
            },
        )
    else:
        _set_job(job_id, {"status": "error", "step": "error", "error": result.error, "final_url": result.final_url})


def _set_batch(job_id: str, data: dict) -> None:
    with _jobs_lock:
        _batch_jobs[job_id] = {**_batch_jobs.get(job_id, {}), **data}


def _cookie_one(email: str) -> dict:
    if stop_requested():
        return {"email": email, "status": "stopped", "error": "Đã dừng"}
    try:
        creds = _login_creds(email)
        result = login_muse(
            email,
            headless=False,
            close_after=True,
            save_cookies=True,
            proxy=creds["proxy"],
            totp_secret=creds["totp_secret"],
            mailtm_password=creds["mailtm_password"],
            account_password=creds["account_password"],
            on_progress=None,
        )
    except Exception as exc:
        return {"email": email, "status": "error", "error": str(exc)}
    if result.step == "done" and result.cookie_header:
        return {
            "email": email,
            "status": "ok",
            "cookies_count": result.cookies_count,
            "saved_at": result.cookies_saved_at,
        }
    return {"email": email, "status": "error", "error": result.error or "Không lưu được cookie"}


def _run_batch_cookies(job_id: str, emails: list[str], *, skip_existing: bool) -> None:
    total = len(emails)
    ok_count = 0
    fail_count = 0
    skip_count = 0
    rows: list[dict] = []
    pending: list[str] = []
    running: set[str] = set()
    state_lock = threading.Lock()

    def publish(detail: str) -> None:
        if stop_requested():
            return
        with state_lock:
            snapshot = {
                "status": "running",
                "total": total,
                "current": ok_count + fail_count + skip_count,
                "workers": BATCH_PARALLEL,
                "running_emails": sorted(running),
                "detail": detail,
                "ok": ok_count,
                "fail": fail_count,
                "skipped": skip_count,
                "results": list(rows),
            }
        _set_batch(job_id, snapshot)

    try:
        for email in emails:
            if skip_existing and get_account_cookies(email):
                skip_count += 1
                rows.append({"email": email, "status": "skipped", "detail": "Đã có cookie"})
            else:
                pending.append(email)
        publish(f"Bỏ qua {skip_count} TK đã có cookie · sắp chạy {min(BATCH_PARALLEL, len(pending) or 0)} luồng")

        def run_one(email: str) -> dict:
            if stop_requested():
                return {"email": email, "status": "stopped", "error": "Đã dừng"}
            with state_lock:
                running.add(email)
                active = sorted(running)
            publish(f"Đang chạy {len(active)}/{BATCH_PARALLEL} luồng · {', '.join(active)}")
            try:
                return _cookie_one(email)
            finally:
                with state_lock:
                    running.discard(email)

        stopped = False
        if pending:
            with ThreadPoolExecutor(max_workers=BATCH_PARALLEL) as pool:
                futures = [pool.submit(run_one, email) for email in pending]
                for fut in as_completed(futures):
                    if stop_requested():
                        stopped = True
                        break
                    row = fut.result()
                    with state_lock:
                        rows.append(row)
                        if row.get("status") == "ok":
                            ok_count += 1
                        elif row.get("status") != "stopped":
                            fail_count += 1
                    publish(
                        f"Xong {ok_count + fail_count + skip_count}/{total} · còn {len(running)} luồng đang chạy"
                    )
                if stopped:
                    for fut in futures:
                        fut.cancel()
        if stopped or stop_requested():
            _set_batch(
                job_id,
                {
                    "status": "stopped",
                    "current": ok_count + fail_count + skip_count,
                    "workers": BATCH_PARALLEL,
                    "running_emails": [],
                    "ran": True,
                    "detail": f"Đã dừng. OK {ok_count}, lỗi {fail_count}, bỏ qua {skip_count}.",
                    "ok": ok_count,
                    "fail": fail_count,
                    "skipped": skip_count,
                    "results": rows,
                },
            )
            return

        ran = bool(pending)
        if ran:
            detail = f"Hoàn tất ({BATCH_PARALLEL} luồng): {ok_count} OK, {fail_count} lỗi, {skip_count} bỏ qua."
        else:
            detail = f"Không mở Chrome — {skip_count} TK đã có cookie. Bấm Đăng nhập lấy lại cookie."
        _set_batch(
            job_id,
            {
                "status": "done",
                "current": total,
                "workers": BATCH_PARALLEL,
                "running_emails": [],
                "ran": ran,
                "detail": detail,
                "ok": ok_count,
                "fail": fail_count,
                "skipped": skip_count,
                "results": rows,
            },
        )
    except Exception as exc:
        _set_batch(job_id, {"status": "error", "error": str(exc), "results": rows})
    finally:
        _batch_running.clear()


def _token_one(email: str) -> dict:
    if stop_requested():
        return {"email": email, "status": "stopped", "error": "Đã dừng"}
    try:
        creds = _login_creds(email)
        result = check_token_usage(email, proxy=creds["proxy"], headless=True)
    except Exception as exc:
        return {"email": email, "status": "error", "error": str(exc)}
    if result.get("ok"):
        return {
            "email": email,
            "status": "ok",
            "used_pct": result.get("used_pct") or "",
            "left": result.get("left") or "",
            "reset": result.get("reset") or "",
        }
    return {"email": email, "status": "error", "error": result.get("error") or "Không đọc được token"}


def _run_batch_tokens(job_id: str, emails: list[str]) -> None:
    total = len(emails)
    ok_count = 0
    fail_count = 0
    skip_count = 0
    rows: list[dict] = []
    pending: list[str] = []
    running: set[str] = set()
    state_lock = threading.Lock()

    def publish(detail: str) -> None:
        if stop_requested():
            return
        with state_lock:
            snapshot = {
                "status": "running",
                "total": total,
                "current": ok_count + fail_count + skip_count,
                "workers": BATCH_PARALLEL,
                "running_emails": sorted(running),
                "detail": detail,
                "ok": ok_count,
                "fail": fail_count,
                "skipped": skip_count,
                "results": list(rows),
            }
        _set_batch(job_id, snapshot)

    try:
        for email in emails:
            rec = get_account_cookies(email)
            if not rec or not (rec.get("cookie_header") or "").strip():
                skip_count += 1
                rows.append({"email": email, "status": "skipped", "detail": "Chưa có cookie"})
            else:
                pending.append(email)
        publish(f"Bỏ qua {skip_count} TK chưa có cookie · sắp chạy {min(BATCH_PARALLEL, len(pending) or 0)} luồng")

        def run_one(email: str) -> dict:
            if stop_requested():
                return {"email": email, "status": "stopped", "error": "Đã dừng"}
            with state_lock:
                running.add(email)
                active = sorted(running)
            publish(f"Đang chạy {len(active)}/{BATCH_PARALLEL} luồng · {', '.join(active)}")
            try:
                return _token_one(email)
            finally:
                with state_lock:
                    running.discard(email)

        stopped = False
        if pending:
            with ThreadPoolExecutor(max_workers=BATCH_PARALLEL) as pool:
                futures = [pool.submit(run_one, email) for email in pending]
                for fut in as_completed(futures):
                    if stop_requested():
                        stopped = True
                        break
                    row = fut.result()
                    with state_lock:
                        rows.append(row)
                        if row.get("status") == "ok":
                            ok_count += 1
                        elif row.get("status") != "stopped":
                            fail_count += 1
                    publish(
                        f"Xong {ok_count + fail_count + skip_count}/{total} · còn {len(running)} luồng đang chạy"
                    )
                if stopped:
                    for fut in futures:
                        fut.cancel()
        if stopped or stop_requested():
            _set_batch(
                job_id,
                {
                    "status": "stopped",
                    "current": ok_count + fail_count + skip_count,
                    "workers": BATCH_PARALLEL,
                    "running_emails": [],
                    "ran": True,
                    "detail": f"Đã dừng. OK {ok_count}, lỗi {fail_count}, bỏ qua {skip_count}.",
                    "ok": ok_count,
                    "fail": fail_count,
                    "skipped": skip_count,
                    "results": rows,
                },
            )
            return

        ran = bool(pending)
        detail = (
            f"Hoàn tất ({BATCH_PARALLEL} luồng): {ok_count} TK đọc được token, {fail_count} lỗi, {skip_count} bỏ qua."
            if ran
            else f"Không chạy TK nào — {skip_count} TK chưa có cookie."
        )
        _set_batch(
            job_id,
            {
                "status": "done",
                "current": total,
                "workers": BATCH_PARALLEL,
                "running_emails": [],
                "ran": ran,
                "detail": detail,
                "ok": ok_count,
                "fail": fail_count,
                "skipped": skip_count,
                "results": rows,
            },
        )
    except Exception as exc:
        _set_batch(job_id, {"status": "error", "error": str(exc), "results": rows})
    finally:
        _batch_running.clear()


@app.post("/api/batch/cookies")
def api_batch_cookies():
    if _batch_running.is_set():
        return jsonify({"ok": False, "error": "Đang chạy batch khác — chờ xong rồi thử lại."}), 409

    body = request.get_json(force=True, silent=True) or {}
    skip_existing = bool(body.get("skip_existing", False))
    emails_param = body.get("emails")
    wh_id = body.get("warehouse_id") or get_active_warehouse().get("id")
    if emails_param:
        emails = [str(e).strip() for e in emails_param if str(e).strip()]
    else:
        emails = warehouse_emails(wh_id)

    if not emails:
        return jsonify({"ok": False, "error": "Không có tài khoản trong danh sách."}), 400

    job_id = uuid.uuid4().hex[:12]
    clear_stop()
    _batch_running.set()
    _set_batch(job_id, {"status": "running", "total": len(emails), "emails": emails})
    thread = threading.Thread(
        target=_run_batch_cookies,
        args=(job_id, emails),
        kwargs={"skip_existing": skip_existing},
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "total": len(emails), "workers": BATCH_PARALLEL})


@app.post("/api/batch/tokens")
def api_batch_tokens():
    if _batch_running.is_set():
        return jsonify({"ok": False, "error": "Đang chạy batch khác — chờ xong rồi thử lại."}), 409

    body = request.get_json(force=True, silent=True) or {}
    emails_param = body.get("emails")
    wh_id = body.get("warehouse_id")
    kind = body.get("kind") or "plain"
    if emails_param:
        emails = [str(e).strip() for e in emails_param if str(e).strip()]
    elif wh_id:
        emails = [
            (a.get("email") or "").strip()
            for a in list_created_accounts(wh_id, kind).get("accounts", [])
            if (a.get("email") or "").strip()
        ]
    else:
        emails = warehouse_emails(get_active_warehouse().get("id"))

    if not emails:
        return jsonify({"ok": False, "error": "Không có tài khoản trong danh sách."}), 400

    job_id = uuid.uuid4().hex[:12]
    clear_stop()
    _batch_running.set()
    _set_batch(job_id, {"status": "running", "total": len(emails), "emails": emails})
    thread = threading.Thread(target=_run_batch_tokens, args=(job_id, emails), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "total": len(emails), "workers": BATCH_PARALLEL})


def _redeem_targets(accounts: list[dict], kind: str, buff_code: str = "") -> list[dict]:
    """Tài khoản chưa có Mã đã nhập trong kho: con sau nhập mã của con trước liền kề,
    con trước không có mã thì lấy mã trong kho. Con chưa có cookie thì bỏ qua.
    Kho buff ('Kho - MÃ'): mọi tài khoản đều nhập đúng mã buff trong tên kho."""
    targets: list[dict] = []
    for pos, acc in enumerate(accounts):
        email = (acc.get("email") or "").strip()
        if not email:
            continue
        if (acc.get("redeemed_invite_code") or "").strip():
            continue
        if not (acc.get("has_cookie") and acc.get("cookie_header")):
            targets.append({"email": email, "status": "skipped", "detail": "Chưa có cookie"})
            continue
        code = ""
        if buff_code:
            code = buff_code
        elif pos > 0:
            code = (accounts[pos - 1].get("invite_code") or "").strip().upper()
        if not code:
            code = warehouse_invite_code(email, kind=kind)
        if not code:
            targets.append({"email": email, "status": "skipped", "detail": "Không có mã để nhập"})
            continue
        targets.append({"email": email, "status": "pending", "code": code})
    return targets


def _redeem_one(item: dict) -> dict:
    email = item["email"]
    if stop_requested():
        return {"email": email, "status": "stopped", "error": "Đã dừng"}
    try:
        creds = _login_creds(email)
        result = redeem_invite_for_account(email, item["code"], proxy=creds["proxy"], headless=True)
    except Exception as exc:
        return {"email": email, "status": "error", "error": str(exc)}
    if result.get("ok"):
        return {"email": email, "status": "ok", "code": result.get("code") or ""}
    return {"email": email, "status": "error", "error": result.get("error") or "Không quy đổi được mã"}


def _run_batch_redeem(job_id: str, targets: list[dict]) -> None:
    total = len(targets)
    ok_count = 0
    fail_count = 0
    skip_count = 0
    rows: list[dict] = []
    pending: list[dict] = []
    running: set[str] = set()
    state_lock = threading.Lock()

    def publish(detail: str) -> None:
        if stop_requested():
            return
        with state_lock:
            snapshot = {
                "status": "running",
                "total": total,
                "current": ok_count + fail_count + skip_count,
                "workers": BATCH_PARALLEL,
                "running_emails": sorted(running),
                "detail": detail,
                "ok": ok_count,
                "fail": fail_count,
                "skipped": skip_count,
                "results": list(rows),
            }
        _set_batch(job_id, snapshot)

    try:
        for item in targets:
            if item.get("status") == "pending":
                pending.append(item)
            else:
                skip_count += 1
                rows.append({"email": item["email"], "status": "skipped", "detail": item.get("detail") or "Bỏ qua"})
        publish(f"Bỏ qua {skip_count} TK · sắp chạy {min(BATCH_PARALLEL, len(pending) or 0)} luồng")

        def run_one(item: dict) -> dict:
            email = item["email"]
            if stop_requested():
                return {"email": email, "status": "stopped", "error": "Đã dừng"}
            with state_lock:
                running.add(email)
                active = sorted(running)
            publish(f"Đang chạy {len(active)}/{BATCH_PARALLEL} luồng · {', '.join(active)}")
            try:
                return _redeem_one(item)
            finally:
                with state_lock:
                    running.discard(email)

        stopped = False
        if pending:
            with ThreadPoolExecutor(max_workers=BATCH_PARALLEL) as pool:
                futures = [pool.submit(run_one, item) for item in pending]
                for fut in as_completed(futures):
                    if stop_requested():
                        stopped = True
                        break
                    row = fut.result()
                    with state_lock:
                        rows.append(row)
                        if row.get("status") == "ok":
                            ok_count += 1
                        elif row.get("status") != "stopped":
                            fail_count += 1
                    publish(
                        f"Xong {ok_count + fail_count + skip_count}/{total} · còn {len(running)} luồng đang chạy"
                    )
                if stopped:
                    for fut in futures:
                        fut.cancel()
        if stopped or stop_requested():
            _set_batch(
                job_id,
                {
                    "status": "stopped",
                    "current": ok_count + fail_count + skip_count,
                    "workers": BATCH_PARALLEL,
                    "running_emails": [],
                    "ran": True,
                    "detail": f"Đã dừng. OK {ok_count}, lỗi {fail_count}, bỏ qua {skip_count}.",
                    "ok": ok_count,
                    "fail": fail_count,
                    "skipped": skip_count,
                    "results": rows,
                },
            )
            return

        ran = bool(pending)
        detail = (
            f"Hoàn tất ({BATCH_PARALLEL} luồng): {ok_count} TK quy đổi được mã, {fail_count} lỗi, {skip_count} bỏ qua."
            if ran
            else f"Không chạy TK nào — {skip_count} TK bỏ qua."
        )
        _set_batch(
            job_id,
            {
                "status": "done",
                "current": total,
                "workers": BATCH_PARALLEL,
                "running_emails": [],
                "ran": ran,
                "detail": detail,
                "ok": ok_count,
                "fail": fail_count,
                "skipped": skip_count,
                "results": rows,
            },
        )
    except Exception as exc:
        _set_batch(job_id, {"status": "error", "error": str(exc), "results": rows})
    finally:
        _batch_running.clear()


@app.post("/api/batch/redeem")
def api_batch_redeem():
    if _batch_running.is_set():
        return jsonify({"ok": False, "error": "Đang chạy batch khác — chờ xong rồi thử lại."}), 409

    body = request.get_json(force=True, silent=True) or {}
    wh_id = body.get("warehouse_id")
    kind = body.get("kind") or "plain"
    if not wh_id:
        return jsonify({"ok": False, "error": "Chưa chọn kho."}), 400
    board = list_created_accounts(wh_id, kind)
    wh = next((w for w in board.get("warehouses", []) if w.get("id") == wh_id), None)
    buff_code = _buff_code_from_name((wh or {}).get("name") or "")
    accounts = board.get("accounts", [])
    emails_param = body.get("emails")
    if emails_param:
        wanted = {(str(e).strip().lower()) for e in emails_param if str(e).strip()}
        accounts = [a for a in accounts if (a.get("email") or "").strip().lower() in wanted]
    targets = _redeem_targets(accounts, kind, buff_code=buff_code)
    pending = [t for t in targets if t.get("status") == "pending"]
    if not pending:
        return jsonify({"ok": False, "error": "Không có tài khoản nào cần chạy lại mã mời (đã nhập đủ hoặc chưa có cookie)."}), 400

    job_id = uuid.uuid4().hex[:12]
    clear_stop()
    _batch_running.set()
    _set_batch(job_id, {"status": "running", "total": len(targets), "emails": [t["email"] for t in targets]})
    thread = threading.Thread(target=_run_batch_redeem, args=(job_id, targets), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "total": len(targets), "pending": len(pending), "workers": BATCH_PARALLEL})


@app.post("/api/batch/stop")
def api_batch_stop():
    closed = request_stop()
    with _jobs_lock:
        for job in _batch_jobs.values():
            if job.get("status") == "running":
                job["status"] = "stopped"
                job["detail"] = "Đã dừng tiến trình đang chạy."
                job["running_emails"] = []
    return jsonify({"ok": True, "closed": closed})


@app.get("/api/batch/<job_id>")
def api_batch_status(job_id: str):
    with _jobs_lock:
        job = _batch_jobs.get(job_id)
    if not job:
        return jsonify({"status": "unknown"}), 404
    return jsonify(job)


@app.get("/api/cookies/copy-text")
def api_cookies_copy_text():
    filter_mode = request.args.get("filter", "has")
    search = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    scope = _scope_emails(request.args.get("warehouse_id"))
    text = export_filtered_text(
        filter_mode=filter_mode or "has",
        search=search,
        limit=limit,
        scope_emails=scope if scope else None,
    )
    if not text.strip():
        return jsonify({"ok": False, "error": "Chưa có cookie khớp bộ lọc.", "text": ""})
    return jsonify({"ok": True, "text": text, "lines": len(text.splitlines())})


class _InviteChain:
    """TK 1 không nhập mã. TK n nhập mã của TK n-1. Mã được phát ngay khi lấy được."""

    def __init__(self, count: int, seed: str = "") -> None:
        self.seed = (seed or "").strip().upper()
        self.slots: list[str | None] = [None] * count
        self.cv = threading.Condition()

    def publish(self, index: int, code: str | None) -> None:
        with self.cv:
            if 0 <= index < len(self.slots) and self.slots[index] is None:
                self.slots[index] = (code or "").strip().upper()
            self.cv.notify_all()

    def wait_prior(self, index: int) -> str | None:
        if index <= 0:
            return self.seed or None
        with self.cv:
            while True:
                if stop_requested():
                    raise RuntimeError("Đã dừng")
                pending = False
                for earlier in range(index - 1, -1, -1):
                    slot = self.slots[earlier]
                    if slot is None:
                        pending = True
                        break
                    if slot:
                        return slot
                if not pending:
                    return self.seed or None
                self.cv.wait(timeout=2)

    def tail(self) -> str:
        tail = self.seed
        for slot in self.slots:
            if slot is None:
                break
            if slot:
                tail = slot
        return tail


def _signup_workers(count: int, proxy_count: int, cap: int = BATCH_PARALLEL) -> int:
    if count <= 1:
        return 1
    if proxy_count:
        return min(count, proxy_count, cap)
    return min(count, cap)


def _signup_one(
    job_id: str,
    *,
    count: int,
    mode: str,
    state: dict,
    state_lock: threading.Lock,
    proxy: str = "",
    index: int = 0,
    chain: _InviteChain | None = None,
    fixed_code: str = "",
    err_box: dict | None = None,
    mail_provider: str = "tempmail",
    browser: str = "auto",
) -> bool:
    box = {"email": "", "published": False}

    def on_invite(code: str | None) -> None:
        box["published"] = True
        if chain is not None:
            chain.publish(index, code)
        if code and box["email"]:
            set_created_invite_code(box["email"], code)

    def on_redeem(code: str | None) -> None:
        if code and box["email"]:
            set_redeemed_invite_code(box["email"], code)

    try:
        ok = _signup_one_run(
            job_id,
            count=count,
            mode=mode,
            state=state,
            state_lock=state_lock,
            proxy=proxy,
            index=index,
            chain=chain,
            box=box,
            on_invite=on_invite,
            on_redeem=on_redeem,
            fixed_code=fixed_code,
            mail_provider=mail_provider,
            browser=browser,
        )
        if err_box is not None:
            err_box["error"] = box.get("error", "")
        return ok
    except Exception as exc:
        if err_box is not None:
            err_box["error"] = str(exc)
        return False
    finally:
        if chain is not None and not box["published"]:
            chain.publish(index, None)


def _signup_one_run(
    job_id: str,
    *,
    count: int,
    mode: str,
    state: dict,
    state_lock: threading.Lock,
    proxy: str,
    index: int,
    chain: _InviteChain | None,
    box: dict,
    on_invite,
    on_redeem=None,
    fixed_code: str = "",
    mail_provider: str = "tempmail",
    browser: str = "auto",
) -> bool:
    email = ""

    def publish(detail: str | None = None) -> None:
        with state_lock:
            running = list(state["running"])
            fails = list(state["failures"])
            steps = dict(state.get("steps") or {})
            text = detail or (
                f"{state['ok']}/{count} TK được · {state['finished']} lần thử · {state['fail']} lỗi"
                + (f" · đang chạy {', '.join(running)}" if running else "")
            )
            # Batch nhieu TK: hien them buoc hien tai cua tung TK dang chay
            # (vd "han...@...: Dang tao hop thu...") de user thay log.
            step_lines = [
                f"{em}: {steps[em]}" for em in running if steps.get(em)
            ]
            if step_lines:
                text += "\n" + "\n".join(step_lines)
            note = state.get("invite_note") or ""
            if note and note not in text:
                text = note + "\n" + text
            if fails:
                text += "\n" + "\n".join(fails)
        _set_job(
            job_id,
            {
                "status": "running",
                "step": "batch",
                "detail": text,
                "email": email,
                "done_count": state["finished"],
                "ok_count": state["ok"],
                "fail_count": state["fail"],
                "failures": fails,
                "total": count,
            },
        )

    def on_progress(p: LoginProgress) -> None:
        if count == 1:
            _set_job(
                job_id,
                {
                    "status": "running" if p.step not in ("done", "error") else "running",
                    "step": p.step,
                    "detail": p.detail,
                    "code": p.code,
                    "error": p.error,
                    "email": email,
                    "final_url": p.final_url,
                },
            )
        else:
            if p.step == "invite" and p.detail:
                with state_lock:
                    state["invite_note"] = p.detail
            # Luu buoc hien tai cua TK nay de publish() hien log.
            if p.detail:
                with state_lock:
                    state.setdefault("steps", {})[email] = p.detail
            publish()

    def note_fail(reason: str, *, closed: bool) -> None:
        text = (reason or "Không tạo được").strip()
        if closed:
            text += ". Đã đóng Chrome"
        line = f"{email}: {text}" if email else text
        with state_lock:
            state["running"] = [item for item in state["running"] if item != email]
            (state.get("steps") or {}).pop(email, None)
            state["fail"] += 1
            state["finished"] += 1
            state["failures"].append(line)
            state["last_error"] = line
            # Lỗi của riêng lần thử này, để lane phân loại đúng proxy của nó
            # (state["last_error"] là chung cho mọi lane, lane khác có thể ghi
            # đè trước khi lane này đọc).
            box["error"] = text
            if "kho thẻ đã hết" in text.lower():
                state["cards_exhausted"] = True
        if count == 1:
            _set_job(
                job_id,
                {
                    "status": "error",
                    "step": "error",
                    "error": line,
                    "detail": line,
                    "email": email,
                    "failures": [line],
                },
            )
        else:
            publish()

    def prepare_mail() -> tuple[str, str, str | None]:
        nonlocal email
        from mail_provider import create_mailbox_for, requests_proxies

        # Doc mail di qua proxy cua attempt (giong Chrome) de proxy phan
        # giai DNS thay may local — may nao loi DNS tempmail.plus thi nho
        # vay van tao duoc hop thu / doc duoc ma.
        email, password, token = create_mailbox_for(
            mail_provider, ACCOUNT_PASSWORD, mode, proxies=requests_proxies(proxy)
        )
        box["email"] = email
        add_created_account(
            email,
            mailtm_password=password,
            kind="password" if mode == "password" else "plain",
            proxy=proxy,
            mail_provider=mail_provider,
        )
        with state_lock:
            state["running"].append(email)
        publish(f"Đã lưu {email}. Hộp thư đọc lại được." if count == 1 else None)
        return email, token, password if mode == "password" else None

    def take_prior() -> str | None:
        # Buff: mọi tài khoản đều nhập cùng 1 mã buff, không nối chuỗi, không lấy mã trong kho.
        if fixed_code:
            return fixed_code
        if chain is not None:
            prior = chain.wait_prior(index)
            if prior:
                return prior
        # Không lấy được mã của tài khoản trước (tài khoản trước lỗi, hoặc chưa có mã):
        # lấy mã trong kho (mã mới nhất, khác tài khoản này) để tài khoản nào cũng có mã nhập.
        fallback = warehouse_invite_code(email, kind="password" if mode == "password" else "plain")
        return fallback or None

    try:
        result = create_muse_account(
            "",
            "",
            proxy=proxy or None,
            keep_open=False,
            on_progress=on_progress,
            take_prior=take_prior,
            on_invite=on_invite,
            on_redeem=on_redeem,
            prepare_mail=prepare_mail,
            allow_redeem_fallback=not fixed_code,
            mail_provider=mail_provider,
            browser_name=browser,
        )
        ok = result.step == "done"
        error = result.error or ""
    except Exception as exc:
        ok = False
        error = str(exc)
    if not ok:
        # Tài khoản tạo lỗi thì bỏ qua, không lưu vào kho.
        try:
            delete_created_account(email)
        except Exception:
            pass
        note_fail(error or "Không tạo được", closed=True)
        return False
    with state_lock:
        state["running"] = [item for item in state["running"] if item != email]
        (state.get("steps") or {}).pop(email, None)
        state["finished"] += 1
        state["ok"] += 1
    if count == 1:
        _set_job(
            job_id,
            {
                "status": "done" if ok else "error",
                "step": "done" if ok else "error",
                "detail": (result.detail or f"Đã tạo {email}.") if ok else error,
                "error": "" if ok else error,
                "email": email,
            },
        )
    else:
        publish()
    return True


def _signup_lanes(count: int, proxies: list[str], per_proxy: int) -> list[tuple[str, list[int]]]:
    """Chia tài khoản cho các proxy theo vòng tròn để dùng hết luồng song song.
    Mỗi proxy tối đa per_proxy TK, chạy lần lượt trên cùng 1 proxy. Chia theo
    khối liên tục (proxy 1 ôm 10 TK đầu) sẽ dồn việc vào vài proxy đầu và các
    proxy còn lại ngồi chơi dù còn luồng trống."""
    n = len(proxies)
    lanes = [
        (proxy, list(range(j, count, n))[:per_proxy])
        for j, proxy in enumerate(proxies)
    ]
    return [(proxy, idxs) for proxy, idxs in lanes if idxs]


def _run_signup_lane(
    job_id: str,
    *,
    count: int,
    mode: str,
    state: dict,
    state_lock: threading.Lock,
    proxy: str,
    indices: list[int],
    chain: _InviteChain | None,
    fixed_code: str = "",
    mail_provider: str = "tempmail",
    browser: str = "auto",
) -> None:
    """Một proxy chạy lần lượt từng tài khoản. Tài khoản lỗi thì báo lỗi rồi
    dừng làn này, các tài khoản còn lại trên proxy đó bỏ qua, làn khác vẫn chạy."""
    for pos, index in enumerate(indices):
        if stop_requested():
            return
        with state_lock:
            if state.get("cards_exhausted"):
                _skip_lane_rest(job_id, count, state, state_lock, indices[pos:], chain, "Kho thẻ đã hết")
                return
        ok = _signup_one(
            job_id,
            count=count,
            mode=mode,
            state=state,
            state_lock=state_lock,
            proxy=proxy,
            index=index,
            chain=chain,
            fixed_code=fixed_code,
            mail_provider=mail_provider,
            browser=browser,
        )
        if ok:
            continue
        rest = indices[pos + 1:]
        if not rest:
            return
        with state_lock:
            for skipped in rest:
                state["fail"] += 1
                state["finished"] += 1
                state["failures"].append(
                    f"TK #{skipped + 1}: bỏ qua, proxy {proxy or 'VPN'} đã lỗi ở TK trước"
                )
            running = list(state["running"])
            fails = list(state["failures"])
            text = (
                f"{state['ok']}/{count} TK được · {state['finished']} lần thử · {state['fail']} lỗi"
                + (f" · đang chạy {', '.join(running)}" if running else "")
            )
            note = state.get("invite_note") or ""
            if note and note not in text:
                text = note + "\n" + text
            if fails:
                text += "\n" + "\n".join(fails)
        if chain is not None:
            for skipped in rest:
                chain.publish(skipped, None)
        _set_job(
            job_id,
            {
                "status": "running",
                "step": "batch",
                "detail": text,
                "done_count": state["finished"],
                "ok_count": state["ok"],
                "fail_count": state["fail"],
                "failures": fails,
                "total": count,
            },
        )
        return


def _skip_lane_rest(job_id: str, count: int, state: dict, state_lock: threading.Lock, rest: list[int], chain: _InviteChain | None, reason: str) -> None:
    """Bỏ qua các tài khoản còn lại của 1 lane (ghi lỗi + mở khóa chuỗi mã mời)."""
    if not rest:
        return
    with state_lock:
        for skipped in rest:
            state["fail"] += 1
            state["finished"] += 1
            state["failures"].append(f"TK #{skipped + 1}: bỏ qua, {reason}")
        running = list(state["running"])
        fails = list(state["failures"])
        text = (
            f"{state['ok']}/{count} TK được · {state['finished']} lần thử · {state['fail']} lỗi"
            + (f" · đang chạy {', '.join(running)}" if running else "")
        )
        note = state.get("invite_note") or ""
        if note and note not in text:
            text = note + "\n" + text
        if fails:
            text += "\n" + "\n".join(fails)
    if chain is not None:
        for skipped in rest:
            chain.publish(skipped, None)
    _set_job(
        job_id,
        {
            "status": "running",
            "step": "batch",
            "detail": text,
            "done_count": state["finished"],
            "ok_count": state["ok"],
            "fail_count": state["fail"],
            "failures": fails,
            "total": count,
        },
    )


_PROXY_ERROR_HINTS = ("proxy", "err_", "timeout", "hết thời gian", "kết nối", "connection", "không tải", "muse chặn", "target page", "browser", "tunnel", "econn", "reset", "timed out")
_NO_PROXY_HINTS = ("kho thẻ", "thẻ", "card", "otp", "mã mời", "quy đổi", "đã dừng", "hộp thư", "captcha", "invite")


def _is_proxy_error(text: str) -> bool:
    """Lỗi có giống lỗi proxy/mạng không. Lỗi thẻ, OTP, mã mời, captcha thì
    không phải lỗi proxy nên không khóa proxy."""
    t = (text or "").lower()
    if any(h in t for h in _NO_PROXY_HINTS):
        return False
    return any(h in t for h in _PROXY_ERROR_HINTS)


class _ProxyPool:
    """Pool Kho proxy cho 1 lần chạy: phát proxy theo thứ tự đã dán, chỉ khóa
    khi lỗi khi chạy. Lượt dùng chỉ để đếm, không khóa proxy khi đủ lượt."""

    def __init__(self, records: list[dict], limit: int):
        self.limit = max(1, int(limit or 1))
        self._lock = threading.Lock()
        self._items = [
            {"id": str(r.get("id")), "proxy": str(r.get("proxy") or ""), "uses": int(r.get("uses") or 0)}
            for r in records
            if str(r.get("proxy") or "").strip()
        ]
        self._in_use: set[str] = set()
        self._dead: set[str] = set()

    def take(self) -> dict | None:
        with self._lock:
            for it in self._items:
                if it["id"] in self._in_use or it["id"] in self._dead:
                    continue
                self._in_use.add(it["id"])
                return dict(it)
            return None

    def release(self, proxy_id: str) -> None:
        with self._lock:
            self._in_use.discard(str(proxy_id))

    def bump(self, proxy_id: str) -> bool:
        """+1 lượt dùng sau 1 TK tạo thành công. Lượt dùng không khóa proxy
        nữa nên luôn trả False (lane giữ nguyên proxy đến khi lỗi mới đổi)."""
        with self._lock:
            rec = bump_proxy_use(proxy_id, self.limit)
            for it in self._items:
                if it["id"] == str(proxy_id):
                    it["uses"] = int((rec or {}).get("uses") or it["uses"] + 1)
                    break
            return False

    def fail(self, proxy_id: str, error: str = "") -> None:
        with self._lock:
            mark_proxy_error(proxy_id, error)
            self._dead.add(str(proxy_id))

    def exhausted(self) -> bool:
        """True khi pool còn proxy nhưng tất cả đều đã lỗi (không còn proxy dùng được)."""
        with self._lock:
            return bool(self._items) and len(self._dead) >= len(self._items)


def _run_signup_lane_pool(
    job_id: str,
    *,
    count: int,
    mode: str,
    state: dict,
    state_lock: threading.Lock,
    indices: list[int],
    chain: _InviteChain | None,
    fixed_code: str = "",
    pool: _ProxyPool,
    per_proxy: int,
    mail_provider: str = "tempmail",
    browser: str = "auto",
) -> None:
    """Một lane dùng Kho proxy: lấy proxy từ pool; proxy lỗi (lỗi mạng/proxy)
    thì khóa lại rồi đổi sang proxy mới và làm tiếp các TK còn lại. Lượt dùng
    không khóa proxy nữa nên lane giữ nguyên proxy đến khi lỗi mới đổi. Lỗi
    không phải do proxy (thẻ, OTP, mã mời...) thì dừng lane như cũ."""
    if not indices:
        return
    current = pool.take()
    if current is None:
        _skip_lane_rest(job_id, count, state, state_lock, indices, chain, "hết proxy trong kho")
        return
    for pos, index in enumerate(indices):
        if stop_requested():
            pool.release(current["id"])
            return
        with state_lock:
            if state.get("cards_exhausted"):
                pool.release(current["id"])
                _skip_lane_rest(job_id, count, state, state_lock, indices[pos:], chain, "Kho thẻ đã hết")
                return
        err_box: dict = {}
        ok = _signup_one(
            job_id,
            count=count,
            mode=mode,
            state=state,
            state_lock=state_lock,
            proxy=current["proxy"],
            index=index,
            chain=chain,
            fixed_code=fixed_code,
            err_box=err_box,
            mail_provider=mail_provider,
            browser=browser,
        )
        rest = indices[pos + 1:]
        if ok:
            pool.bump(current["id"])
            continue
        # Lỗi của chính lần thử này (không đọc state["last_error"] chung nữa:
        # lane khác có thể đã ghi đè nó bằng lỗi thẻ/OTP trước khi lane này
        # đọc, khiến proxy lỗi không bị khóa).
        last_error = (err_box.get("error") or "").strip()
        if not last_error:
            with state_lock:
                last_error = str(state.get("last_error") or "")
        if not _is_proxy_error(last_error):
            pool.release(current["id"])
            if rest:
                _skip_lane_rest(job_id, count, state, state_lock, rest, chain, f"proxy {current['proxy']} lỗi ở TK trước")
            return
        pool.fail(current["id"], last_error)
        pool.release(current["id"])
        with state_lock:
            state["failures"].append(f"Proxy {current['proxy']} lỗi — đã khóa, đổi sang proxy khác")
        if not rest:
            return
        nxt = pool.take()
        if nxt is None:
            _skip_lane_rest(job_id, count, state, state_lock, rest, chain, "hết proxy trong kho")
            return
        current = nxt
    pool.release(current["id"])


def _run_signup_batch(job_id: str, count: int, mode: str = "password", proxies: list[str] | None = None, seed: str = "", per_proxy: int = 5, max_workers: int = BATCH_PARALLEL, fixed_code: str = "", reset_stop: bool = True, finalize: bool = True, use_proxy_warehouse: bool = False, mail_provider: str = "tempmail", browser: str = "auto") -> None:
    """Chạy bù đến đủ số TK: count là số tài khoản TẠO THÀNH CÔNG cần có, không
    phải số lần thử. Tài khoản lỗi/bỏ qua được chạy bù ở vòng sau cho đến khi
    đủ số đó. Chỉ dừng khi: đủ số TK, người dùng bấm Dừng, Kho thẻ đã hết,
    hoặc Kho proxy đã lỗi hết — không tự dừng sau vài vòng trống nữa."""
    if reset_stop:
        clear_stop()
    fixed_code = (fixed_code or "").strip().upper()
    from mail_provider import normalize_provider

    mail_provider = normalize_provider(mail_provider)
    proxies = [line.strip() for line in (proxies or []) if line and line.strip()]
    seed = (seed or "").strip().upper()
    per_proxy = max(1, min(50, int(per_proxy or 5)))
    max_workers = max(1, min(MAX_WORKERS, int(max_workers or BATCH_PARALLEL)))
    set_signup_turn_gap(3)
    target = max(0, int(count or 0))
    state = {"ok": 0, "fail": 0, "finished": 0, "running": [], "last_error": "", "failures": [], "invite_note": "", "cards_exhausted": False, "steps": {}}
    state_lock = threading.Lock()
    if proxies:
        workers = min(len(proxies), max_workers)
        via = f" · {len(proxies)} proxy × {per_proxy} TK/proxy"
        if use_proxy_warehouse:
            via += " · Kho proxy"
    else:
        workers = _signup_workers(target, 0, max_workers)
        via = ""
    seed_note = f" · buff mã {fixed_code} cho cả {target} TK" if fixed_code else (f" · mã mời {seed} cho TK đầu" if seed else "")
    _set_job(
        job_id,
        {
            "status": "running",
            "step": "batch",
            "detail": f"Tạo {target} tài khoản" + via + seed_note + (f" · {workers} luồng" if workers > 1 else ""),
            "total": target,
            "done_count": 0,
            "ok_count": 0,
            "fail_count": 0,
        },
    )
    if target < 1:
        _set_job(job_id, {"status": "error", "step": "done", "detail": "Số tài khoản phải từ 1 trở lên.", "total": target, "done_count": 0, "ok_count": 0, "fail_count": 0})
        return
    rounds = 0
    no_progress = False
    stop_reason = ""
    used_pool = False
    while state["ok"] < target and not stop_requested():
        deficit = target - state["ok"]
        # Số lần thử tối đa mỗi vòng: proxy dán sẵn thì mỗi proxy tối đa
        # per_proxy TK/vòng; Kho proxy thì đọc lại danh sách còn dùng được.
        pool_records: list[dict] = []
        if use_proxy_warehouse:
            pool_records = usable_proxies(per_proxy)
            if pool_records:
                proxies = [str(p.get("proxy") or "") for p in pool_records if str(p.get("proxy") or "").strip()]
            elif used_pool:
                # Kho proxy đã cạn sau các vòng trước: dừng bù, không chạy trần.
                stop_reason = "proxies"
                no_progress = True
                break
        if proxies:
            round_count = min(deficit, len(proxies) * per_proxy)
        else:
            round_count = deficit
        if round_count < 1:
            no_progress = True
            break
        rounds += 1
        fail_before = len(state["failures"])
        proxy_pool = None
        round_seed = seed if rounds == 1 else latest_invite_code()
        chain = None if fixed_code else _InviteChain(round_count, round_seed)
        if proxies and use_proxy_warehouse:
            used_pool = True
            workers = min(len(proxies), max_workers)
            proxy_pool = _ProxyPool(pool_records, per_proxy)
            lane_indices = [list(range(j, round_count, workers)) for j in range(workers)]
            lane_indices = [idxs for idxs in lane_indices if idxs]
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(
                    pool.map(
                        lambda idxs: _run_signup_lane_pool(
                            job_id,
                            count=target,
                            mode=mode,
                            state=state,
                            state_lock=state_lock,
                            indices=idxs,
                            chain=chain,
                            fixed_code=fixed_code,
                            pool=proxy_pool,
                            per_proxy=per_proxy,
                            mail_provider=mail_provider,
                            browser=browser,
                        ),
                        lane_indices,
                    )
                )
        elif proxies:
            workers = min(len(proxies), max_workers)
            lanes = _signup_lanes(round_count, proxies, per_proxy)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(
                    pool.map(
                        lambda lane: _run_signup_lane(
                            job_id,
                            count=target,
                            mode=mode,
                            state=state,
                            state_lock=state_lock,
                            proxy=lane[0],
                            indices=lane[1],
                            chain=chain,
                            fixed_code=fixed_code,
                            mail_provider=mail_provider,
                            browser=browser,
                        ),
                        lanes,
                    )
                )
        else:
            workers = _signup_workers(round_count, 0, max_workers)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(
                    pool.map(
                        lambda index: _signup_one(
                            job_id,
                            count=target,
                            mode=mode,
                            state=state,
                            state_lock=state_lock,
                            proxy="",
                            index=index,
                            chain=chain,
                            fixed_code=fixed_code,
                            mail_provider=mail_provider,
                            browser=browser,
                        ),
                        range(round_count),
                    )
                )
        if chain is not None:
            try:
                set_invite_chain_code(chain.tail())
            except Exception:
                pass
        # Dừng ngay khi hết tài nguyên, khỏi chạy thêm vòng bù vô ích:
        # hết thẻ ("Kho thẻ đã hết" là bằng chứng chắc chắn, không còn thẻ
        # nào dùng được) hoặc Kho proxy lỗi hết (pool cạn).
        new_failures = list(state["failures"][fail_before:])
        if any("kho thẻ đã hết" in f.lower() for f in new_failures):
            stop_reason = "cards"
            no_progress = True
            break
        if proxy_pool is not None and proxy_pool.exhausted():
            stop_reason = "proxies"
            no_progress = True
            break
        with state_lock:
            state["failures"] = list(state["failures"][-20:])
        if state["ok"] < target and not stop_requested():
            _set_job(
                job_id,
                {
                    "status": "running",
                    "step": "batch",
                    "detail": f"Đã tạo {state['ok']}/{target} TK · đang bù thêm {target - state['ok']} TK (vòng {rounds + 1})",
                    "total": target,
                    "done_count": state["finished"],
                    "ok_count": state["ok"],
                    "fail_count": state["fail"],
                    "failures": list(state["failures"]),
                },
            )
    if not finalize:
        _set_job(
            job_id,
            {
                "status": "running",
                "step": "batch",
                "detail": f"Đã tạo {state['ok']}/{target} TK · {state['fail']} lỗi."
                + (
                    " — Kho thẻ đã hết."
                    if stop_reason == "cards"
                    else " — Proxy đã lỗi hết."
                    if stop_reason == "proxies"
                    else ""
                ),
                "total": target,
                "done_count": state["finished"],
                "ok_count": state["ok"],
                "fail_count": state["fail"],
                "failures": list(state["failures"]),
            },
        )
        return
    stopped = stop_requested()
    status = "error" if state["ok"] == 0 else "done"
    if stopped:
        summary = f"Đã dừng · {state['ok']}/{target} tài khoản được tạo."
    elif state["ok"] >= target:
        summary = f"Xong {state['ok']}/{target} tài khoản · {state['fail']} lỗi."
        if rounds > 1:
            summary += f" (đã chạy bù {rounds - 1} vòng)"
    elif no_progress:
        if stop_reason == "cards":
            summary = (
                f"Dừng bù · {state['ok']}/{target} tài khoản · {state['fail']} lỗi — "
                "Kho thẻ đã hết (các thẻ đều bị Muse báo lỗi hoặc đã đủ 10 lượt), không tạo thêm được TK nào. "
                "Thêm thẻ mới vào Kho thẻ rồi bấm Tạo lại để bù tiếp."
            )
        elif stop_reason == "proxies":
            summary = (
                f"Dừng bù · {state['ok']}/{target} tài khoản · {state['fail']} lỗi — "
                "Proxy đã lỗi hết (không còn proxy dùng được trong Kho proxy), không tạo thêm được TK nào. "
                "Thêm proxy mới vào Kho proxy rồi bấm Tạo lại để bù tiếp."
            )
        else:
            summary = (
                f"Dừng bù · {state['ok']}/{target} tài khoản · {state['fail']} lỗi — "
                "không tạo thêm được TK nào. Kiểm tra proxy/thẻ rồi bấm Tạo lại để bù tiếp."
            )
    else:
        summary = f"Xong {state['ok']}/{target} tài khoản · {state['fail']} lỗi."
    if state["failures"]:
        summary += "\n" + "\n".join(state["failures"])
    _set_job(
        job_id,
        {
            "status": status,
            "step": "done",
            "detail": summary,
            "total": target,
            "done_count": state["finished"],
            "ok_count": state["ok"],
            "fail_count": state["fail"],
            "failures": list(state["failures"]),
        },
    )


def _buff_code_from_name(name: str) -> str:
    """Kho buff đặt tên 'Kho - MÃ': lấy lại mã từ tên kho để chạy lại mã mời cho đúng mã."""
    name = (name or "").strip()
    if not name.startswith("Kho - "):
        return ""
    code = name[6:].strip().upper()
    return code if re.fullmatch(r"[A-Z0-9]{4,16}", code) else ""


def _run_buff_batch(job_id: str, codes: list[str], per_code: int, mode: str = "password", proxies: list[str] | None = None, per_proxy: int = 5, max_workers: int = BATCH_PARALLEL, mail_provider: str = "tempmail", browser: str = "auto") -> None:
    """Buff tài khoản: mỗi mã tạo per_code tài khoản mới, mọi tài khoản đều nhập đúng mã đó
    (không nối chuỗi như tạo thường), lấy cookie và lưu vào kho riêng tên 'Kho - MÃ'.
    Chạy lần lượt từng mã."""
    clear_stop()
    kind = "password" if mode == "password" else "plain"
    results: list[tuple[str, str, int, int]] = []
    total_ok = 0
    total_fail = 0
    total_done = 0
    for code in codes:
        if stop_requested():
            break
        # Kho buff riêng cho mã này: có rồi thì dùng lại (buff thêm), chưa có thì tạo mới.
        board = list_created_accounts(None, kind)
        existing = next((w for w in board.get("warehouses", []) if (w.get("name") or "") == f"Kho - {code}"), None)
        if existing:
            set_active_created_warehouse(existing["id"])
            wh_name = existing["name"]
        else:
            wh_name = create_created_warehouse(kind, name=f"Kho - {code}").get("name") or f"Kho - {code}"
        _run_signup_batch(
            job_id,
            count=per_code,
            mode=mode,
            proxies=proxies,
            per_proxy=per_proxy,
            max_workers=max_workers,
            fixed_code=code,
            reset_stop=False,
            finalize=False,
            mail_provider=mail_provider,
            browser=browser,
        )
        job = _get_job(job_id)
        ok = int(job.get("ok_count") or 0)
        fail = int(job.get("fail_count") or 0)
        results.append((code, wh_name, ok, fail))
        total_ok += ok
        total_fail += fail
        total_done += ok + fail
        _set_job(
            job_id,
            {
                "status": "running",
                "step": "batch",
                "detail": f"Buff {code}: {ok}/{ok + fail} được · tổng {total_ok} TK · {total_fail} lỗi",
                "total": len(codes) * per_code,
                "done_count": total_done,
                "ok_count": total_ok,
                "fail_count": total_fail,
            },
        )
    stopped = stop_requested()
    summary = ("Đã dừng · " if stopped else "") + f"Buff xong {total_ok} tài khoản · {total_fail} lỗi."
    for code, wh_name, ok, fail in results:
        summary += f"\n{wh_name}: {ok}/{ok + fail} được"
    _set_job(
        job_id,
        {
            "status": "done" if total_ok else "error",
            "step": "done",
            "detail": summary,
            "total": len(codes) * per_code,
            "done_count": total_done,
            "ok_count": total_ok,
            "fail_count": total_fail,
        },
    )


def _run_signup_job(job_id: str, mode: str = "random") -> None:
    from tempmail_plus_client import TempMailPlusError, create_han_mailbox, create_mailbox

    def on_progress(p: LoginProgress) -> None:
        _set_job(
            job_id,
            {
                "status": "running" if p.step not in ("done", "error") else p.step,
                "step": p.step,
                "detail": p.detail,
                "code": p.code,
                "error": p.error,
                "final_url": p.final_url,
            },
        )

    with_password = mode == "password"
    holder = {"email": ""}

    def prepare_mail() -> tuple[str, str, str | None]:
        if with_password:
            email, password, token = create_han_mailbox(ACCOUNT_PASSWORD, "mailto.plus")
        else:
            email, password, token = create_mailbox(ACCOUNT_PASSWORD)
        holder["email"] = email
        add_created_account(
            email,
            mailtm_password=password,
            kind="password" if with_password else "plain",
        )
        _set_job(
            job_id,
            {
                "status": "running",
                "step": "mail_saved",
                "email": email,
                "detail": f"Đã lưu {email}. Hộp thư đọc lại được. Bắt đầu tạo tài khoản.",
            },
        )
        return email, token, password if with_password else None

    try:
        result = create_muse_account(
            "",
            "",
            on_progress=on_progress,
            prepare_mail=prepare_mail,
        )
    except Exception as exc:
        _set_job(job_id, {"status": "error", "step": "error", "error": str(exc), "email": holder["email"]})
        return
    _set_job(
        job_id,
        {
            "status": "done" if result.step == "done" else "error",
            "step": result.step,
            "detail": result.detail,
            "error": result.error,
            "email": holder["email"],
            "code": result.code,
            "final_url": result.final_url,
        },
    )


@app.get("/api/cards")
def api_cards_list():
    return jsonify(list_card_warehouses())


@app.post("/api/cards")
def api_cards_add():
    body = request.get_json(force=True, silent=True) or {}
    try:
        card = add_card_warehouse(body.get("number") or "", body.get("expiry") or "", body.get("cvv") or "")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "card": card, **list_card_warehouses()})


@app.post("/api/cards/add-many")
def api_cards_add_many():
    body = request.get_json(force=True, silent=True) or {}
    if not str(body.get("text") or "").strip():
        return jsonify({"ok": False, "error": "Chưa dán thẻ nào."}), 400
    result = add_card_warehouses_bulk(body.get("text") or "")
    return jsonify({"ok": True, **result, **list_card_warehouses()})


@app.post("/api/cards/delete")
def api_cards_delete():
    body = request.get_json(force=True, silent=True) or {}
    if not delete_card_warehouse((body.get("id") or "").strip()):
        return jsonify({"ok": False, "error": "Không thấy kho thẻ"}), 404
    return jsonify({"ok": True, **list_card_warehouses()})


@app.post("/api/cards/delete-many")
def api_cards_delete_many():
    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "Chưa chọn thẻ."}), 400
    removed = delete_card_warehouses([str(item) for item in ids])
    if not removed:
        return jsonify({"ok": False, "error": "Không thấy thẻ."}), 404
    return jsonify({"ok": True, "removed": removed, **list_card_warehouses()})


@app.get("/api/proxies")
def api_proxies_list():
    return jsonify({"ok": True, "proxies": list_proxies()})


@app.post("/api/proxies")
def api_proxies_add():
    body = request.get_json(force=True, silent=True) or {}
    lines = body.get("lines") or body.get("proxies") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    result = add_proxies([str(item) for item in lines])
    return jsonify({"ok": True, **result, "proxies": list_proxies()})


@app.post("/api/proxies/delete")
def api_proxies_delete():
    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or ([body.get("id")] if body.get("id") else [])
    removed = delete_proxies([str(item) for item in ids])
    if not removed:
        return jsonify({"ok": False, "error": "Không thấy proxy."}), 404
    return jsonify({"ok": True, "removed": removed, "proxies": list_proxies()})


@app.post("/api/proxies/reset")
def api_proxies_reset():
    body = request.get_json(force=True, silent=True) or {}
    rec = reset_proxy((body.get("id") or "").strip())
    if not rec:
        return jsonify({"ok": False, "error": "Không thấy proxy."}), 404
    return jsonify({"ok": True, "proxy": rec, "proxies": list_proxies()})


@app.post("/api/proxies/reset-many")
def api_proxies_reset_many():
    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or []
    reset = reset_proxies([str(item) for item in ids])
    if not reset:
        return jsonify({"ok": False, "error": "Không thấy proxy lỗi nào."}), 404
    return jsonify({"ok": True, "reset": reset, "proxies": list_proxies()})


@app.get("/api/captcha")
def api_captcha_status():
    from captcha_solver import status_summary

    return jsonify({"ok": True, **status_summary()})


@app.post("/api/captcha")
def api_captcha_save():
    from captcha_solver import save_config, status_summary

    body = request.get_json(force=True, silent=True) or {}
    provider = str(body.get("provider") or "2captcha")
    api_key = str(body.get("api_key") or "")
    enabled = bool(body.get("enabled", True))
    if enabled and not api_key.strip():
        return jsonify({"ok": False, "error": "Chưa nhập API key."}), 400
    try:
        save_config(provider, api_key, enabled)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **status_summary()})


@app.get("/api/gmail")
def api_gmail_list():
    from gmail_store import gmail_summary

    return jsonify({"ok": True, **gmail_summary()})


@app.post("/api/gmail/add-many")
def api_gmail_add_many():
    from gmail_store import add_gmail_accounts

    body = request.get_json(force=True, silent=True) or {}
    if not str(body.get("text") or "").strip():
        return jsonify({"ok": False, "error": "Chưa dán Gmail nào."}), 400
    return jsonify({"ok": True, **add_gmail_accounts(body.get("text") or "")})


@app.post("/api/gmail/delete")
def api_gmail_delete():
    from gmail_store import delete_gmail_accounts, gmail_summary

    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or ([body.get("id")] if body.get("id") else [])
    removed = delete_gmail_accounts([str(item) for item in ids])
    if not removed:
        return jsonify({"ok": False, "error": "Không thấy Gmail."}), 404
    return jsonify({"ok": True, "removed": removed, **gmail_summary()})


@app.post("/api/gmail/reset-many")
def api_gmail_reset_many():
    from gmail_store import gmail_summary, reset_gmail_accounts

    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "Chưa chọn Gmail."}), 400
    reset = reset_gmail_accounts([str(item) for item in ids])
    if not reset:
        return jsonify({"ok": False, "error": "Không thấy Gmail."}), 404
    return jsonify({"ok": True, "reset": reset, **gmail_summary()})


@app.get("/api/yahoo")
def api_yahoo_list():
    from yahoo_store import yahoo_summary

    return jsonify({"ok": True, **yahoo_summary()})


@app.post("/api/yahoo/add-many")
def api_yahoo_add_many():
    from yahoo_store import add_yahoo_accounts

    body = request.get_json(force=True, silent=True) or {}
    if not str(body.get("text") or "").strip():
        return jsonify({"ok": False, "error": "Chưa dán Yahoo nào."}), 400
    return jsonify({"ok": True, **add_yahoo_accounts(body.get("text") or "")})


@app.post("/api/yahoo/delete")
def api_yahoo_delete():
    from yahoo_store import delete_yahoo_accounts, yahoo_summary

    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or ([body.get("id")] if body.get("id") else [])
    removed = delete_yahoo_accounts([str(item) for item in ids])
    if not removed:
        return jsonify({"ok": False, "error": "Không thấy Yahoo."}), 404
    return jsonify({"ok": True, "removed": removed, **yahoo_summary()})


@app.post("/api/yahoo/reset-many")
def api_yahoo_reset_many():
    from yahoo_store import reset_yahoo_accounts, yahoo_summary

    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "Chưa chọn Yahoo."}), 400
    reset = reset_yahoo_accounts([str(item) for item in ids])
    if not reset:
        return jsonify({"ok": False, "error": "Không thấy Yahoo."}), 404
    return jsonify({"ok": True, "reset": reset, **yahoo_summary()})


# --- Tài khoản META-MUSE (v133) ---
# Buoc 1: tao TK Meta bang IP may (KHONG proxy US) qua meta.ai, luu vao Kho
# TK Meta. Buoc 2: dung proxy US dang nhap muse.ai bang TK Meta da luu.
def _meta_publish(job_id: str, total: int, state: dict, state_lock: threading.Lock, unit: str = "TK Meta được") -> None:
    with state_lock:
        running = list(state["running"])
        fails = list(state["failures"])
        steps = dict(state.get("steps") or {})
        text = (
            f"{state['ok']}/{total} {unit} · {state['finished']} lần thử · {state['fail']} lỗi"
            + (f" · đang chạy {', '.join(running)}" if running else "")
        )
        step_lines = [f"{em}: {steps[em]}" for em in running if steps.get(em)]
        if step_lines:
            text += "\n" + "\n".join(step_lines)
        if fails:
            text += "\n" + "\n".join(fails)
    _set_job(
        job_id,
        {
            "status": "running",
            "step": "batch",
            "detail": text,
            "done_count": state["finished"],
            "ok_count": state["ok"],
            "fail_count": state["fail"],
            "failures": fails,
            "total": total,
        },
    )


def _meta_create_one(job_id: str, total: int, state: dict, state_lock: threading.Lock, mail_provider: str, browser: str, index: int, proxy: str = "") -> None:
    box = {"key": f"luồng {index}", "email": ""}

    def on_progress(p: LoginProgress) -> None:
        if p.step == "mail" and p.detail:
            m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", p.detail)
            if m and not box["email"]:
                old = box["key"]
                box["email"] = m.group(0)
                box["key"] = box["email"]
                with state_lock:
                    state["running"] = [box["key"] if x == old else x for x in state["running"]]
                    if old in (state.get("steps") or {}):
                        state["steps"][box["key"]] = state["steps"].pop(old)
        if p.detail:
            with state_lock:
                state.setdefault("steps", {})[box["key"]] = p.detail
        _meta_publish(job_id, total, state, state_lock)

    with state_lock:
        state["running"].append(box["key"])
    try:
        result = create_meta_account(
            mail_provider=mail_provider,
            browser_name=browser,
            account_password=ACCOUNT_PASSWORD,
            on_progress=on_progress,
            proxy=proxy or None,
        )
    except Exception as exc:
        result = LoginProgress(step="error", error=str(exc))
    with state_lock:
        state["running"] = [x for x in state["running"] if x != box["key"]]
        (state.get("steps") or {}).pop(box["key"], None)
        state["finished"] += 1
        if result.step == "done":
            state["ok"] += 1
            try:
                from meta_store import add_meta_account

                email = box["email"]
                if not email:
                    m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", result.detail or "")
                    email = m.group(0) if m else ""
                if email:
                    add_meta_account(email, ACCOUNT_PASSWORD, result.code or "")
            except Exception:
                pass
        else:
            state["fail"] += 1
            err = (result.error or "Không tạo được TK Meta").strip()
            line = f"{box['email'] or box['key']}: {err}"
            state["failures"].append(line)
            state["last_error"] = line
    _meta_publish(job_id, total, state, state_lock)


def _run_meta_create_batch(job_id: str, count: int, mail_provider: str, browser: str, max_workers: int, proxies: list[str] | None = None, per_proxy: int = 5) -> None:
    """Chạy bù đến đủ số TK Meta: count là số TK TẠO THÀNH CÔNG cần có.
    Chỉ dừng khi đủ số TK hoặc người dùng bấm Dừng (như batch signup).
    proxies rong thi dung IP may; co proxy thi chia theo lane
    (moi proxy lam per_proxy TK) nhu batch dang nhap."""
    clear_stop()
    from mail_provider import normalize_provider

    mail_provider = normalize_provider(mail_provider)
    max_workers = max(1, min(MAX_WORKERS, int(max_workers or BATCH_PARALLEL)))
    proxies = [p for p in (proxies or []) if p]
    per_proxy = max(1, min(50, int(per_proxy or 5)))
    proxy_note = f" · {len(proxies)} proxy ({per_proxy} TK/proxy)" if proxies else " · không dùng proxy (IP máy)"
    target = max(0, int(count or 0))
    state: dict = {"ok": 0, "fail": 0, "finished": 0, "running": [], "last_error": "", "failures": [], "steps": {}}
    state_lock = threading.Lock()
    _set_job(
        job_id,
        {
            "status": "running",
            "step": "batch",
            "detail": f"Tạo {target} tài khoản Meta{proxy_note}",
            "total": target,
            "done_count": 0,
            "ok_count": 0,
            "fail_count": 0,
        },
    )
    if target < 1:
        _set_job(job_id, {"status": "error", "step": "done", "detail": "Số tài khoản phải từ 1 trở lên.", "total": target, "done_count": 0, "ok_count": 0, "fail_count": 0})
        return
    while state["ok"] < target and not stop_requested():
        deficit = target - state["ok"]
        n = min(deficit, max_workers)
        base = state["finished"]
        round_proxies = [proxies[(base + k) // per_proxy % len(proxies)] if proxies else "" for k in range(n)]
        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(lambda i: _meta_create_one(job_id, target, state, state_lock, mail_provider, browser, i + 1, round_proxies[i]), range(n)))
    if state["ok"] >= target:
        _set_job(
            job_id,
            {
                "status": "done",
                "step": "done",
                "detail": f"Đã tạo {state['ok']} tài khoản Meta · {state['finished']} lần thử · {state['fail']} lỗi",
                "done_count": state["finished"],
                "ok_count": state["ok"],
                "fail_count": state["fail"],
                "failures": list(state["failures"]),
                "total": target,
            },
        )
    else:
        _set_job(
            job_id,
            {
                "status": "error",
                "step": "done",
                "detail": "Đã dừng tạo TK Meta.",
                "error": state.get("last_error") or "",
                "done_count": state["finished"],
                "ok_count": state["ok"],
                "fail_count": state["fail"],
                "failures": list(state["failures"]),
                "total": target,
            },
        )


def _meta_login_one(job_id: str, total: int, state: dict, state_lock: threading.Lock, account: dict, proxy: str, browser: str) -> None:
    email = (account.get("email") or "").strip()
    password = (account.get("password") or "").strip() or ACCOUNT_PASSWORD

    def on_progress(p: LoginProgress) -> None:
        if p.detail:
            with state_lock:
                state.setdefault("steps", {})[email] = p.detail
        _meta_publish(job_id, total, state, state_lock, unit="TK Muse đăng nhập được")

    with state_lock:
        state["running"].append(email)
    try:
        result = login_muse_with_meta(email, password, proxy=proxy, browser_name=browser, on_progress=on_progress)
    except Exception as exc:
        result = LoginProgress(step="error", error=str(exc))
    with state_lock:
        state["running"] = [x for x in state["running"] if x != email]
        (state.get("steps") or {}).pop(email, None)
        state["finished"] += 1
        if result.step == "done":
            state["ok"] += 1
        else:
            state["fail"] += 1
            err = (result.error or "Không đăng nhập được").strip()
            line = f"{email}: {err}"
            state["failures"].append(line)
            state["last_error"] = line
            # Đăng nhập lỗi thì mở lại TK về sẵn sàng để thử lại proxy khác.
            try:
                from meta_store import reset_meta_accounts

                reset_meta_accounts([email])
            except Exception:
                pass
    _meta_publish(job_id, total, state, state_lock, unit="TK Muse đăng nhập được")


def _run_meta_login_batch(job_id: str, count: int, proxies: list[str], per_proxy: int, max_workers: int, browser: str, use_proxy_warehouse: bool) -> None:
    """Đăng nhập muse.ai bằng TK Meta sẵn sàng trong kho, mỗi TK qua 1
    proxy US (mỗi proxy tối đa per_proxy TK)."""
    clear_stop()
    max_workers = max(1, min(MAX_WORKERS, int(max_workers or BATCH_PARALLEL)))
    per_proxy = max(1, min(50, int(per_proxy or 5)))
    proxies = [line.strip() for line in (proxies or []) if line and line.strip()]
    if use_proxy_warehouse:
        pool_records = usable_proxies(per_proxy)
        proxies = [str(p.get("proxy") or "") for p in pool_records if str(p.get("proxy") or "").strip()]
    target = max(0, int(count or 0))
    from meta_store import reset_meta_accounts, take_meta_account

    accounts = []
    for _ in range(target):
        acct = take_meta_account()
        if not acct:
            break
        accounts.append(acct)
    if not accounts:
        _set_job(job_id, {"status": "error", "step": "done", "detail": "Kho TK Meta đã hết — tạo TK Meta trước rồi đăng nhập.", "total": target, "done_count": 0, "ok_count": 0, "fail_count": 0})
        return
    if not proxies:
        reset_meta_accounts([a.get("email") or "" for a in accounts])
        _set_job(job_id, {"status": "error", "step": "done", "detail": "Chưa có proxy US — dán proxy US vào ô hoặc thêm vào Kho proxy.", "total": target, "done_count": 0, "ok_count": 0, "fail_count": 0})
        return
    state: dict = {"ok": 0, "fail": 0, "finished": 0, "running": [], "last_error": "", "failures": [], "steps": {}}
    state_lock = threading.Lock()
    total = len(accounts)
    _set_job(
        job_id,
        {
            "status": "running",
            "step": "batch",
            "detail": f"Đăng nhập Muse bằng {total} TK Meta · {len(proxies)} proxy US",
            "total": total,
            "done_count": 0,
            "ok_count": 0,
            "fail_count": 0,
        },
    )
    lanes = []
    for i, acct in enumerate(accounts):
        proxy = proxies[(i // per_proxy) % len(proxies)]
        lanes.append((acct, proxy))
    workers = min(len(lanes), max_workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda lane: _meta_login_one(job_id, total, state, state_lock, lane[0], lane[1], browser), lanes))
    if stop_requested():
        _set_job(job_id, {"status": "error", "step": "done", "detail": "Đã dừng đăng nhập.", "error": state.get("last_error") or "", "done_count": state["finished"], "ok_count": state["ok"], "fail_count": state["fail"], "failures": list(state["failures"]), "total": total})
        return
    _set_job(
        job_id,
        {
            "status": "done",
            "step": "done",
            "detail": f"Đã đăng nhập Muse {state['ok']}/{total} TK Meta · {state['fail']} lỗi",
            "done_count": state["finished"],
            "ok_count": state["ok"],
            "fail_count": state["fail"],
            "failures": list(state["failures"]),
            "total": total,
        },
    )


def _meta_muse_one(job_id: str, total: int, state: dict, state_lock: threading.Lock, mail_provider: str, browser: str, index: int, muse_proxy: str) -> None:
    """Mot TK Muse tron ven: tao TK Meta (mail temp, sinh nhat 18+,
    mat khau co dinh) roi dang nhap muse.ai bang TK Meta do. CA HAI
    BUOC DUNG CHUNG 1 PROXY US ngay tu dau (theo user 2026-11-12
    23:10 — mo trinh duyet la bo proxy US vao luon, khong tao Meta
    bang IP may roi moi doi proxy US). TK Meta luon duoc luu vao kho
    (de thu lai buoc 2 neu can)."""
    box = {"key": f"luồng {index}", "email": ""}

    def on_progress(p: LoginProgress) -> None:
        if p.step == "mail" and p.detail:
            m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", p.detail)
            if m and not box["email"]:
                old = box["key"]
                box["email"] = m.group(0)
                box["key"] = box["email"]
                with state_lock:
                    state["running"] = [box["key"] if x == old else x for x in state["running"]]
                    if old in (state.get("steps") or {}):
                        state["steps"][box["key"]] = state["steps"].pop(old)
        if p.detail:
            with state_lock:
                state.setdefault("steps", {})[box["key"]] = p.detail
        _meta_publish(job_id, total, state, state_lock, unit="TK Muse tạo được")

    with state_lock:
        state["running"].append(box["key"])
    # Buoc 1: tao TK Meta — dung luon proxy US (khong dung IP may).
    try:
        result = create_meta_account(
            mail_provider=mail_provider,
            browser_name=browser,
            account_password=ACCOUNT_PASSWORD,
            on_progress=on_progress,
            proxy=muse_proxy or None,
        )
    except Exception as exc:
        result = LoginProgress(step="error", error=str(exc))
    email = box["email"]
    if not email:
        m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", result.detail or "")
        email = m.group(0) if m else ""
    if result.step != "done" or not email:
        with state_lock:
            state["running"] = [x for x in state["running"] if x != box["key"]]
            (state.get("steps") or {}).pop(box["key"], None)
            state["finished"] += 1
            state["fail"] += 1
            err = (result.error or "Không tạo được TK Meta").strip()
            line = f"{email or box['key']}: {err}"
            state["failures"].append(line)
            state["last_error"] = line
        _meta_publish(job_id, total, state, state_lock, unit="TK Muse tạo được")
        return
    # Luu TK Meta vao kho (de thu lai buoc 2 neu dang nhap Muse loi).
    try:
        from meta_store import add_meta_account

        add_meta_account(email, ACCOUNT_PASSWORD, result.code or "")
    except Exception:
        pass
    # Buoc 2: dang nhap muse.ai bang TK Meta vua tao, qua proxy US.
    try:
        login_result = login_muse_with_meta(email, ACCOUNT_PASSWORD, proxy=muse_proxy, browser_name=browser, on_progress=on_progress)
    except Exception as exc:
        login_result = LoginProgress(step="error", error=str(exc))
    with state_lock:
        state["running"] = [x for x in state["running"] if x != box["key"]]
        (state.get("steps") or {}).pop(box["key"], None)
        state["finished"] += 1
        if login_result.step == "done":
            state["ok"] += 1
            # Dang nhap Muse xong thi TK Meta nay da dung (de kho khong
            # lay lai no o muc dang nhap lai).
            try:
                from meta_store import mark_meta_used

                mark_meta_used(email)
            except Exception:
                pass
        else:
            state["fail"] += 1
            err = (login_result.error or "Không đăng nhập được Muse").strip()
            line = f"{email}: Tạo Meta xong nhưng đăng nhập Muse lỗi — {err}"
            state["failures"].append(line)
            state["last_error"] = line
    _meta_publish(job_id, total, state, state_lock, unit="TK Muse tạo được")


def _run_meta_muse_batch(job_id: str, count: int, mail_provider: str, browser: str, max_workers: int, muse_proxies: list[str], muse_per_proxy: int, use_proxy_warehouse: bool) -> None:
    """Tao TK Muse tron ven: moi TK = tao Meta + dang nhap muse.ai,
    ca hai buoc dung chung 1 proxy US ngay tu dau. Bu lai den du so
    TK Muse."""
    clear_stop()
    from mail_provider import normalize_provider

    mail_provider = normalize_provider(mail_provider)
    max_workers = max(1, min(MAX_WORKERS, int(max_workers or BATCH_PARALLEL)))
    muse_proxies = [line.strip() for line in (muse_proxies or []) if line and line.strip()]
    if use_proxy_warehouse:
        pool_records = usable_proxies(muse_per_proxy)
        muse_proxies = [str(p.get("proxy") or "") for p in pool_records if str(p.get("proxy") or "").strip()]
    muse_per_proxy = max(1, min(50, int(muse_per_proxy or 5)))
    target = max(0, int(count or 0))
    state: dict = {"ok": 0, "fail": 0, "finished": 0, "running": [], "last_error": "", "failures": [], "steps": {}}
    state_lock = threading.Lock()
    _set_job(
        job_id,
        {
            "status": "running",
            "step": "batch",
            "detail": f"Tạo {target} tài khoản Muse (Meta + đăng nhập, proxy US ngay từ đầu)",
            "total": target,
            "done_count": 0,
            "ok_count": 0,
            "fail_count": 0,
        },
    )
    if target < 1:
        _set_job(job_id, {"status": "error", "step": "done", "detail": "Số tài khoản phải từ 1 trở lên.", "total": target, "done_count": 0, "ok_count": 0, "fail_count": 0})
        return
    if not muse_proxies:
        _set_job(job_id, {"status": "error", "step": "done", "detail": "Chưa có proxy US — dán proxy US vào ô hoặc thêm vào Kho proxy.", "total": target, "done_count": 0, "ok_count": 0, "fail_count": 0})
        return
    while state["ok"] < target and not stop_requested():
        deficit = target - state["ok"]
        n = min(deficit, max_workers)
        base = state["finished"]
        round_muse = [muse_proxies[(base + k) // muse_per_proxy % len(muse_proxies)] for k in range(n)]
        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(lambda i: _meta_muse_one(job_id, target, state, state_lock, mail_provider, browser, i + 1, round_muse[i]), range(n)))
    if state["ok"] >= target:
        _set_job(
            job_id,
            {
                "status": "done",
                "step": "done",
                "detail": f"Đã tạo {state['ok']} tài khoản Muse · {state['finished']} lần thử · {state['fail']} lỗi",
                "done_count": state["finished"],
                "ok_count": state["ok"],
                "fail_count": state["fail"],
                "failures": list(state["failures"]),
                "total": target,
            },
        )
    else:
        _set_job(
            job_id,
            {
                "status": "error",
                "step": "done",
                "detail": "Đã dừng tạo TK Muse.",
                "error": state.get("last_error") or "",
                "done_count": state["finished"],
                "ok_count": state["ok"],
                "fail_count": state["fail"],
                "failures": list(state["failures"]),
                "total": target,
            },
        )


@app.get("/api/meta")
def api_meta_list():
    from meta_store import meta_summary

    return jsonify({"ok": True, **meta_summary()})


@app.post("/api/meta/create")
def api_meta_create():
    from mail_provider import normalize_provider

    body = request.get_json(force=True, silent=True) or {}
    mail_provider = normalize_provider(body.get("mail_provider"))
    browser = normalize_browser(body.get("browser"))
    try:
        count = int(body.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        max_workers = int(body.get("max_workers") or BATCH_PARALLEL)
    except (TypeError, ValueError):
        max_workers = BATCH_PARALLEL
    max_workers = max(1, min(MAX_WORKERS, max_workers))
    proxies = [normalize_proxy_line(line) for line in (body.get("proxies") or "").splitlines() if normalize_proxy_line(line)]
    try:
        per_proxy = int(body.get("per_proxy") or 5)
    except (TypeError, ValueError):
        per_proxy = 5
    per_proxy = max(1, min(50, per_proxy))
    if count < 1 or count > SIGNUP_MAX:
        return jsonify({"ok": False, "error": f"Nhập số tài khoản từ 1 đến {SIGNUP_MAX}."}), 400
    proxy_note = f" · {len(proxies)} proxy ({per_proxy} TK/proxy)" if proxies else " · không dùng proxy (IP máy)"
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, {"status": "running", "step": "batch", "detail": f"Tạo {count} tài khoản Meta{proxy_note}", "total": count})
    thread = threading.Thread(target=_run_meta_create_batch, args=(job_id, count, mail_provider, browser, max_workers, proxies, per_proxy), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "count": count, "max_workers": max_workers})


@app.post("/api/meta/login")
def api_meta_login():
    body = request.get_json(force=True, silent=True) or {}
    browser = normalize_browser(body.get("browser"))
    try:
        count = int(body.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    proxies = [normalize_proxy_line(line) for line in (body.get("proxies") or "").splitlines() if normalize_proxy_line(line)]
    use_proxy_warehouse = not proxies
    try:
        per_proxy = int(body.get("per_proxy") or 5)
    except (TypeError, ValueError):
        per_proxy = 5
    per_proxy = max(1, min(50, per_proxy))
    try:
        max_workers = int(body.get("max_workers") or BATCH_PARALLEL)
    except (TypeError, ValueError):
        max_workers = BATCH_PARALLEL
    max_workers = max(1, min(MAX_WORKERS, max_workers))
    if count < 1 or count > SIGNUP_MAX:
        return jsonify({"ok": False, "error": f"Nhập số tài khoản từ 1 đến {SIGNUP_MAX}."}), 400
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, {"status": "running", "step": "batch", "detail": f"Đăng nhập Muse bằng {count} TK Meta", "total": count})
    thread = threading.Thread(target=_run_meta_login_batch, args=(job_id, count, proxies, per_proxy, max_workers, browser, use_proxy_warehouse), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "count": count})


@app.post("/api/meta-muse/create")
def api_meta_muse_create():
    """Tao TK Muse tron ven trong 1 nut: tao TK Meta (mail temp, sinh
    nhat 18+, mat khau co dinh) roi dang nhap muse.ai bang TK Meta do
    qua proxy US. TK Meta luon duoc luu vao kho."""
    from mail_provider import normalize_provider

    body = request.get_json(force=True, silent=True) or {}
    mail_provider = normalize_provider(body.get("mail_provider"))
    browser = normalize_browser(body.get("browser"))
    try:
        count = int(body.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        max_workers = int(body.get("max_workers") or BATCH_PARALLEL)
    except (TypeError, ValueError):
        max_workers = BATCH_PARALLEL
    max_workers = max(1, min(MAX_WORKERS, max_workers))
    muse_proxies = [normalize_proxy_line(line) for line in (body.get("muse_proxies") or "").splitlines() if normalize_proxy_line(line)]
    use_proxy_warehouse = not muse_proxies
    try:
        muse_per_proxy = int(body.get("muse_per_proxy") or 5)
    except (TypeError, ValueError):
        muse_per_proxy = 5
    muse_per_proxy = max(1, min(50, muse_per_proxy))
    if count < 1 or count > SIGNUP_MAX:
        return jsonify({"ok": False, "error": f"Nhập số tài khoản từ 1 đến {SIGNUP_MAX}."}), 400
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, {"status": "running", "step": "batch", "detail": f"Tạo {count} tài khoản Muse (Meta + đăng nhập)", "total": count})
    thread = threading.Thread(target=_run_meta_muse_batch, args=(job_id, count, mail_provider, browser, max_workers, muse_proxies, muse_per_proxy, use_proxy_warehouse), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "count": count, "max_workers": max_workers})


@app.post("/api/meta/delete")
def api_meta_delete():
    from meta_store import delete_meta_accounts, meta_summary

    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or ([body.get("id")] if body.get("id") else [])
    removed = delete_meta_accounts([str(item) for item in ids])
    if not removed:
        return jsonify({"ok": False, "error": "Không thấy TK Meta."}), 404
    return jsonify({"ok": True, "removed": removed, **meta_summary()})


@app.post("/api/meta/reset-many")
def api_meta_reset_many():
    from meta_store import meta_summary, reset_meta_accounts

    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "Chưa chọn TK Meta."}), 400
    reset = reset_meta_accounts([str(item) for item in ids])
    if not reset:
        return jsonify({"ok": False, "error": "Không thấy TK Meta."}), 404
    return jsonify({"ok": True, "reset": reset, **meta_summary()})


@app.get("/api/summary")
def api_summary():
    """Số lượng active/tổng cho sidebar: tài khoản có cookie / tổng tài khoản."""
    cookie_emails = {normalize_email_key(e) for e in list_all_cookies().keys()}

    def _count(rows: list[dict]) -> dict[str, int]:
        total = len(rows)
        active = sum(
            1 for r in rows if normalize_email_key(str(r.get("email") or "")) in cookie_emails
        )
        return {"active": active, "total": total}

    plain = list_all_created_accounts("plain")
    password = list_all_created_accounts("password")
    buff = [
        r
        for r in plain + password
        if (r.get("warehouse_name") or "").startswith("Kho - ")
    ]
    cards = list_card_warehouses()["warehouses"]
    proxies = list_proxies()
    try:
        from gmail_store import list_gmail_accounts as _list_gmail

        gmail_rows = _list_gmail()
    except Exception:
        gmail_rows = []
    try:
        from yahoo_store import list_yahoo_accounts as _list_yahoo

        yahoo_rows = _list_yahoo()
    except Exception:
        yahoo_rows = []
    try:
        from meta_store import list_meta_accounts as _list_meta

        meta_rows = _list_meta()
    except Exception:
        meta_rows = []
    return jsonify(
        {
            "ok": True,
            "accounts": _count(list_all_regular_accounts()),
            "created_plain": _count(plain),
            "created_password": _count(password),
            "buff": _count(buff),
            "cards": {"active": len(usable_card_warehouses()), "total": len(cards)},
            "proxies": {
                "active": sum(1 for p in proxies if p.get("status") == "ready"),
                "total": len(proxies),
            },
            "gmail": {
                "active": sum(1 for g in gmail_rows if g.get("status") == "ready"),
                "total": len(gmail_rows),
            },
            "yahoo": {
                "active": sum(1 for g in yahoo_rows if g.get("status") == "ready"),
                "total": len(yahoo_rows),
            },
            "meta": {
                "active": sum(1 for g in meta_rows if g.get("status") == "ready"),
                "total": len(meta_rows),
            },
        }
    )


@app.get("/api/created")
def api_created_accounts():
    warehouse_id = (request.args.get("warehouse_id") or "").strip() or None
    kind = "password" if request.args.get("kind") == "password" else "plain"
    return jsonify(list_created_accounts(warehouse_id, kind))


@app.post("/api/created/warehouses")
def api_created_warehouses_create():
    body = request.get_json(force=True, silent=True) or {}
    kind = "password" if body.get("kind") == "password" else "plain"
    wh = create_created_warehouse(kind, body.get("name"), max_accounts=body.get("max_accounts"))
    return jsonify({"ok": True, "warehouse": {"id": wh.get("id"), "name": wh.get("name"), "kind": kind, "max": wh.get("max")}, **list_created_accounts(wh.get("id"), kind)})


@app.post("/api/created/accounts/delete")
def api_created_accounts_delete():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip()
    kind = body.get("kind") or "plain"
    warehouse_id = (body.get("warehouse_id") or "").strip() or None
    if not email:
        return jsonify({"ok": False, "error": "Chưa chọn tài khoản"}), 400
    if not delete_created_account(email):
        return jsonify({"ok": False, "error": "Không thấy tài khoản"}), 404
    return jsonify({"ok": True, **list_created_accounts(warehouse_id, kind)})


@app.post("/api/created/warehouses/delete")
def api_created_warehouses_delete():
    body = request.get_json(force=True, silent=True) or {}
    result = delete_created_warehouse((body.get("id") or "").strip())
    if result is None:
        return jsonify({"ok": False, "error": "Không thấy kho tạo"}), 404
    kind = result.get("kind") or "plain"
    return jsonify({"ok": True, **result, **list_created_accounts(None, kind)})


@app.post("/api/created/active")
def api_created_active():
    body = request.get_json(force=True, silent=True) or {}
    warehouse_id = (body.get("id") or "").strip()
    kind = "password" if body.get("kind") == "password" else "plain"
    if not set_active_created_warehouse(warehouse_id):
        return jsonify({"ok": False, "error": "Không thấy kho tạo"}), 404
    return jsonify({"ok": True, **list_created_accounts(warehouse_id, kind)})


@app.post("/api/signup")
def api_signup():
    body = request.get_json(force=True, silent=True) or {}
    mode = "plain" if body.get("mode") == "plain" else "password"
    from mail_provider import normalize_provider

    mail_provider = normalize_provider(body.get("mail_provider"))
    browser = normalize_browser(body.get("browser"))
    raw_count = body.get("count")
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        count = 1 if raw_count in (None, "") else 0
    proxies = [normalize_proxy_line(line) for line in (body.get("proxies") or "").splitlines() if normalize_proxy_line(line)]
    invite_code = (body.get("invite_code") or "").strip().upper()
    if invite_code and not re.fullmatch(r"[A-Z0-9]{4,16}", invite_code):
        return jsonify({"ok": False, "error": "Mã mời chỉ gồm chữ và số, dài 4–16 ký tự."}), 400
    try:
        per_proxy = int(body.get("per_proxy") or 5)
    except (TypeError, ValueError):
        per_proxy = 5
    per_proxy = max(1, min(50, per_proxy))
    # Ô proxy để trống = dùng Kho proxy (dán sẵn trong kho, tool tự quản lý).
    use_proxy_warehouse = not proxies
    if use_proxy_warehouse:
        proxies = [p["proxy"] for p in usable_proxies(per_proxy)]
    try:
        max_workers = int(body.get("max_workers") or BATCH_PARALLEL)
    except (TypeError, ValueError):
        max_workers = BATCH_PARALLEL
    max_workers = max(1, min(MAX_WORKERS, max_workers))
    if count < 1 or count > SIGNUP_MAX:
        return jsonify({"ok": False, "error": f"Nhập số tài khoản từ 1 đến {SIGNUP_MAX}."}), 400
    # Số TK là số tài khoản TẠO THÀNH CÔNG cần có — tài khoản lỗi/bỏ qua được
    # chạy bù ở các vòng sau, nên không giới hạn số TK theo proxy × per_proxy nữa.
    proxies = proxies[:count]
    job_id = uuid.uuid4().hex[:12]
    workers = min(len(proxies), max_workers) if proxies else _signup_workers(count, 0, max_workers)
    _set_job(
        job_id,
        {
            "status": "running",
            "step": "batch",
            "detail": f"Tạo {count} tài khoản" + (f" · {workers} luồng" if workers > 1 else ""),
            "total": count,
        },
    )
    thread = threading.Thread(target=_run_signup_batch, args=(job_id, count, mode, proxies, invite_code, per_proxy, max_workers), kwargs={"use_proxy_warehouse": use_proxy_warehouse, "mail_provider": mail_provider, "browser": browser}, daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "count": count, "workers": workers, "per_proxy": per_proxy, "max_workers": max_workers})


@app.post("/api/buff")
def api_buff():
    body = request.get_json(force=True, silent=True) or {}
    mode = "plain" if body.get("mode") == "plain" else "password"
    from mail_provider import normalize_provider

    mail_provider = normalize_provider(body.get("mail_provider"))
    browser = normalize_browser(body.get("browser"))
    codes: list[str] = []
    for line in str(body.get("codes") or "").splitlines():
        code = (line or "").strip().upper()
        if not code:
            continue
        if not re.fullmatch(r"[A-Z0-9]{4,16}", code):
            return jsonify({"ok": False, "error": f"Mã mời không hợp lệ: {code} (chỉ chữ và số, 4–16 ký tự)."}), 400
        if code not in codes:
            codes.append(code)
    if not codes:
        return jsonify({"ok": False, "error": "Chưa nhập mã mời. Dán mỗi mã một dòng vào Kho code."}), 400
    if len(codes) > BUFF_MAX_CODES:
        return jsonify({"ok": False, "error": f"Tối đa {BUFF_MAX_CODES} mã một lần."}), 400
    try:
        per_code = int(body.get("per_code") or BUFF_MAX_PER_CODE)
    except (TypeError, ValueError):
        per_code = BUFF_MAX_PER_CODE
    per_code = max(1, min(BUFF_MAX_PER_CODE, per_code))
    proxies = [normalize_proxy_line(line) for line in (body.get("proxies") or "").splitlines() if normalize_proxy_line(line)]
    try:
        per_proxy = int(body.get("per_proxy") or 5)
    except (TypeError, ValueError):
        per_proxy = 5
    per_proxy = max(1, min(50, per_proxy))
    try:
        max_workers = int(body.get("max_workers") or BATCH_PARALLEL)
    except (TypeError, ValueError):
        max_workers = BATCH_PARALLEL
    max_workers = max(1, min(MAX_WORKERS, max_workers))
    per_code_actual = min(per_code, len(proxies) * per_proxy) if proxies else per_code
    total = len(codes) * per_code_actual
    job_id = uuid.uuid4().hex[:12]
    _set_job(
        job_id,
        {
            "status": "running",
            "step": "batch",
            "detail": f"Buff {len(codes)} mã · {per_code_actual} TK/mã · tối đa {total} tài khoản",
            "total": total,
        },
    )
    thread = threading.Thread(target=_run_buff_batch, args=(job_id, codes, per_code_actual, mode, proxies, per_proxy, max_workers, mail_provider, browser), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "total": total, "codes": codes, "per_code": per_code_actual})


@app.post("/api/login")
def api_login_start():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "Thiếu email"}), 400
    headless = bool(body.get("headless", False))
    close_after = bool(body.get("close_after", False))
    save_cookies = bool(body.get("save_cookies", True))
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, {"status": "running", "email": email})
    thread = threading.Thread(
        target=_run_login_job,
        args=(job_id, email, headless),
        kwargs={"close_after": close_after, "save_cookies": save_cookies},
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/api/cookies/vault")
def api_cookies_vault():
    wid = request.args.get("warehouse_id") or get_active_warehouse().get("id")
    wh = get_warehouse(wid) or get_active_warehouse()
    items: list[dict] = []
    for acc in load_accounts(wh["id"]):
        email = acc["email"]
        rec = get_account_cookies(email) or {}
        header = rec.get("cookie_header") or rec.get("muse_cookie_header") or ""
        token = rec.get("muse_token") or token_from_cookie_header(header)
        items.append(
            {
                "email": email,
                "has_cookie": bool(header),
                "saved_at": acc.get("cookie_saved_at") or rec.get("saved_at"),
                "cookie_header": header,
                "export_line": f"{email}|{header}" if header else "",
                "muse_token": token,
                "account_status": rec.get("account_status") or "",
                "login_error": rec.get("login_error") or "",
                "token_used_pct": rec.get("token_used_pct") or "",
                "token_left": rec.get("token_left") or "",
                "token_reset": rec.get("token_reset") or "",
                "token_checked_at": rec.get("token_checked_at") or "",
                "token_error": rec.get("token_error") or "",
            }
        )
    return jsonify(
        {
            "items": items,
            "total": len(items),
            "warehouse": {"id": wh["id"], "name": wh.get("name"), "max": get_accounts_per_warehouse()},
        }
    )


@app.get("/api/cookies")
def api_cookies_all():
    data = list_all_cookies()
    summary = {
        k: {
            "email": v.get("email", k),
            "saved_at": v.get("saved_at"),
            "count": v.get("count", 0),
            "has_cookie": bool(v.get("cookie_header")),
        }
        for k, v in data.items()
    }
    return jsonify({"cookies": summary})


@app.get("/api/cookies/export")
def api_cookies_export():
    fmt = request.args.get("format", "txt")
    filter_mode = request.args.get("filter", "has")
    search = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    scope = _scope_emails(request.args.get("warehouse_id"))
    if fmt in ("txt", "header", "pipe"):
        content = export_filtered_text(
            filter_mode=filter_mode or "has",
            search=search,
            limit=limit,
            scope_emails=scope if scope else None,
        )
        filename = f"muse-cookies-{filter_mode}-{min(limit, 500)}.txt"
        mimetype = "text/plain; charset=utf-8"
        if not content.strip():
            return jsonify({"ok": False, "error": "Không có cookie khớp bộ lọc."}), 404
        return Response(
            content,
            mimetype=mimetype,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    content, filename, mimetype = export_bulk(fmt)
    if not content.strip():
        return jsonify({"ok": False, "error": "Chưa có cookie nào để xuất. Hãy Lấy cookie trước."}), 404
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/cookies/export.txt")
def api_cookies_export_legacy():
    content, filename, mimetype = export_bulk("txt")
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/cookies/export/<path:email>")
def api_cookies_export_one(email: str):
    fmt = request.args.get("format", "txt")
    packed = export_for_email(email.strip(), fmt)
    if not packed:
        return jsonify({"ok": False, "error": "Chưa có cookie cho tài khoản này."}), 404
    content, filename, mimetype = packed
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/cookies/<path:email>")
def api_cookies_one(email: str):
    rec = get_account_cookies(email.strip())
    if not rec:
        return jsonify({"ok": False, "error": "Chưa có cookie — bấm Lấy cookie hoặc Tự đăng nhập."}), 404
    if request.args.get("brief") == "1":
        return jsonify(
            {
                "ok": True,
                "email": rec.get("email", email),
                "saved_at": rec.get("saved_at"),
                "count": rec.get("count", 0),
                "cookie_header": rec.get("cookie_header"),
                "muse_cookie_header": rec.get("muse_cookie_header"),
                "muse_token": rec.get("muse_token") or token_from_cookie_header(rec.get("cookie_header") or ""),
                "account_status": rec.get("account_status") or "",
                "login_error": rec.get("login_error") or "",
            }
        )
    return jsonify(
        {
            "ok": True,
            "email": rec.get("email", email),
            "saved_at": rec.get("saved_at"),
            "count": rec.get("count", 0),
            "cookie_header": rec.get("cookie_header"),
            "muse_cookie_header": rec.get("muse_cookie_header"),
            "cookies": rec.get("cookies", []),
        }
    )


@app.get("/api/login/<job_id>")
def api_login_status(job_id: str):
    with _jobs_lock:
        job = _login_jobs.get(job_id)
    if not job:
        return jsonify({"status": "unknown"}), 404
    return jsonify(job)


def _watch_files() -> list[str]:
    paths: list[str] = []
    templates = BUNDLE_DIR / "templates"
    if templates.is_dir():
        paths.extend(str(p) for p in templates.rglob("*") if p.is_file())
    for name in (
        "app.py",
        "warehouse_store.py",
        "cookie_store.py",
        "muse_automation.py",
        "lay2fa_client.py",
        "mailtm_client.py",
        "tempmail_plus_client.py",
        "mail_provider.py",
    ):
        p = BUNDLE_DIR / name
        if p.is_file():
            paths.append(str(p))
    return paths


def main() -> None:
    import os

    frozen = is_frozen()
    if frozen:
        os.environ["FLASK_RELOAD"] = "0"
    else:
        os.environ.setdefault("FLASK_RELOAD", "1")
    port = int(os.environ.get("PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    auto_reload = not frozen and os.environ.get("FLASK_RELOAD", "1") != "0"
    debug = auto_reload
    is_worker = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if frozen:
        print(f"Muse Manager: {url}")
        print(f"Dữ liệu kho/cookie: {DATA_DIR}")
        if os.environ.get("NO_BROWSER") != "1":
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        from waitress import serve

        serve(app, host="127.0.0.1", port=port, threads=8)
        return
    print(f"Muse Account Manager: {url}" + (" · auto-reload ON" if auto_reload else ""))
    if not is_worker and os.environ.get("NO_BROWSER") != "1":
        webbrowser.open(url)
    app.run(
        host="127.0.0.1",
        port=port,
        debug=debug,
        use_reloader=auto_reload,
        threaded=True,
        extra_files=_watch_files() if auto_reload else None,
    )


if __name__ == "__main__":
    main()
