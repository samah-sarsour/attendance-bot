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

load_dotenv()

BOT_TOKEN = os.getenv("ATTENDANCE_BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Attendance Bot").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
PORT = int(os.getenv("PORT", "10000"))

admin_raw = os.getenv("ADMIN_ID", "0").strip()
ADMIN_ID = int(admin_raw) if admin_raw.isdigit() else 0

SHEET_ATTENDANCE = "attendance"
SHEET_STUDENTS_NEW = "students new"
SHEET_STUDENTS_OLD = "students old"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COURSE_IMAGE = os.path.join(BASE_DIR, "course.png")

telegram_app: Optional[Application] = None


def now_dt() -> datetime:
    return datetime.now(ZoneInfo("Asia/Riyadh"))


def today_str() -> str:
    return now_dt().strftime("%Y-%m-%d")


def time_str() -> str:
    return now_dt().strftime("%I:%M %p")


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def normalize_name(name: str) -> str:
    return " ".join((name or "").strip().split()).casefold()


attendance = {
    "active": False,
    "records": [],
    "user_ids": set(),
    "started_at": None,
    "session_date": None,
    "message_chat_id": None,
    "message_id": None,
    "known_students": set(),
    "all_students": [],
    "new_students": [],
}


def get_client():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON غير موجود")

    creds = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(creds, scopes=scopes)
    return gspread.authorize(credentials)


def get_sheet():
    return get_client().open(GOOGLE_SHEET_NAME)


def ensure_sheet_headers():
    sh = get_sheet()

    needed = [
        (SHEET_ATTENDANCE, ["الاسم الثلاثي", "التاريخ", "وقت التسجيل", "الحالة"]),
        (SHEET_STUDENTS_NEW, ["الاسم الثلاثي"]),
        (SHEET_STUDENTS_OLD, ["الاسم الثلاثي"]),
    ]

    for sheet_name, headers in needed:
        try:
            ws = sh.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_name, rows=2000, cols=10)

        row1 = ws.row_values(1)
        if not row1:
            ws.append_row(headers)


def read_names(sheet_name: str):
    sh = get_sheet()
    ws = sh.worksheet(sheet_name)
    values = ws.col_values(1)[1:]

    names = []
    seen = set()

    for name in values:
        clean_name = " ".join(str(name).split()).strip()
        if not clean_name:
            continue

        key = normalize_name(clean_name)
        if key not in seen:
            seen.add(key)
            names.append(clean_name)

    return names


def load_students():
    ensure_sheet_headers()

    old_names = read_names(SHEET_STUDENTS_OLD)
    new_names = read_names(SHEET_STUDENTS_NEW)

    all_students = []
    known_students = set()

    for name in old_names + new_names:
        key = normalize_name(name)
        if key not in known_students:
            known_students.add(key)
            all_students.append(name)

    attendance["all_students"] = all_students
    attendance["known_students"] = known_students
    attendance["new_students"] = []


def save_all():
    ensure_sheet_headers()
    sh = get_sheet()

    # حفظ الطلاب الجدد
    if attendance["new_students"]:
        ws_new = sh.worksheet(SHEET_STUDENTS_NEW)
        rows_new = [[n] for n in attendance["new_students"]]
        ws_new.append_rows(rows_new, value_input_option="USER_ENTERED")

    # حفظ الحضور
    ws_att = sh.worksheet(SHEET_ATTENDANCE)

    present_map = {}
    for r in attendance["records"]:
        key = normalize_name(r["full_name"])
        if key not in present_map:
            present_map[key] = r

    rows = []

    for name in attendance["all_students"]:
        key = normalize_name(name)
        if key in present_map:
            r = present_map[key]
            rows.append([name, attendance["session_date"], r["time"], "حاضر"])
        else:
            rows.append([name, attendance["session_date"], "", "لم يحضر"])

    if rows:
        ws_att.append_rows(rows, value_input_option="USER_ENTERED")


def build_button(active=True):
    if active:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("تسجيل الحضور", callback_data="reg")]]
        )
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("تم إغلاق التسجيل", callback_data="closed")]]
    )


