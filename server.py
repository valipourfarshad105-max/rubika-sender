# -*- coding: utf-8 -*-
"""
server.py
----------
پنل ارسال پیام از اکانت شخصی روبیکا (Flask + rubpy).
معادل server.js در پروژهٔ تلگرام؛ همان معماری و همان API را دارد تا
صفحهٔ HTML موجود (public/index.html) تقریباً بدون تغییر کار کند.
"""

import os
import io
import json
import time
import threading
import traceback
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, send_file, Response
from werkzeug.utils import secure_filename
import openpyxl

from rubika_service import RubikaService, RubikaServiceError, NotOnRubika
from rate_limiter import RateLimiter

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = BASE_DIR / "uploads"
PERSISTENT_PHOTO_DIR = UPLOADS_DIR / "persistent"
STATE_FILE = DATA_DIR / "state.json"
RATE_LIMIT_FILE = DATA_DIR / "rate_limit_state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PERSISTENT_PHOTO_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", 3000))
RUBIKA_SESSION = os.environ.get("RUBIKA_SESSION")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")

if not RUBIKA_SESSION or not ADMIN_TOKEN:
    raise SystemExit(
        "مقادیر RUBIKA_SESSION / ADMIN_TOKEN را در فایل .env تنظیم کن.\n"
        "برای گرفتن RUBIKA_SESSION ابتدا دستور «python login.py» را اجرا کن."
    )

# حداقل تأخیر مجاز بین ارسال‌ها (میلی‌ثانیه). طبق درخواست شما، محتاطانه‌تر از پنل تلگرام:
# در تلگرام حداقل ۳ ثانیه بود؛ اینجا حداقل ۶ ثانیه.
MIN_DELAY_MS = int(os.environ.get("MIN_DELAY_MS", 6000))

MAX_MESSAGES_PER_MINUTE = int(os.environ.get("MAX_MESSAGES_PER_MINUTE", 8))
MAX_MESSAGES_PER_HOUR = int(os.environ.get("MAX_MESSAGES_PER_HOUR", 30))
MAX_NEW_CONTACTS_PER_DAY = int(os.environ.get("MAX_NEW_CONTACTS_PER_DAY", 200))

app = Flask(__name__, static_folder=None)

rate_limiter = RateLimiter(
    str(RATE_LIMIT_FILE),
    max_messages_per_minute=MAX_MESSAGES_PER_MINUTE,
    max_messages_per_hour=MAX_MESSAGES_PER_HOUR,
    max_new_contacts_per_day=MAX_NEW_CONTACTS_PER_DAY,
)

# ---------------------------------------------------------------------------
# Persistent state (روی دیسک سرور، نه روی مرورگر)
# ---------------------------------------------------------------------------


def default_state():
    return {
        "listId": None,
        "numbers": [],  # [{ phone, status }]
        "text": "",
        "photo": None,  # { filename, originalName, mimeType, size }
        "cursor": 0,
        "results": [],  # [{ phone, status, error, time }]
        "batch": {
            "status": "idle",  # idle | running | stopped | done
            "total": 0,
            "startOffset": 0,
            "processed": 0,
            "delayMs": MIN_DELAY_MS,
            "startedAt": None,
        },
    }


state_lock = threading.Lock()


def load_state():
    if not STATE_FILE.exists():
        return default_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            parsed = json.load(f)
        merged = default_state()
        merged.update(parsed)
        merged["batch"] = {**default_state()["batch"], **(parsed.get("batch") or {})}
        return merged
    except Exception as e:
        print("خطا در خواندن state.json:", e)
        return default_state()


def save_state():
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


state = load_state()

if state["batch"]["status"] == "running":
    state["batch"]["status"] = "stopped"
    save_state()

active_batch = {"running": False, "stop_requested": False}
active_batch_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Rubika client
# ---------------------------------------------------------------------------

print("در حال اتصال به روبیکا...")
rubika = RubikaService(RUBIKA_SESSION)
rubika.start()
print("متصل شد. حساب:", rubika.account_info())

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"success": False, "error": "دسترسی غیرمجاز."}), 401
        token = auth[7:]
        if token != ADMIN_TOKEN:
            return jsonify({"success": False, "error": "رمز پنل اشتباه است."}), 403
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Phone normalization (مطابق فرمتی که خودِ rubpy برای شمارهٔ ایرانی انتظار دارد)
# ---------------------------------------------------------------------------

FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
AR_DIGITS = "٠١٢٣٤٥٦٧٨٩"


