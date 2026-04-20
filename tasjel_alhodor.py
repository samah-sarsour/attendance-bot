import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
import uvicorn

# =========================================
# إعدادات
# =========================================
load_dotenv()

BOT_TOKEN = os.getenv("ATTENDANCE_BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Attendance Bot").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
PORT = int(os.getenv("PORT", "10000"))

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

SHEET_ATTENDANCE = "attendance"
SHEET_STUDENTS_NEW = "students new"
SHEET_STUDENTS_OLD = "students old"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COURSE_IMAGE = os.path.join(BASE_DIR, "course.png")

telegram_app: Optional[Application] = None

# =========================================
# الوقت (السعودية)
# =========================================
def now_dt():
    return datetime.now(ZoneInfo("Asia/Riyadh"))

def today_str():
    return now_dt().strftime("%Y-%m-%d")

def time_str():
    return now_dt().strftime("%I:%M %p")

# =========================================
# الحالة
# =========================================
attendance = {
    "active": False,
    "records": [],
    "user_ids": set(),
    "started_at": None,
    "session_date": None,

    "status_chat_id": None,
    "status_message_id": None,

    "button_chat_id": None,
    "button_message_id": None,

    "known_students": set(),
    "all_students": [],
    "new_students": [],
}

# =========================================
# مساعدات
# =========================================
def is_admin(user_id):
    return user_id == ADMIN_ID

def normalize_name(name):
    return " ".join((name or "").strip().split()).lower()

# =========================================
# Google Sheets
# =========================================
def get_client():
    creds = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(creds, scopes=scopes)
    return gspread.authorize(credentials)

def get_sheet():
    return get_client().open(GOOGLE_SHEET_NAME)

def read_names(sheet):
    ws = get_sheet().worksheet(sheet)
    return [n.strip() for n in ws.col_values(1)[1:] if n]

def load_students():
    old = read_names(SHEET_STUDENTS_OLD)
    new = read_names(SHEET_STUDENTS_NEW)

    attendance["all_students"] = old + new
    attendance["known_students"] = {normalize_name(n) for n in old + new}
    attendance["new_students"] = []

def save_all():
    sh = get_sheet()

    # حفظ الجدد
    if attendance["new_students"]:
        ws_new = sh.worksheet(SHEET_STUDENTS_NEW)
        ws_new.append_rows([[n] for n in attendance["new_students"]])

    # الحضور
    ws = sh.worksheet(SHEET_ATTENDANCE)

    present = {normalize_name(r["full_name"]): r for r in attendance["records"]}

    rows = []

    for name in attendance["all_students"]:
        key = normalize_name(name)
        if key in present:
            rows.append([name, attendance["session_date"], present[key]["time"], "حاضر"])
        else:
            rows.append([name, attendance["session_date"], "", "لم يحضر"])

    ws.append_rows(rows)

# =========================================
# واجهة
# =========================================
def build_button(active=True):
    if active:
        return InlineKeyboardMarkup([[InlineKeyboardButton("تسجيل الحضور", callback_data="reg")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("تم الإغلاق", callback_data="closed")]])

def build_text():
    if not attendance["records"]:
        names = "لا يوجد تسجيل"
    else:
        names = "\n".join(
            f"{i+1}) {r['full_name']} - {r['time']}"
            for i, r in enumerate(attendance["records"])
        )

    return (
        f"📋 كشف الحضور\n\n"
        f"📅 {attendance['session_date']}\n"
        f"🕒 البداية: {attendance['started_at'].strftime('%I:%M %p')}\n"
        f"👥 العدد: {len(attendance['records'])}\n\n"
        f"{names}"
    )

# =========================================
# أوامر
# =========================================
async def start(update, context):
    if update.message:
        await update.message.reply_text("البوت يعمل ✅")

async def myid(update, context):
    if update.message:
        await update.message.reply_text(str(update.effective_user.id))

async def start_attendance(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ليس لديك صلاحية.")
        return

    load_students()

    attendance["active"] = True
    attendance["records"] = []
    attendance["user_ids"] = set()
    attendance["started_at"] = now_dt()
    attendance["session_date"] = today_str()

    # رسالة القائمة
    status = await update.message.reply_text(build_text())
    attendance["status_chat_id"] = status.chat_id
    attendance["status_message_id"] = status.message_id

    # صورة + زر
    if os.path.exists(COURSE_IMAGE):
        with open(COURSE_IMAGE, "rb") as photo:
            btn = await update.message.reply_photo(
                photo=photo,
                caption="اضغط لتسجيل الحضور",
                reply_markup=build_button()
            )
    else:
        btn = await update.message.reply_text(
            "اضغط لتسجيل الحضور",
            reply_markup=build_button()
        )

    attendance["button_chat_id"] = btn.chat_id
    attendance["button_message_id"] = btn.message_id

async def register(update, context):
    q = update.callback_query
    await q.answer()

    if not attendance["active"]:
        return

    if q.from_user.id in attendance["user_ids"]:
        await q.answer("تم تسجيلك مسبقًا", show_alert=True)
        return

    name = q.from_user.full_name.strip()
    key = normalize_name(name)

    if key not in attendance["known_students"]:
        attendance["known_students"].add(key)
        attendance["all_students"].append(name)
        attendance["new_students"].append(name)

    attendance["user_ids"].add(q.from_user.id)
    attendance["records"].append({
        "full_name": name,
        "time": time_str()
    })

    # تحديث القائمة فقط
    await context.bot.edit_message_text(
        chat_id=attendance["status_chat_id"],
        message_id=attendance["status_message_id"],
        text=build_text()
    )

async def end_attendance(update, context):
    if not is_admin(update.effective_user.id):
        return

    save_all()
    attendance["active"] = False

    # إغلاق الزر
    await context.bot.edit_message_reply_markup(
        chat_id=attendance["button_chat_id"],
        message_id=attendance["button_message_id"],
        reply_markup=build_button(False)
    )

    await update.message.reply_text("تم الحفظ ✅")

# =========================================
# Webhook
# =========================================
async def webhook(request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})

async def home(request):
    return PlainTextResponse("Bot is running")

async def health(request):
    return PlainTextResponse("ok")

async def startup():
    global telegram_app

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("myid", myid))
    telegram_app.add_handler(CommandHandler("start_attendance", start_attendance))
    telegram_app.add_handler(CommandHandler("end_attendance", end_attendance))
    telegram_app.add_handler(CallbackQueryHandler(register))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(WEBHOOK_URL)

app = Starlette(
    routes=[
        Route("/", home),
        Route("/healthz", health),
        Route("/webhook", webhook, methods=["POST"]),
    ],
    on_startup=[startup],
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
