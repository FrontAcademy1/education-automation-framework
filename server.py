from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

HOST = "127.0.0.1"
PORT = 8033
DEFAULT_TIMEOUT_MS = 15_000
MAX_LOG_QUEUE = 1000

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_SORT_KEYS"] = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

@dataclass
class AutomationState:
    running: bool = False
    paused: bool = False
    stop_requested: bool = False
    job_id: Optional[str] = None
    thread: Optional[threading.Thread] = None
    logs: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=MAX_LOG_QUEUE))
    lock: threading.RLock = field(default_factory=threading.RLock)

state = AutomationState()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(message: str, level: str = "info") -> None:
    record = {
        "time": now_iso(),
        "level": level.lower(),
        "message": str(message),
    }
    logging.log(
        {"info": logging.INFO, "success": logging.INFO, "warning": logging.WARNING,
         "error": logging.ERROR}.get(record["level"], logging.INFO),
        record["message"],
    )
    try:
        state.logs.put_nowait(record)
    except queue.Full:
        try:
            state.logs.get_nowait()
        except queue.Empty:
            pass
        try:
            state.logs.put_nowait(record)
        except queue.Full:
            pass


def validate_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("رابط المنصة مطلوب.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("الرابط يجب أن يبدأ بـ http:// أو https:// ويحتوي على نطاق صالح.")
    return value


def credentials_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username:
        raise ValueError("اسم المستخدم/الرقم مطلوب.")
    if not password:
        raise ValueError("كلمة المرور مطلوبة.")
    return username, password


async def safe_text(page: Page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=5_000))[:10_000]
    except Exception:
        return ""


async def inspect_page(page: Page) -> dict[str, Any]:
    """Non-destructive inspection of common educational UI elements."""
    result: dict[str, Any] = {
        "title": "",
        "url": page.url,
        "forms": [],
        "inputs": [],
        "buttons": [],
        "links": [],
    }

    try:
        result["title"] = await page.title()
    except Exception:
        pass

    try:
        inputs = page.locator("input, textarea, select")
        count = min(await inputs.count(), 100)
        for i in range(count):
            el = inputs.nth(i)
            try:
                result["inputs"].append({
                    "tag": await el.evaluate("(e) => e.tagName.toLowerCase()"),
                    "type": await el.get_attribute("type"),
                    "name": await el.get_attribute("name"),
                    "id": await el.get_attribute("id"),
                    "placeholder": await el.get_attribute("placeholder"),
                    "aria_label": await el.get_attribute("aria-label"),
                })
            except Exception:
                continue
    except Exception:
        pass

    try:
        buttons = page.locator("button, input[type=submit], input[type=button]")
        count = min(await buttons.count(), 100)
        for i in range(count):
            el = buttons.nth(i)
            try:
                result["buttons"].append({
                    "text": (await el.inner_text()).strip()[:200] if await el.evaluate(
                        "(e) => e.tagName.toLowerCase() !== 'input'"
                    ) else await el.get_attribute("value"),
                    "type": await el.get_attribute("type"),
                    "name": await el.get_attribute("name"),
                    "id": await el.get_attribute("id"),
                })
            except Exception:
                continue
    except Exception:
        pass

    try:
        links = page.locator("a[href]")
        count = min(await links.count(), 100)
        for i in range(count):
            el = links.nth(i)
            try:
                result["links"].append({
                    "text": (await el.inner_text()).strip()[:200],
                    "href": await el.get_attribute("href"),
                })
            except Exception:
                continue
    except Exception:
        pass

    return result


async def wait_if_paused() -> None:
    while True:
        with state.lock:
            if state.stop_requested:
                raise RuntimeError("تم طلب الإيقاف.")
            paused = state.paused
        if not paused:
            return
        await asyncio.sleep(0.25)


async def login_generic(page: Page, username: str, password: str) -> bool:
    """Best-effort login helper. It only fills likely credential fields and clicks a login control."""
    user_selectors = [
        'input[type="email"]',
        'input[name*="email" i]',
        'input[name*="user" i]',
        'input[id*="email" i]',
        'input[id*="user" i]',
        'input[autocomplete="username"]',
    ]
    pass_selectors = [
        'input[type="password"]',
        'input[name*="pass" i]',
        'input[id*="pass" i]',
        'input[autocomplete="current-password"]',
    ]
    login_selectors = [
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button:has-text("تسجيل الدخول")',
        'input[type="submit"]',
        'button[type="submit"]',
    ]

    user_field = None
    pass_field = None

    for selector in user_selectors:
        try:
            candidate = page.locator(selector).first
            if await candidate.is_visible(timeout=1_000):
                user_field = candidate
                break
        except Exception:
            continue

    for selector in pass_selectors:
        try:
            candidate = page.locator(selector).first
            if await candidate.is_visible(timeout=1_000):
                pass_field = candidate
                break
        except Exception:
            continue

    if not user_field or not pass_field:
        log("لم يتم العثور على حقول دخول قياسية؛ تم الاكتفاء بفحص الصفحة.", "warning")
        return False

    await user_field.fill(username)
    await pass_field.fill(password)

    for selector in login_selectors:
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=1_000):
                await button.click()
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                log("تم إرسال نموذج تسجيل الدخول القياسي.")
                return True
        except Exception:
            continue

    log("تم ملء بيانات الدخول، لكن لم يتم العثور على زر دخول قياسي.", "warning")
    return False


