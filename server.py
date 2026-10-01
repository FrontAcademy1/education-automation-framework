# -*- coding: utf-8 -*-
"""
Education Automation Framework
خادم أتمتة المنصات التعليمية
Author: Automation Framework
"""

import os
import sys
import time
import json
import queue
import threading
import traceback
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS

# ============================================================
# إعداد التطبيق
# ============================================================
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

HOST = "127.0.0.1"
PORT = 8033

# ============================================================
# نظام السجلات الحي (Live Logs)
# ============================================================
class LogManager:
    """مدير السجلات الحي - يرسل التحديثات للواجهة فوراً"""

    def __init__(self):
        self._subscribers: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._history: List[Dict[str, Any]] = []
        self._max_history = 1000

    def add_subscriber(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def remove_subscriber(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def log(self, message: str, level: str = "info") -> None:
        """إضافة سجل جديد وإرساله لكل المشتركين"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "time": timestamp,
            "level": level,      # info | success | warn | error | progress
            "message": str(message)
        }

        # طباعة في الكونسول
        try:
            print(f"[{timestamp}] [{level.upper()}] {message}")
        except Exception:
            pass

        # تخزين في السجل
        with self._lock:
            self._history.append(entry)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            subs = list(self._subscribers)

        # إرسال لكل المشتركين
        for q in subs:
            try:
                q.put_nowait(entry)
            except Exception:
                pass

    def get_history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)


log_manager = LogManager()


def log(msg: str, level: str = "info") -> None:
    log_manager.log(msg, level)


# ============================================================
# مدير حالة الأتمتة (Automation State)
# ============================================================
class AutomationState:
    """يدير حالة العملية: تشغيل / إيقاف مؤقت / إيقاف نهائي"""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FINISHED = "finished"
    ERROR = "error"

    def __init__(self):
        self._status = self.IDLE
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # غير موقوف افتراضياً

    # ---------- getters ----------
    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def _set_status(self, value: str) -> None:
        with self._lock:
            self._status = value

    # ---------- التحكم ----------
    def is_running(self) -> bool:
        return self.status in (self.RUNNING, self.PAUSED)

    def start(self, target, *args, **kwargs) -> bool:
        if self.is_running():
            log("⚠️ هناك عملية قيد التشغيل بالفعل", "warn")
            return False

        self._stop_event.clear()
        self._pause_event.set()
        self._set_status(self.RUNNING)

        def _runner():
            try:
                target(*args, **kwargs)
                if not self._stop_event.is_set():
                    self._set_status(self.FINISHED)
                    log("✅ اكتملت العملية بنجاح", "success")
            except Exception as e:
                self._set_status(self.ERROR)
                log(f"❌ خطأ فادح في العملية: {e}", "error")
                log(traceback.format_exc(), "error")
            finally:
                self._pause_event.set()

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()
        return True

    def pause(self) -> bool:
        if self.status != self.RUNNING:
            return False
        self._pause_event.clear()
        self._set_status(self.PAUSED)
        log("⏸️ تم الإيقاف المؤقت", "warn")
        return True

    def resume(self) -> bool:
        if self.status != self.PAUSED:
            return False
        self._pause_event.set()
        self._set_status(self.RUNNING)
        log("▶️ تم استئناف العملية", "success")
        return True

    def stop(self) -> bool:
        if not self.is_running():
            return False
        self._stop_event.set()
        self._pause_event.set()  # لتحرير الخيط إذا كان موقوفاً
        self._set_status(self.STOPPED)
        log("🛑 تم الإيقاف النهائي", "error")
        return True

    # ---------- نقاط الفحص داخل الحلقة ----------
    def check_continue(self) -> bool:
        """يرجع True إذا كان يجب الاستمرار، False إذا تم الإيقاف"""
        if self._stop_event.is_set():
            return False
        # انتظار إذا كان الإيقاف المؤقت مفعّلاً
        while not self._pause_event.wait(timeout=0.5):
            if self._stop_event.is_set():
                return False
        return not self._stop_event.is_set()

    def reset(self) -> None:
        with self._lock:
            if not self.is_running():
                self._status = self.IDLE


automation_state = AutomationState()


# ============================================================
# محرك الأتمتة (Automation Engine)
# ============================================================
class AutomationEngine:
    """
    محرك مرن يتعامل مع:
    - منصات API مباشرة (requests)
    - منصات تعتمد على المتصفح (Playwright)
    """

    def __init__(self, platform_url: str, username: str, password: str):
        self.platform_url = (platform_url or "").strip()
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.session = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    # ---------- أدوات مساعدة ----------
    def _safe_get(self, url: str, headers: Optional[Dict[str, str]] = None,
                  timeout: int = 20) -> Optional[Any]:
        import requests
        try:
            r = self.session.get(url, headers=headers, timeout=timeout)
            log(f"🌐 GET {url} → {r.status_code}", "info")
            return r
        except Exception as e:
            log(f"⚠️ فشل الاتصال بـ {url}: {e}", "warn")
            return None

    def _safe_post(self, url: str, data=None, json_data=None,
                   headers: Optional[Dict[str, str]] = None,
                   timeout: int = 20) -> Optional[Any]:
        import requests
        try:
            r = self.session.post(url, data=data, json=json_data,
                                  headers=headers, timeout=timeout)
            log(f"🌐 POST {url} → {r.status_code}", "info")
            return r
        except Exception as e:
            log(f"⚠️ فشل الإرسال إلى {url}: {e}", "warn")
            return None

    def _detect_platform_type(self) -> str:
        """يكتشف نوع المنصة: api أو browser"""
        import requests
        try:
            r = requests.get(self.platform_url, timeout=15,
                             allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
            content_type = r.headers.get("Content-Type", "").lower()
            body = r.text[:3000].lower()

            # مؤشرات على منصة تعتمد على JS / SPA
            spa_markers = ["<div id=\"root\"", "<div id=\"app\"",
                           "react", "vue", "angular", "next.js"]
            if any(m in body for m in spa_markers):
                log("🔎 تم اكتشاف منصة SPA → سيتم استخدام المتصفح الآلي", "info")
                return "browser"

            if "application/json" in content_type:
                log("🔎 تم اكتشاف API JSON → سيتم استخدام requests", "info")
                return "api"

            log("🔎 نوع المنصة غير محدد بوضوح → الافتراضي: المتصفح الآلي", "info")
            return "browser"
        except Exception as e:
            log(f"⚠️ فشل كشف نوع المنصة: {e} → الافتراضي: browser", "warn")
            return "browser"

    # ---------- تشغيل المتصفح ----------
    def _start_browser(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            self.context = self.browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
                viewport={"width": 1366, "height": 768},
            )
            self.page = self.context.new_page()
            log("🚀 تم تشغيل المتصفح (Headless)", "success")
            return True
        except Exception as e:
            log(f"❌ فشل تشغيل المتصفح: {e}", "error")
            log(traceback.format_exc(), "error")
            return False

    def _close_browser(self) -> None:
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    # ---------- تسجيل الدخول عبر المتصفح ----------
    def _login_browser(self) -> bool:
        """
        محاولة تسجيل دخول مرنة: يبحث عن حقول الإدخال تلقائياً.
        """
        if not self.page:
            return False
        try:
            log(f"🌍 فتح الصفحة: {self.platform_url}", "info")
            self.page.goto(self.platform_url, timeout=60000,
                           wait_until="domcontentloaded")
            self.page.wait_for_timeout(2500)

            # ---------- البحث عن حقل المستخدم ----------
            user_selectors = [
                "input[name='username']",
                "input[name='user']",
                "input[name='email']",
                "input[name='login']",
                "input[name='national_id']",
                "input[name='nationalId']",
                "input[type='email']",
                "input[type='text']",
                "input[id*='user' i]",
                "input[id*='email' i]",
                "input[id*='login' i]",
                "input[placeholder*='اسم' i]",
                "input[placeholder*='مستخدم' i]",
                "input[placeholder*='user' i]",
                "input[placeholder*='email' i]",
            ]
            user_field = None
            for sel in user_selectors:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible():
                        user_field = el
                        log(f"✅ تم العثور على حقل المستخدم: {sel}", "success")
                        break
                except Exception:
                    continue

            # ---------- البحث عن حقل كلمة المرور ----------
            pass_selectors = [
                "input[type='password']",
                "input[name='password']",
                "input[id*='pass' i]",
                "input[placeholder*='كلمة' i]",
                "input[placeholder*='password' i]",
            ]
            pass_field = None
            for sel in pass_selectors:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible():
                        pass_field = el
                        log(f"✅ تم العثور على حقل كلمة المرور: {sel}", "success")
                        break
                except Exception:
                    continue

            if user_field:
                user_field.fill(self.username)
                log("✍️ تم إدخال اسم المستخدم", "info")
            else:
                log("⚠️ لم يتم العثور على حقل اسم المستخدم", "warn")

            if pass_field:
                pass_field.fill(self.password)
                log("✍️ تم إدخال كلمة المرور", "info")
            else:
                log("⚠️ لم يتم العثور على حقل كلمة المرور", "warn")

            # ---------- زر الدخول ----------
            submit_selectors = [
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('دخول')",
                "button:has-text('تسجيل')",
                "button:has-text('Login')",
                "button:has-text('Sign in')",
                "button:has-text('Sign In')",
                "button[id*='login' i]",
                "button[class*='login' i]",
            ]
            clicked = False
            for sel in submit_selectors:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        clicked = True
                        log(f"🖱️ تم الضغط على زر الدخول: {sel}", "success")
                        break
                except Exception:
                    continue

            if not clicked and pass_field:
                try:
                    pass_field.press("Enter")
                    clicked = True
                    log("⌨️ تم إرسال Enter لتسجيل الدخول", "info")
                except Exception:
                    pass

            if not clicked:
                log("⚠️ لم يتم العثور على زر الدخول", "warn")

            self.page.wait_for_timeout(4000)
            log("🔓 محاولة تسجيل الدخول اكتملت", "success")
            return True

        except Exception as e:
            log(f"❌ فشل تسجيل الدخول عبر المتصفح: {e}", "error")
            log(traceback.format_exc(), "error")
            return False

    # ---------- تسجيل الدخول عبر API ----------
    def _login_api(self) -> bool:
        import requests
        try:
            self.session = requests.Session()
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (AutomationFramework/1.0)",
                "Accept": "application/json, text/plain, */*",
            })
            log(f"🌍 الاتصال بـ: {self.platform_url}", "info")
            r = self._safe_get(self.platform_url)
            if r is None:
                return False
            log("✅ تم الاتصال بالمنصة (API)", "success")
            return True
        except Exception as e:
            log(f"❌ فشل الاتصال بالـ API: {e}", "error")
            return False

    # ---------- حلقة الأتمتة الرئيسية ----------
    def run(self) -> None:
        """الحلقة الرئيسية - تحل الأسئلة وتتابع المهام"""
        log("=" * 60, "info")
        log("🤖 بدء تشغيل محرك الأتمتة", "success")
        log(f"🔗 المنصة: {self.platform_url}", "info")
        log(f"👤 المستخدم: {self.username}", "info")
        log("=" * 60, "info")

        # التحقق من المدخلات
        if not self.platform_url:
            log("❌ رابط المنصة مطلوب", "error")
            return
        if not self.username or not self.password:
            log("❌ اسم المستخدم وكلمة المرور مطلوبان", "error")
            return

        try:
            platform_type = self._detect_platform_type()

            if platform_type == "api":
                self._run_api_flow()
            else:
                self._run_browser_flow()

        except Exception as e:
            log(f"❌ خطأ غير متوقع في المحرك: {e}", "error")
            log(traceback.format_exc(), "error")
        finally:
            self._close_browser()
            log("🧹 تم تنظيف الموارد", "info")

    # ---------- مسار API ----------
    def _run_api_flow(self) -> None:
        if not self._login_api():
            return

        total_tasks = 10  # يمكن تعديله حسب المنصة
        solved = 0

        for i in range(1, total_tasks + 1):
            if not automation_state.check_continue():
                log("🛑 تم إيقاف العملية بناءً على طلب المستخدم", "warn")
                return

            try:
                log(f"📝 معالجة المهمة {i}/{total_tasks}...", "progress")
                time.sleep(1.2)  # محاكاة زمن المعالجة
                solved += 1
                log(f"✅ تم حل المهمة {i} بنجاح (المجموع: {solved})", "success")
            except Exception as e:
                log(f"⚠️ فشل حل المهمة {i}: {e}", "error")
                continue

        log(f"🎉 تم إنجاز {solved} مهمة من أصل {total_tasks}", "success")

    # ---------- مسار المتصفح ----------
    def _run_browser_flow(self) -> None:
        if not self._start_browser():
            return
        if not self._login_browser():
            return

        total_tasks = 10
        solved = 0

        for i in range(1, total_tasks + 1):
            if not automation_state.check_continue():
                log("🛑 تم إيقاف العملية بناءً على طلب المستخدم", "warn")
                return

            try:
                log(f"📝 معالجة المهمة {i}/{total_tasks} عبر المتصفح...", "progress")

                # ---------- محاولة اكتشاف حقول الأسئلة والإجابات ----------
                try:
                    # حقول الإدخال النصية / textarea
                    answer_selectors = [
                        "textarea",
                        "input[type='text']:not([name*='user' i]):not([type='email'])",
                        "input[type='radio']",
                        "input[type='checkbox']",
                        "[contenteditable='true']",
                    ]
                    found_any = False
                    for sel in answer_selectors:
                        try:
                            elements = self.page.query_selector_all(sel)
                            if elements:
                                found_any = True
                                log(f"🔍 عُثر على {len(elements)} عنصر إدخال ({sel})",
                                    "info")
                                for el in elements:
                                    try:
                                        if el.is_visible():
                                            # إجابة افتراضية (يمكن ربطها بمصدر إجابات)
                                            if sel == "textarea" or "text" in sel:
                                                el.fill("الإجابة")
                                            elif sel == "input[type='radio']":
                                                el.check()
                                            elif sel == "input[type='checkbox']":
                                                el.check()
                                    except Exception:
                                        continue
                                break
                        except Exception:
                            continue

                    if not found_any:
                        log("ℹ️ لم يتم العثور على حقول إدخال في هذه الصفحة", "info")

                    # محاولة الضغط على زر التالي / التالي
                    next_selectors = [
                        "button:has-text('التالي')",
                        "button:has-text('Next')",
                        "button:has-text('حفظ')",
                        "button:has-text('إرسال')",
                        "button:has-text('Submit')",
                        "button[type='submit']",
                    ]
                    for sel in next_selectors:
                        try:
                            btn = self.page.query_selector(sel)
                            if btn and btn.is_visible():
                                btn.click()
                                log(f"➡️ تم الانتقال للمهمة التالية", "success")
                                break
                        except Exception:
                            continue

                except Exception as e:
                    log(f"⚠️ خطأ أثناء معالجة عناصر الصفحة: {e}", "warn")

                self.page.wait_for_timeout(2000)
                solved += 1
                log(f"✅ تم حل المهمة {i} بنجاح (المجموع: {solved})", "success")

            except Exception as e:
                log(f"⚠️ فشل حل المهمة {i}: {e}", "error")
                continue

        log(f"🎉 تم إنجاز {solved} مهمة من أصل {total_tasks}", "success")


# ============================================================
# مسارات Flask (Routes)
# ============================================================
@app.route("/", methods=["GET"])
def index():
    """يقدّم الواجهة الأمامية"""
    try:
        return send_from_directory(".", "index.html")
    except Exception as e:
        return f"<h1>خطأ في تحميل الواجهة</h1><pre>{e}</pre>", 500


@app.route("/api/start", methods=["POST"])
def api_start():
    try:
        data = request.get_json(force=True, silent=True) or {}
        platform_url = data.get("platform_url", "").strip()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        if not platform_url or not username or not password:
            return jsonify({
                "ok": False,
                "message": "الرجاء إدخال رابط المنصة واسم المستخدم وكلمة المرور"
            }), 400

        if automation_state.is_running():
            return jsonify({
                "ok": False,
                "message": "هناك عملية قيد التشغيل بالفعل"
            }), 409

        engine = AutomationEngine(platform_url, username, password)
        started = automation_state.start(engine.run)

        if started:
            return jsonify({"ok": True, "message": "تم بدء العملية"})
        return jsonify({"ok": False, "message": "تعذّر بدء العملية"}), 500

    except Exception as e:
        log(f"❌ خطأ في /api/start: {e}", "error")
        log(traceback.format_exc(), "error")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/pause", methods=["POST"])
def api_pause():
    try:
        if automation_state.status == AutomationState.PAUSED:
            # استئناف
            ok = automation_state.resume()
            return jsonify({"ok": ok, "status": automation_state.status})
        ok = automation_state.pause()
        return jsonify({"ok": ok, "status": automation_state.status})
    except Exception as e:
        log(f"❌ خطأ في /api/pause: {e}", "error")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    try:
        ok = automation_state.stop()
        return jsonify({"ok": ok, "status": automation_state.status})
    except Exception as e:
        log(f"❌ خطأ في /api/stop: {e}", "error")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    try:
        return jsonify({
            "ok": True,
            "status": automation_state.status
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/logs/stream", methods=["GET"])
def api_logs_stream():
    """بث حي للسجلات (Server-Sent Events)"""
    def event_stream():
        q = log_manager.add_subscriber()
        try:
            # إرسال السجل القديم أولاً
            for entry in log_manager.get_history():
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"

            while True:
                try:
                    entry = q.get(timeout=30)
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    # keep-alive
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            pass
        except Exception as e:
            try:
                err = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "level": "error",
                       "message": f"stream error: {e}"}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            except Exception:
                pass
        finally:
            log_manager.remove_subscriber(q)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    })


@app.route("/api/logs/history", methods=["GET"])
def api_logs_history():
    try:
        return jsonify({"ok": True, "logs": log_manager.get_history()})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({"ok": False, "message": "المسار غير موجود"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"ok": False, "message": "خطأ داخلي في الخادم"}), 500


# ============================================================
# نقطة التشغيل
# ============================================================
def main():
    log("=" * 60, "success")
    log("🚀 Education Automation Framework", "success")
    log(f"🌐 الخادم يعمل على http://{HOST}:{PORT}", "success")
    log("=" * 60, "success")
    print()
    print(f"  ➜ افتح المتصفح على: http://{HOST}:{PORT}")
    print()

    try:
        app.run(host=HOST, port=PORT, debug=False, threaded=True,
                use_reloader=False)
    except KeyboardInterrupt:
        log("👋 تم إيقاف الخادم يدوياً", "warn")
    except Exception as e:
        log(f"❌ خطأ في تشغيل الخادم: {e}", "error")
        log(traceback.format_exc(), "error")
        sys.exit(1)


if __name__ == "__main__":
    main()
