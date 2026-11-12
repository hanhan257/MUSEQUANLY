"""Giai reCAPTCHA v2 bang dich vu ben ngoai (2Captcha / CapSolver).

Dung o buoc xac minh nguoi that (login_muse -> _drive_until_selfie): khi
luoi chon hinh mo ra, thay vi chi doi user giai tay, tool gui sitekey +
URL trang cho dich vu, doi token, chen token vao o g-recaptcha-response
roi bam Tiep tuc. Neu dich vu loi / het tien / khong tim thay sitekey,
tra ve False va buoc tiep tuc nhu cu (user giai tay trong Chrome duoc giu).

Key API luu o captcha_config.json (DATA_DIR, da gitignore) — KHONG commit,
KHONG gui qua chat. Chi stdlib + requests.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

import requests
from playwright.sync_api import Page

from app_paths import DATA_DIR

CONFIG_FILE = DATA_DIR / "captcha_config.json"
PROVIDERS = ("2captcha", "capsolver")
SOLVE_TIMEOUT = 180
POLL_INTERVAL = 5


class CaptchaSolverError(RuntimeError):
    pass


def load_config() -> dict:
    """Doc cau hinh giai captcha. Tra dict {provider, api_key, enabled}."""
    cfg = {"provider": "2captcha", "api_key": "", "enabled": True}
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return cfg
    if not isinstance(raw, dict):
        return cfg
    provider = str(raw.get("provider") or "2captcha").strip().lower()
    cfg["provider"] = provider if provider in PROVIDERS else "2captcha"
    cfg["api_key"] = str(raw.get("api_key") or "").strip()
    cfg["enabled"] = bool(raw.get("enabled", True))
    return cfg


def save_config(provider: str, api_key: str, enabled: bool = True) -> dict:
    """Ghi cau hinh giai captcha (key chi nam trong file nay, gitignore)."""
    provider = (provider or "2captcha").strip().lower()
    if provider not in PROVIDERS:
        raise CaptchaSolverError("Nha cung cap chi co 2captcha hoac capsolver.")
    cfg = {"provider": provider, "api_key": (api_key or "").strip(), "enabled": bool(enabled)}
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def configured() -> tuple[str, str] | None:
    """(provider, api_key) neu da bat va co key, khong thi None."""
    cfg = load_config()
    if not cfg.get("enabled"):
        return None
    key = cfg.get("api_key") or ""
    if not key:
        return None
    return cfg["provider"], key


def status_summary() -> dict:
    """Tom tat cho UI — KHONG bao gio tra key."""
    cfg = load_config()
    return {
        "provider": cfg["provider"],
        "enabled": bool(cfg["enabled"]),
        "has_key": bool(cfg.get("api_key")),
    }


def extract_sitekey(page: Page) -> dict | None:
    """Tim sitekey reCAPTCHA tren trang (ca main frame + iframe).

    Tra {"sitekey", "enterprise", "action"} hoac None neu khong thay.
    """
    sitekey = None
    enterprise = False
    action = None
    collect_js = """() => {
      const keys = [];
      document.querySelectorAll('[data-sitekey]').forEach(el => {
        const k = el.getAttribute('data-sitekey');
        if (k) keys.push(k);
      });
      document.querySelectorAll('[data-action]').forEach(el => {
        const a = el.getAttribute('data-action');
        if (a) keys.push('__action__:' + a);
      });
      // Ca iframe long ben trong frame nay (vd: Google recaptcha nam
      // trong iframe referer_frame.php cua Meta) — lay src de tim k=.
      document.querySelectorAll('iframe').forEach(el => {
        const s = el.getAttribute('src') || '';
        if (s) keys.push('__iframe__:' + s);
      });
      for (const s of document.querySelectorAll('script')) {
        const t = s.textContent || '';
        const m = t.match(/['"]sitekey['"]\\s*[:=]\\s*['"]([^'"]+)['"]/);
        if (m) keys.push(m[1]);
        const m2 = t.match(/enterprise\\.render\\([^,]+,\\s*\\{[^}]*sitekey\\s*[:=]\\s*['"]([^'"]+)['"]/);
        if (m2) keys.push(m2[1]);
      }
      return keys;
    }"""
    try:
        frames = list(page.frames)
    except Exception:
        return None
    for frame in frames:
        url = (frame.url or "")
        low = url.lower()
        if "recaptcha" in low:
            if "enterprise" in low:
                enterprise = True
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                if qs.get("k") and qs["k"][0]:
                    sitekey = qs["k"][0]
                if qs.get("action") and qs["action"][0]:
                    action = qs["action"][0]
            except Exception:
                pass
        try:
            found = frame.evaluate(collect_js)
        except Exception:
            found = []
        for item in found or []:
            if isinstance(item, str) and item.startswith("__action__:"):
                action = action or item[len("__action__:"):]
            elif isinstance(item, str) and item.startswith("__iframe__:"):
                # Iframe long ben trong (vd: anchor cua Google nam trong
                # referer_frame.php cua Meta) — lay k= tu src cua no.
                src = item[len("__iframe__:"):]
                low2 = src.lower()
                if "recaptcha" in low2:
                    if "enterprise" in low2:
                        enterprise = True
                    try:
                        qs2 = urllib.parse.parse_qs(urllib.parse.urlparse(src).query)
                        if qs2.get("k") and qs2["k"][0]:
                            sitekey = sitekey or qs2["k"][0]
                        if qs2.get("action") and qs2["action"][0]:
                            action = action or qs2["action"][0]
                    except Exception:
                        pass
            elif isinstance(item, str) and item:
                sitekey = sitekey or item
        if sitekey:
            return {"sitekey": sitekey, "enterprise": enterprise, "action": action}
    return None


# ---------------- 2Captcha ----------------

_2CAPTCHA_IN = "https://2captcha.com/in.php"
_2CAPTCHA_RES = "https://2captcha.com/res.php"


def _2captcha_submit(api_key: str, sitekey: str, page_url: str, enterprise: bool, action: str | None) -> str:
    params = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": page_url,
        "json": 1,
    }
    if enterprise:
        params["enterprise"] = 1
        if action:
            params["action"] = action
    try:
        resp = requests.get(_2CAPTCHA_IN, params=params, timeout=30)
        data = resp.json()
    except Exception as exc:
        raise CaptchaSolverError(f"2Captcha khong gui duoc yeu cau ({exc})")
    if not isinstance(data, dict) or data.get("status") != 1:
        raise CaptchaSolverError(f"2Captcha tu choi: {(data or {}).get('request', 'khong ro ly do')}")
    return str(data["request"])


def _2captcha_poll(api_key: str, captcha_id: str, timeout: int = SOLVE_TIMEOUT) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(
                _2CAPTCHA_RES,
                params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
                timeout=30,
            )
            data = resp.json()
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        if isinstance(data, dict) and data.get("status") == 1:
            return str(data["request"])
        if isinstance(data, dict) and data.get("request") not in (None, "CAPCHA_NOT_READY"):
            raise CaptchaSolverError(f"2Captcha loi: {data.get('request')}")
        time.sleep(POLL_INTERVAL)
    raise CaptchaSolverError("2Captcha het gio cho token (qua 180s).")


def _2captcha_balance(api_key: str) -> float | None:
    try:
        resp = requests.get(
            _2CAPTCHA_RES,
            params={"key": api_key, "action": "getbalance", "json": 1},
            timeout=20,
        )
        data = resp.json()
    except Exception:
        return None
    if isinstance(data, dict) and data.get("status") == 1:
        try:
            return float(data.get("request") or 0)
        except Exception:
            return None
    return None


# ---------------- CapSolver ----------------

_CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
_CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"
_CAPSOLVER_BALANCE = "https://api.capsolver.com/getBalance"


def _capsolver_submit(api_key: str, sitekey: str, page_url: str, enterprise: bool, action: str | None) -> str:
    task = {
        "type": "ReCaptchaV2EnterpriseTaskProxyLess" if enterprise else "ReCaptchaV2TaskProxyLess",
        "websiteURL": page_url,
        "websiteKey": sitekey,
    }
    if enterprise and action:
        task["pageAction"] = action
    try:
        resp = requests.post(_CAPSOLVER_CREATE, json={"clientKey": api_key, "task": task}, timeout=30)
        data = resp.json()
    except Exception as exc:
        raise CaptchaSolverError(f"CapSolver khong gui duoc yeu cau ({exc})")
    if not isinstance(data, dict) or data.get("errorId") != 0 or not data.get("taskId"):
        raise CaptchaSolverError(f"CapSolver tu choi: {(data or {}).get('errorDescription', 'khong ro ly do')}")
    return str(data["taskId"])


def _capsolver_poll(api_key: str, task_id: str, timeout: int = SOLVE_TIMEOUT) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.post(
                _CAPSOLVER_RESULT, json={"clientKey": api_key, "taskId": task_id}, timeout=30
            )
            data = resp.json()
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        if not isinstance(data, dict) or data.get("errorId") != 0:
            raise CaptchaSolverError(f"CapSolver loi: {(data or {}).get('errorDescription', 'khong ro ly do')}")
        status = data.get("status")
        if status == "ready":
            token = ((data.get("solution") or {}).get("gRecaptchaResponse") or "")
            if token:
                return str(token)
            raise CaptchaSolverError("CapSolver tra ve token rong.")
        if status == "failed":
            raise CaptchaSolverError("CapSolver giai that bai.")
        # "processing" / "idle" -> doi tiep
        time.sleep(POLL_INTERVAL)
    raise CaptchaSolverError("CapSolver het gio cho token (qua 180s).")


def _capsolver_balance(api_key: str) -> float | None:
    try:
        resp = requests.post(_CAPSOLVER_BALANCE, json={"clientKey": api_key}, timeout=20)
        data = resp.json()
    except Exception:
        return None
    if isinstance(data, dict) and data.get("errorId") == 0:
        try:
            return float(data.get("balance") or 0)
        except Exception:
            return None
    return None


# ---------------- Giai chung ----------------


def solve_recaptcha(page_url: str, sitekey: str, enterprise: bool = False, action: str | None = None, timeout: int = SOLVE_TIMEOUT) -> str:
    """Gui sitekey cho dich vu da cau hinh va doi token. Tra token."""
    conf = configured()
    if conf is None:
        raise CaptchaSolverError("Chua cau hinh key giai captcha (xem muc Giai captcha).")
    provider, api_key = conf
    if provider == "capsolver":
        balance = _capsolver_balance(api_key)
        if balance is not None and balance <= 0:
            raise CaptchaSolverError("CapSolver het tien — nap them roi thu lai.")
        task_id = _capsolver_submit(api_key, sitekey, page_url, enterprise, action)
        return _capsolver_poll(api_key, task_id, timeout=timeout)
    balance = _2captcha_balance(api_key)
    if balance is not None and balance <= 0:
        raise CaptchaSolverError("2Captcha het tien — nap them roi thu lai.")
    captcha_id = _2captcha_submit(api_key, sitekey, page_url, enterprise, action)
    return _2captcha_poll(api_key, captcha_id, timeout=timeout)


_INJECT_JS = """(token) => {
  let n = 0;
  document.querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response').forEach(el => {
    el.value = token;
    n++;
    // Bao cho JS cua trang biet gia tri da doi (React/Enterprise co
    // the chi nhan khi co su kien input/change).
    try { el.dispatchEvent(new Event('input', {bubbles: true})); } catch (e) {}
    try { el.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
  });
  try {
    const cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients) {
      for (const key of Object.keys(cfg.clients)) {
        const client = cfg.clients[key];
        const fire = (obj) => {
          if (!obj || typeof obj !== 'object') return;
          if (typeof obj.callback === 'function') { try { obj.callback(token); } catch (e) {} }
          for (const k of Object.keys(obj)) {
            const v = obj[k];
            if (v && typeof v === 'object' && typeof v.callback === 'function') {
              try { v.callback(token); } catch (e) {}
            }
          }
        };
        fire(client);
      }
    }
  } catch (e) {}
  return n;
}"""


def inject_token(page: Page, token: str) -> int:
    """Chen token vao moi o g-recaptcha-response tren trang. Tra so o da chen."""
    total = 0
    try:
        frames = list(page.frames)
    except Exception:
        return 0
    for frame in frames:
        try:
            n = frame.evaluate(_INJECT_JS, token)
            total += int(n or 0)
        except Exception:
            continue
    return total


def auto_solve_page(page: Page, timeout: int = SOLVE_TIMEOUT) -> tuple[bool, str]:
    """Giai captcha tren trang hien tai bang dich vu da cau hinh.

    Tra (True, ten dich vu) neu chen duoc token, (False, ly do) neu khong.
    Khong bao gio raise — loi duoc tra ve dang chu de buoc goi tu quyet.
    """
    conf = configured()
    if conf is None:
        return False, "chua cau hinh key giai captcha"
    provider, _key = conf
    label = "CapSolver" if provider == "capsolver" else "2Captcha"
    try:
        info = extract_sitekey(page)
    except Exception as exc:
        return False, f"khong doc duoc sitekey ({exc})"
    if not info or not info.get("sitekey"):
        return False, "khong tim thay sitekey tren trang"
    try:
        token = solve_recaptcha(
            page.url,
            info["sitekey"],
            enterprise=bool(info.get("enterprise")),
            action=info.get("action"),
            timeout=timeout,
        )
    except CaptchaSolverError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"loi giai captcha ({exc})"
    try:
        placed = inject_token(page, token)
    except Exception as exc:
        return False, f"khong chen duoc token ({exc})"
    if placed <= 0:
        return False, "khong chen duoc token vao trang"
    return True, label