def normalize_phone(value) -> str:
    if value is None:
        return ""
    phone = str(value).strip()
    phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    phone = "".join(
        str(FA_DIGITS.index(ch)) if ch in FA_DIGITS else
        str(AR_DIGITS.index(ch)) if ch in AR_DIGITS else ch
        for ch in phone
    )

    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("00"):
        phone = phone[2:]
    if phone.startswith("0"):
        phone = "98" + phone[1:]

    if not phone.isdigit():
        return ""
    if not (10 <= len(phone) <= 15):
        return ""
    if not phone.startswith("98"):
        # اگر با کد کشور دیگری وارد شده، همان را نگه می‌داریم (بین‌المللی)
        pass

    return phone


def compute_list_id(phone_list):
    joined = ",".join(phone_list)
    h = 0
    for ch in joined:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return f"{len(phone_list)}:{h}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clear_persistent_photo_files():
    for f in PERSISTENT_PHOTO_DIR.iterdir():
        try:
            f.unlink()
        except Exception:
            pass


def public_state():
    remaining = max(0, len(state["numbers"]) - state["cursor"])
    rl_status = rate_limiter.status()
    return {
        "listId": state["listId"],
        "numbers": state["numbers"],
        "text": state["text"],
        "photo": state["photo"],
        "cursor": state["cursor"],
        "remaining": remaining,
        "totalNumbers": len(state["numbers"]),
        "results": state["results"],
        "batch": state["batch"],
        "rateLimit": rl_status,
        "minDelayMs": MIN_DELAY_MS,
        "account": rubika.account_info(),
    }


def describe_exception(e: Exception) -> str:
    """
    برخی خطاهای داخلی خودِ کتابخانهٔ rubpy وقتی با str() چاپ شوند، پیام‌شان
    خالی یا نامفهوم است (مثلاً فقط یک دیکشنری وضعیت را برمی‌گردانند).
    این تابع تلاش می‌کند همیشه یک متن قابل‌فهم برای کاربر و برای لاگ سرور بسازد.
    """
    text = str(e).strip()
    if not text or text in ("None", "{}"):
        text = repr(e)
    class_name = type(e).__name__
    return f"{class_name}: {text}"


def log_exception(context: str, e: Exception):
    print(f"=== خطا در {context} ===")
    print(describe_exception(e))
    traceback.print_exc()
    print("=" * 40)


def record_result(result):
    state["results"].append(result)
    for n in state["numbers"]:
        if n["phone"] == result["phone"]:
            n["status"] = result["status"]
            break
    save_state()


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "public", "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    full = BASE_DIR / "public" / filename
    if full.exists():
        return send_from_directory(BASE_DIR / "public", filename)
    return jsonify({"success": False, "error": "not found"}), 404


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@app.route("/api/state", methods=["GET"])
@auth_required
def get_state():
    return jsonify({"success": True, "state": public_state()})


# ---------------------------------------------------------------------------
# Excel import
# ---------------------------------------------------------------------------

ALLOWED_EXCEL_EXT = {".xlsx", ".xls", ".csv"}


@app.route("/api/state/excel", methods=["POST"])
@auth_required
def import_excel():
    try:
        file = request.files.get("excel")
        if not file:
            return jsonify({"success": False, "error": "فایل Excel انتخاب نشده است."}), 400

        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXCEL_EXT:
            return jsonify({"success": False, "error": "فقط فایل Excel با فرمت XLSX، XLS یا CSV مجاز است."}), 400

        numbers = []
        seen = set()

        if ext == ".csv":
            import csv
            content = file.stream.read().decode("utf-8-sig", errors="ignore")
            reader = csv.reader(io.StringIO(content))
            rows = list(reader)
        else:
            wb = openpyxl.load_workbook(file, data_only=True)
            ws = wb[wb.sheetnames[0]]
            rows = [[cell.value for cell in row] for row in ws.iter_rows()]

        for row in rows:
            if not row:
                continue
            raw = row[0]
            phone = normalize_phone(raw)
            if not phone or phone in seen:
                continue
            seen.add(phone)
            numbers.append(phone)

        new_list_id = compute_list_id(numbers)

        if new_list_id != state["listId"]:
            state["listId"] = new_list_id
            state["numbers"] = [{"phone": p, "status": ""} for p in numbers]
            state["cursor"] = 0
            state["results"] = []
            state["batch"] = default_state()["batch"]

        save_state()
        return jsonify({"success": True, "count": len(numbers), "state": public_state()})
    except Exception as e:
        print("EXCEL IMPORT ERROR:", e)
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


@app.route("/api/state/text", methods=["POST"])
@auth_required
def save_text():
    data = request.get_json(force=True, silent=True) or {}
    state["text"] = str(data.get("text", ""))
    save_state()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Photo
