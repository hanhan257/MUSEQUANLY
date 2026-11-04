"""Muse AI account manager — local web UI."""

from __future__ import annotations

import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from cookie_store import (
    export_bulk,
    export_clipboard_text,
    export_filtered_text,
    export_for_email,
    get_account_cookies,
    list_all_cookies,
    token_from_cookie_header,
)
from lay2fa_client import get_latest_muse_code, parse_account_line
from muse_automation import LoginProgress, login_muse
from warehouse_store import (
    ACCOUNTS_PER_WAREHOUSE,
    create_warehouse,
    ensure_initialized,
    get_accounts_for_warehouse,
    get_active_warehouse,
    get_proxy_for_email,
    get_warehouse,
    import_split_append,
    import_split_replace,
    list_warehouses_summary,
    move_account_to_error,
    rename_warehouse,
    restore_account_from_error,
    save_active_accounts,
    set_active_warehouse,
    warehouse_emails,
)

ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = ROOT / "accounts.txt"
MUSE_LOGIN_URL = "https://muse.ai/"
APP_BUILD = "recovery-click-v22"
BATCH_PARALLEL = 5

ensure_initialized()

app = Flask(__name__)


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

    auto_reload = os.environ.get("FLASK_RELOAD", "1") != "0"
    return jsonify(
        {
            "build": APP_BUILD,
            "auto_reload": auto_reload,
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
    wh = create_warehouse(body.get("name"))
    return jsonify({"ok": True, "warehouse": wh, "summary": list_warehouses_summary()})


@app.patch("/api/warehouses/<warehouse_id>")
def api_warehouses_rename(warehouse_id: str):
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not rename_warehouse(warehouse_id, name):
        return jsonify({"ok": False, "error": "Không tìm thấy kho"}), 404
    return jsonify({"ok": True, "summary": list_warehouses_summary()})


@app.post("/api/warehouses/import-split")
def api_warehouses_import_split():
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("text", "")
    proxies_raw = body.get("proxies", "")
    mode = (body.get("mode") or "replace").strip().lower()
    if mode == "append":
        summary = import_split_append(raw, proxies_raw)
    else:
        summary = import_split_replace(raw, proxies_raw)
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
                "max": ACCOUNTS_PER_WAREHOUSE,
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
        email, lay2fa_url = parsed
        proxy = proxy_lines[index].strip() if index < len(proxy_lines) else ""
        index += 1
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({"email": email, "lay2fa_url": lay2fa_url, "proxy": proxy})
    active = get_active_warehouse()
    if active.get("kind") != "error" and len(rows) > ACCOUNTS_PER_WAREHOUSE:
        return jsonify(
            {"ok": False, "error": f"Mỗi kho tối đa {ACCOUNTS_PER_WAREHOUSE} tài khoản. Dùng Dán tài khoản để chia nhiều kho."}
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
        email, lay2fa_url = parsed
        key = email.lower()
        if key not in by_email:
            added += 1
        by_email[key] = {"email": email, "lay2fa_url": lay2fa_url}
    merged = list(by_email.values())
    if len(merged) > ACCOUNTS_PER_WAREHOUSE:
        return jsonify({"ok": False, "error": f"Kho hiện tại tối đa {ACCOUNTS_PER_WAREHOUSE} TK."}), 400
    save_active_accounts(merged)
    return jsonify({"ok": True, "added": added, "total": len(by_email)})


@app.get("/api/otp/<path:email>")
def api_otp(email: str):
    email = email.strip()
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
    result = login_muse(
        email,
        headless=headless,
        close_after=close_after,
        save_cookies=save_cookies,
        proxy=get_proxy_for_email(email) or None,
        on_progress=on_progress,
    )
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
    try:
        result = login_muse(
            email,
            headless=False,
            close_after=True,
            save_cookies=True,
            proxy=get_proxy_for_email(email) or None,
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
            with state_lock:
                running.add(email)
                active = sorted(running)
            publish(f"Đang chạy {len(active)}/{BATCH_PARALLEL} luồng · {', '.join(active)}")
            try:
                return _cookie_one(email)
            finally:
                with state_lock:
                    running.discard(email)

        if pending:
            with ThreadPoolExecutor(max_workers=BATCH_PARALLEL) as pool:
                futures = [pool.submit(run_one, email) for email in pending]
                for fut in as_completed(futures):
                    row = fut.result()
                    with state_lock:
                        rows.append(row)
                        if row.get("status") == "ok":
                            ok_count += 1
                        else:
                            fail_count += 1
                    publish(
                        f"Xong {ok_count + fail_count + skip_count}/{total} · còn {len(running)} luồng đang chạy"
                    )

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
        header = acc.get("has_cookie") and (get_account_cookies(email) or {}).get("cookie_header") or ""
        rec = get_account_cookies(email) or {}
        header = header or rec.get("cookie_header") or rec.get("muse_cookie_header") or ""
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
            }
        )
    return jsonify(
        {
            "items": items,
            "total": len(items),
            "warehouse": {"id": wh["id"], "name": wh.get("name"), "max": ACCOUNTS_PER_WAREHOUSE},
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
    templates = ROOT / "templates"
    if templates.is_dir():
        paths.extend(str(p) for p in templates.rglob("*") if p.is_file())
    for name in (
        "app.py",
        "warehouse_store.py",
        "cookie_store.py",
        "muse_automation.py",
        "lay2fa_client.py",
    ):
        p = ROOT / name
        if p.is_file():
            paths.append(str(p))
    return paths


def main() -> None:
    import os

    os.environ.setdefault("FLASK_RELOAD", "1")
    port = int(os.environ.get("PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    auto_reload = os.environ.get("FLASK_RELOAD", "1") != "0"
    debug = auto_reload
    is_worker = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
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