def build_text():
    if not attendance["records"]:
        names = "لا يوجد تسجيل"
    else:
        names = "\n".join(
            f"{i+1}) {r['full_name']} - {r['time']}"
            for i, r in enumerate(attendance["records"])
        )

    start_time = attendance["started_at"].strftime("%I:%M %p") if attendance["started_at"] else "-"

    return (
        f"📋 كشف الحضور\n\n"
        f"📅 التاريخ: {attendance['session_date']}\n"
        f"🕒 وقت البداية: {start_time}\n"
        f"👥 العدد: {len(attendance['records'])}\n\n"
        f"{names}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("البوت يعمل ✅")


async def start_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ليس لديك صلاحية.")
        return

    try:
        load_students()
    except Exception as e:
        await update.message.reply_text(f"تعذر الاتصال بـ Google Sheets:\n{e}")
        return

    attendance["active"] = True
    attendance["records"] = []
    attendance["user_ids"] = set()
    attendance["started_at"] = now_dt()
    attendance["session_date"] = today_str()
    attendance["new_students"] = []

    if os.path.exists(COURSE_IMAGE):
        with open(COURSE_IMAGE, "rb") as photo:
            sent = await update.message.reply_photo(
                photo=photo,
                caption=build_text(),
                reply_markup=build_button(True)
            )
    else:
        sent = await update.message.reply_text(
            build_text(),
            reply_markup=build_button(True)
        )

    attendance["message_chat_id"] = sent.chat_id
    attendance["message_id"] = sent.message_id


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None:
        return

    if q.data == "closed":
        await q.answer("تم إغلاق التسجيل", show_alert=True)
        return

    if not attendance["active"]:
        await q.answer("التسجيل مغلق", show_alert=True)
        return

    if q.from_user.id in attendance["user_ids"]:
        await q.answer("تم تسجيلك مسبقًا", show_alert=True)
        return

    name = " ".join((q.from_user.full_name or "").split()).strip()
    if not name:
        name = "بدون اسم"

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

    await q.answer("تم تسجيل حضورك ✅", show_alert=True)

    try:
        if q.message and q.message.photo:
            await q.message.edit_caption(
                caption=build_text(),
                reply_markup=build_button(True)
            )
        elif q.message:
            await q.message.edit_text(
                text=build_text(),
                reply_markup=build_button(True)
            )
    except Exception:
        pass


async def end_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ليس لديك صلاحية.")
        return

    if not attendance["active"]:
        await update.message.reply_text("لا توجد جلسة حضور مفتوحة الآن.")
        return

    try:
        save_all()
    except Exception as e:
        await update.message.reply_text(f"حدث خطأ أثناء الحفظ:\n{e}")
        return

    attendance["active"] = False

    try:
        if attendance["message_chat_id"] and attendance["message_id"]:
            await context.bot.edit_message_reply_markup(
                chat_id=attendance["message_chat_id"],
                message_id=attendance["message_id"],
                reply_markup=build_button(False)
            )
    except Exception:
        pass

    await update.message.reply_text("تم الحفظ ✅")


async def webhook(request: Request):
    global telegram_app

    if telegram_app is None:
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=500)

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})


async def home(request: Request):
    return PlainTextResponse("Bot is running")


async def health(request: Request):
    return PlainTextResponse("ok")


async def startup():
    global telegram_app

    if not BOT_TOKEN:
        raise ValueError("ATTENDANCE_BOT_TOKEN غير موجود")
    if not WEBHOOK_URL:
        raise ValueError("WEBHOOK_URL غير موجود")

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("start_attendance", start_attendance))
    telegram_app.add_handler(CommandHandler("end_attendance", end_attendance))
    telegram_app.add_handler(CallbackQueryHandler(register, pattern="^(reg|closed)$"))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(WEBHOOK_URL)


starlette_app = Starlette(
    routes=[
        Route("/", home, methods=["GET"]),
        Route("/healthz", health, methods=["GET"]),
        Route("/webhook", webhook, methods=["POST"]),
    ],
    on_startup=[startup],
)

if __name__ == "__main__":
    uvicorn.run(starlette_app, host="0.0.0.0", port=PORT)