# ---------------------------------------------------------------------------

ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@app.route("/api/state/photo", methods=["POST"])
@auth_required
def upload_photo():
    try:
        file = request.files.get("photo")
        if not file:
            return jsonify({"success": False, "error": "عکسی انتخاب نشده است."}), 400

        if file.mimetype not in ALLOWED_IMAGE_MIMES:
            return jsonify({"success": False, "error": "فقط فایل‌های تصویری (jpg, png, webp, gif) مجاز هستند."}), 400

        clear_persistent_photo_files()

        ext = Path(file.filename).suffix or ""
        filename = f"photo-{int(time.time() * 1000)}{ext}"
        file.save(PERSISTENT_PHOTO_DIR / filename)

        size = (PERSISTENT_PHOTO_DIR / filename).stat().st_size

        state["photo"] = {
            "filename": filename,
            "originalName": file.filename,
            "mimeType": file.mimetype,
            "size": size,
        }
        save_state()
        return jsonify({"success": True, "photo": state["photo"]})
    except Exception as e:
        print("PHOTO UPLOAD ERROR:", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/state/photo", methods=["DELETE"])
@auth_required
def delete_photo():
    clear_persistent_photo_files()
    state["photo"] = None
    save_state()
    return jsonify({"success": True})


@app.route("/media/photo", methods=["GET"])
@auth_required
def media_photo():
    if not state["photo"]:
        return "عکسی ذخیره نشده است.", 404
    file_path = PERSISTENT_PHOTO_DIR / state["photo"]["filename"]
    if not file_path.exists():
        return "فایل عکس یافت نشد.", 404
    return send_file(file_path, mimetype=state["photo"].get("mimeType") or "image/jpeg")


# ---------------------------------------------------------------------------
# Cursor / report reset
# ---------------------------------------------------------------------------


@app.route("/api/state/reset-cursor", methods=["POST"])
@auth_required
def reset_cursor():
    state["cursor"] = 0
    save_state()
    return jsonify({"success": True})


@app.route("/api/state/clear-report", methods=["POST"])
@auth_required
def clear_report():
    state["results"] = []
    for n in state["numbers"]:
        n["status"] = ""
    save_state()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Single manual send
# ---------------------------------------------------------------------------


@app.route("/api/send", methods=["POST"])
@auth_required
def send_single():
    data = request.get_json(force=True, silent=True) or {}
    phone = normalize_phone(data.get("phone"))

    if not phone:
        return jsonify({"success": False, "status": "failed", "error": "شماره تلفن معتبر نیست."}), 400

    photo_path = None
    if state["photo"]:
        photo_path = str(PERSISTENT_PHOTO_DIR / state["photo"]["filename"])

    if not state["text"] and not photo_path:
        return jsonify({"success": False, "status": "failed", "phone": phone, "error": "متن یا عکس ذخیره‌شده‌ای وجود ندارد."}), 400

    try:
        with active_batch_lock:
            pass  # ارسال دستی صرفاً برای اطمینان از عدم برخورد با batch از لاک استفاده می‌کند (فقط خواندن)

        if not rate_limiter.can_add_new_contact(phone):
            result = {
                "phone": phone,
                "status": "failed",
                "error": "سقف روزانهٔ افزودن مخاطب جدید پر شده؛ فردا دوباره تلاش کن.",
                "time": datetime.now().isoformat(),
            }
            record_result(result)
            return jsonify({"success": False, **result}), 429

        try:
            guid = rubika.resolve_guid(phone)
        except NotOnRubika as e:
            result = {
                "phone": phone,
                "status": "not_found",
                "error": str(e),
                "time": datetime.now().isoformat(),
            }
            record_result(result)
            return jsonify({"success": False, **result}), 404

        rate_limiter.record_new_contact(phone)

        wait_s = rate_limiter.wait_seconds_for_message_slot()
        if wait_s > 0:
            time.sleep(wait_s)

        rubika.send_smart(guid, state["text"], photo_path)
        rate_limiter.record_message_sent()

        result = {"phone": phone, "status": "sent", "error": "", "time": datetime.now().isoformat()}
        record_result(result)
        return jsonify({"success": True, "status": "sent", "phone": phone, "message": "ارسال با موفقیت انجام شد."})
    except Exception as e:
        log_exception(f"ارسال دستی به {phone}", e)
        result = {"phone": phone, "status": "failed", "error": describe_exception(e), "time": datetime.now().isoformat()}
        record_result(result)
        return jsonify({"success": False, **result}), 500


# ---------------------------------------------------------------------------
# Batch sending job
# ---------------------------------------------------------------------------


def run_batch_job(numbers, text, photo_path, delay_ms):
    for phone in numbers:
        with active_batch_lock:
            if not active_batch["running"] or active_batch["stop_requested"]:
                state["batch"]["status"] = "stopped"
                save_state()
                active_batch["running"] = False
                return

        result = None
        try:
            if not rate_limiter.can_add_new_contact(phone):
                result = {
                    "phone": phone,
                    "status": "failed",
                    "error": "سقف روزانهٔ افزودن مخاطب جدید پر شده.",
                    "time": datetime.now().isoformat(),
                }
                apply_batch_result(result)
                time.sleep(delay_ms / 1000)
                continue

            try:
                guid = rubika.resolve_guid(phone)
            except NotOnRubika as e:
                result = {
                    "phone": phone,
                    "status": "not_found",
                    "error": str(e),
                    "time": datetime.now().isoformat(),
                }
                apply_batch_result(result)
                time.sleep(delay_ms / 1000)
                continue

            rate_limiter.record_new_contact(phone)

            wait_s = rate_limiter.wait_seconds_for_message_slot()
            if wait_s > 0:
                time.sleep(wait_s)

            rubika.send_smart(guid, text, photo_path)
            rate_limiter.record_message_sent()
            result = {"phone": phone, "status": "sent", "error": "", "time": datetime.now().isoformat()}
        except Exception as e:
            log_exception(f"ارسال گروهی به {phone}", e)
            result = {"phone": phone, "status": "failed", "error": describe_exception(e), "time": datetime.now().isoformat()}

        apply_batch_result(result)
        time.sleep(delay_ms / 1000)

    state["batch"]["status"] = "done"
    save_state()
    active_batch["running"] = False


def apply_batch_result(result):
    state["results"].append(result)
    for n in state["numbers"]:
        if n["phone"] == result["phone"]:
            n["status"] = result["status"]
            break
    state["cursor"] += 1
    state["batch"]["processed"] += 1
    save_state()


@app.route("/api/batch/start", methods=["POST"])
@auth_required
def batch_start():
    try:
        with active_batch_lock:
            if active_batch["running"]:
                return jsonify({"success": False, "error": "یک عملیات ارسال گروهی دیگر در حال اجراست."}), 409

            remaining = len(state["numbers"]) - state["cursor"]
            if remaining <= 0:
                return jsonify({"success": False, "error": "تمام شماره‌های این لیست قبلاً پردازش شده‌اند."}), 400

            photo_path = None
            if state["photo"]:
                photo_path = str(PERSISTENT_PHOTO_DIR / state["photo"]["filename"])

            if not state["text"] and not photo_path:
                return jsonify({"success": False, "error": "متن یا عکس ذخیره‌شده‌ای وجود ندارد."}), 400

            data = request.get_json(force=True, silent=True) or {}
            count = data.get("count")
            count = max(1, min(int(count) if count else remaining, remaining))
            delay_ms = max(MIN_DELAY_MS, int(data.get("delayMs") or MIN_DELAY_MS))

            start_offset = state["cursor"]
            target_numbers = [n["phone"] for n in state["numbers"][start_offset:start_offset + count]]

            state["batch"] = {
                "status": "running",
                "total": len(target_numbers),
                "startOffset": start_offset,
                "processed": 0,
                "delayMs": delay_ms,
                "startedAt": datetime.now().isoformat(),
            }
            save_state()

            active_batch["running"] = True
            active_batch["stop_requested"] = False

            text = state["text"]

        thread = threading.Thread(
            target=run_batch_job,
            args=(target_numbers, text, photo_path, delay_ms),
            daemon=True,
        )
        thread.start()

        return jsonify({"success": True, "total": len(target_numbers)})
    except Exception as e:
        print("BATCH START ERROR:", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/batch/stop", methods=["POST"])
@auth_required
def batch_stop():
    with active_batch_lock:
        if not active_batch["running"]:
            return jsonify({"success": False, "error": "عملیات فعالی وجود ندارد."}), 404
        active_batch["stop_requested"] = True
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Excel report
# ---------------------------------------------------------------------------


@app.route("/api/report", methods=["GET"])
@auth_required
def get_report():
    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "گزارش ارسال"
        ws.append(["ردیف", "شماره", "وضعیت", "توضیحات", "زمان"])

        status_map = {
            "sent": "ارسال شد",
            "not_found": "شماره روبیکا ندارد / قابل شناسایی نیست",
            "failed": "ارسال ناموفق",
        }

        for idx, item in enumerate(state["results"], start=1):
            status_text = status_map.get(item.get("status"), "نامشخص")
            time_str = ""
            if item.get("time"):
                try:
                    time_str = datetime.fromisoformat(item["time"]).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    time_str = item["time"]
            ws.append([idx, item.get("phone", ""), status_text, item.get("error", ""), time_str])

        widths = [8, 22, 34, 50, 22]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[chr(64 + i)].width = w

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="rubika-report.xlsx",
        )
    except Exception as e:
        print("REPORT ERROR:", e)
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


@app.errorhandler(Exception)
def handle_error(err):
    print("UNHANDLED ERROR:", err)
    return jsonify({"success": False, "error": str(err) or "خطای درخواست"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