async def browser_job(url: str, username: str, password: str) -> None:
    playwright = None
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None

    try:
        log(f"بدء جلسة المتصفح: {url}")
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            ignore_https_errors=False,
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        log(f"تم فتح الصفحة: {await page.title() or page.url}", "success")

        await wait_if_paused()
        await login_generic(page, username, password)

        await wait_if_paused()
        data = await inspect_page(page)
        log(
            f"فحص الصفحة: {len(data['inputs'])} حقل إدخال، "
            f"{len(data['buttons'])} زر، {len(data['links'])} رابط."
        )

        # Deliberately non-destructive: this framework does not solve or submit
        # graded questions automatically.
        log("تم الانتهاء من الفحص. لا يتم إرسال إجابات أو تجاوز اختبارات تلقائياً.", "success")

    except PlaywrightTimeoutError as exc:
        log(f"انتهت مهلة المتصفح: {exc}", "error")
    except PlaywrightError as exc:
        log(f"خطأ Playwright: {exc}", "error")
    except Exception as exc:
        log(f"خطأ غير متوقع في مهمة المتصفح: {type(exc).__name__}: {exc}", "error")
    finally:
        for obj, name in [(context, "context"), (browser, "browser")]:
            if obj:
                try:
                    await obj.close()
                except Exception as exc:
                    log(f"تعذر إغلاق {name}: {exc}", "warning")
        if playwright:
            try:
                await playwright.stop()
            except Exception as exc:
                log(f"تعذر إيقاف Playwright: {exc}", "warning")


def requests_probe(url: str) -> dict[str, Any]:
    """Safe GET probe for APIs/public pages; no credential submission."""
    try:
        response = requests.get(
            url,
            timeout=(5, 15),
            allow_redirects=True,
            headers={"User-Agent": "EducationAutomationFramework/1.0"},
        )
        return {
            "status_code": response.status_code,
            "final_url": response.url,
            "content_type": response.headers.get("content-type", ""),
            "allow": response.headers.get("allow", ""),
            "bytes": len(response.content),
        }
    except requests.RequestException as exc:
        raise RuntimeError(f"فشل طلب HTTP: {exc}") from exc


def run_job(payload: dict[str, Any], job_id: str) -> None:
    try:
        url = validate_url(payload.get("url", ""))
        username, password = credentials_from_payload(payload)

        with state.lock:
            state.running = True
            state.paused = False
            state.stop_requested = False
            state.job_id = job_id

        log(f"المهمة {job_id} بدأت.")

        try:
            probe = requests_probe(url)
            status_code = probe["status_code"]
            if status_code == 405:
                allow = f" | Allow: {probe['allow']}" if probe.get("allow") else ""
                log(
                    f"HTTP probe: 405 Method Not Allowed{allow} — "
                    "تم تجاهلها وسيستمر تشغيل المتصفح.",
                    "warning",
                )
            else:
                log(
                    f"HTTP probe: {status_code} | "
                    f"{probe['content_type']} | {probe['bytes']} bytes"
                )
        except Exception as exc:
            log(f"تعذر فحص HTTP الأولي: {exc}", "warning")

        asyncio.run(browser_job(url, username, password))
        log(f"المهمة {job_id} انتهت.", "success")

    except Exception as exc:
        log(f"فشل تشغيل المهمة: {type(exc).__name__}: {exc}", "error")
    finally:
        with state.lock:
            state.running = False
            state.paused = False
            state.stop_requested = False
            state.thread = None


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    with state.lock:
        return jsonify({
            "running": state.running,
            "paused": state.paused,
            "job_id": state.job_id,
        })


@app.post("/api/start")
def start():
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400

    payload = request.get_json(silent=True) or {}
    try:
        validate_url(payload.get("url", ""))
        credentials_from_payload(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with state.lock:
        if state.running:
            return jsonify({"error": "توجد مهمة تعمل بالفعل.", "job_id": state.job_id}), 409

        job_id = str(uuid.uuid4())
        thread = threading.Thread(
            target=run_job,
            args=(payload, job_id),
            daemon=True,
            name=f"automation-{job_id[:8]}",
        )
        state.thread = thread
        state.running = True
        state.paused = False
        state.stop_requested = False
        state.job_id = job_id
        thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.post("/api/pause")
def pause():
    with state.lock:
        if not state.running:
            return jsonify({"error": "لا توجد مهمة تعمل."}), 409
        state.paused = True
    log("تم طلب الإيقاف المؤقت.", "warning")
    return jsonify({"ok": True, "paused": True})


@app.post("/api/resume")
def resume():
    with state.lock:
        if not state.running:
            return jsonify({"error": "لا توجد مهمة تعمل."}), 409
        state.paused = False
    log("تم استئناف المهمة.")
    return jsonify({"ok": True, "paused": False})


@app.post("/api/stop")
def stop():
    with state.lock:
        if not state.running:
            return jsonify({"error": "لا توجد مهمة تعمل."}), 409
        state.stop_requested = True
        state.paused = False
    log("تم طلب الإيقاف النهائي؛ ستتوقف المهمة عند أقرب نقطة آمنة.", "warning")
    return jsonify({"ok": True})


@app.get("/api/logs")
def logs():
    def generate():
        # Send a heartbeat-compatible SSE stream. The browser can reconnect safely.
        while True:
            try:
                record = state.logs.get(timeout=15)
                yield f"data: {json.dumps(record, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"
            except GeneratorExit:
                break
            except Exception as exc:
                logging.exception("SSE error: %s", exc)
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    logging.exception("Unhandled Flask exception")
    return jsonify({"error": "حدث خطأ داخلي غير متوقع.", "type": type(exc).__name__}), 500


if __name__ == "__main__":
    log(f"تشغيل الخادم على http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
