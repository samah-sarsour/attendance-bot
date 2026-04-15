import os
import json
from datetime import datetime
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
    MessageHandler,
    ContextTypes,
    filters,
)

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
import uvicorn

# =========================================
# تحميل .env محليًا إذا وجد
# =========================================
load_dotenv()

BOT_TOKEN = os.getenv("ATTENDANCE_BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Attendance Bot").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
PORT = int(os.getenv("PORT", "10000"))

admin_raw = os.getenv("ADMIN_ID", "0").strip()
ADMIN_ID = int(admin_raw) if admin_raw.isdigit() else 0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COURSE_IMAGE = os.path.join(BASE_DIR, "course.png")

SHEET_ATTENDANCE = "attendance"
SHEET_STUDENTS_NEW = "students new"
SHEET_STUDENTS_OLD = "students old"

telegram_app: Optional[Application] = None

# =========================================
# حالة الجلسة الحالية
# =========================================
attendance = {
    "active": False,
    "records": [],
    "user_ids": set(),
    "started_at": None,
    "session_date": None,
    "message_chat_id": None,
    "message_id": None,

    # للأداء السريع
    "known_students": set(),      # أسماء الطلاب المعروفين normalized
    "all_students": [],           # كل الطلاب الحاليين بالأسماء الأصلية
    "new_students_to_add": [],    # أسماء جديدة ستُحفظ عند النهاية فقط
}

# =========================================
# دوال الوقت والمساعدة
# =========================================
def now_dt() -> datetime:
    return datetime.now()


def today_str() -> str:
    return now_dt().strftime("%d-%m-%Y")


def time_str() -> str:
    return now_dt().strftime("%I:%M %p")


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def normalize_name(name: str) -> str:
    return " ".join((name or "").strip().split()).casefold()


# =========================================
# Google Sheets
# =========================================
def get_gspread_client():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON غير موجود")

    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise ValueError(f"فشل قراءة GOOGLE_SERVICE_ACCOUNT_JSON: {e}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(credentials)


def get_spreadsheet():
    gc = get_gspread_client()
    return gc.open(GOOGLE_SHEET_NAME)


def ensure_sheet_headers():
    sh = get_spreadsheet()

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


def read_names_from_sheet(sheet_name: str) -> list[str]:
    sh = get_spreadsheet()
    ws = sh.worksheet(sheet_name)

    values = ws.col_values(1)[1:]
    names = []
    seen = set()

    for full_name in values:
        if not full_name:
            continue

        full_name = " ".join(str(full_name).split()).strip()
        key = normalize_name(full_name)

        if key not in seen:
            names.append(full_name)
            seen.add(key)

    return names


def load_students_into_memory():
    ensure_sheet_headers()

    old_names = read_names_from_sheet(SHEET_STUDENTS_OLD)
    new_names = read_names_from_sheet(SHEET_STUDENTS_NEW)

    all_names = []
    known = set()

    for name in old_names + new_names:
        clean_name = " ".join(name.split()).strip()
        key = normalize_name(clean_name)

        if key not in known:
            all_names.append(clean_name)
            known.add(key)

    attendance["known_students"] = known
    attendance["all_students"] = all_names
    attendance["new_students_to_add"] = []


def save_new_students_to_sheet():
    if not attendance["new_students_to_add"]:
        return

    ensure_sheet_headers()
    sh = get_spreadsheet()
    ws = sh.worksheet(SHEET_STUDENTS_NEW)

    rows = [[name] for name in attendance["new_students_to_add"]]
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def save_session_to_sheet():
    ensure_sheet_headers()

    session_date = attendance["session_date"]
    present_records = attendance["records"]
    all_students = attendance["all_students"]

    present_map = {}
    for record in present_records:
        key = normalize_name(record["full_name"])
        if key not in present_map:
            present_map[key] = record

    sh = get_spreadsheet()
    ws = sh.worksheet(SHEET_ATTENDANCE)

    rows_to_add = []

    # الحاضرون
    for record in present_map.values():
        rows_to_add.append([
            record["full_name"],
            session_date,
            record["registered_time"],
            "حاضر",
        ])

    # غير الحاضرين
    for student_name in all_students:
        key = normalize_name(student_name)
        if key not in present_map:
            rows_to_add.append([
                student_name,
                session_date,
                "",
                "لم يحضر",
            ])

    if rows_to_add:
        ws.append_rows(rows_to_add, value_input_option="USER_ENTERED")


# =========================================
# واجهة الرسائل
# =========================================
def build_button(active: bool = True):
    if active:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("اضغط هنا للتسجيل", callback_data="register")]
        ])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تم إغلاق التسجيل", callback_data="closed")]
    ])


def build_text() -> str:
    date_text = attendance["session_date"] or today_str()
    start_time = attendance["started_at"].strftime("%I:%M %p") if attendance["started_at"] else "-"

    if not attendance["records"]:
        names_block = "لا يوجد تسجيل حتى الآن"
    else:
        lines = []
        for i, record in enumerate(attendance["records"], start=1):
            lines.append(f"{i}) {record['full_name']} | {record['registered_time']}")
        names_block = "\n".join(lines)

    return (
        "📋 كشف الحضور\n"
        "━━━━━━━━━━━━\n\n"
        f"📅 التاريخ: {date_text}\n"
        f"🕒 وقت البداية: {start_time}\n"
        f"👥 العدد: {len(attendance['records'])}\n\n"
        "━━━━━━━━━━━━\n"
        f"{names_block}"
    )


# =========================================
# أوامر البوت
# =========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "أهلاً بك 👋\n\n"
            "الأوامر المتاحة:\n"
            "/start_attendance - بدء تسجيل الحضور\n"
            "/show_attendance - عرض كشف الحضور الحالي\n"
            "/end_attendance - إنهاء التسجيل\n"
            "/myid - معرفة رقمك"
        )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.effective_user:
        await update.message.reply_text(f"🆔 رقمك هو:\n{update.effective_user.id}")


async def echo_private_for_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        update.message
        and update.effective_chat
        and update.effective_user
        and update.effective_chat.type == "private"
        and is_admin(update.effective_user.id)
    ):
        text = update.message.text.strip()
        await update.message.reply_text(f"وصلتني رسالتك ✅\n\n{text}")


async def start_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ليس لديك صلاحية.")
        return

    try:
        load_students_into_memory()
    except Exception as e:
        await update.message.reply_text(f"تعذر الاتصال بـ Google Sheets:\n{e}")
        return

    attendance["active"] = True
    attendance["records"] = []
    attendance["user_ids"] = set()
    attendance["started_at"] = now_dt()
    attendance["session_date"] = today_str()
    attendance["new_students_to_add"] = []

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
    query = update.callback_query
    if query is None:
        return

    user = query.from_user

    if query.data == "closed":
        await query.answer("تم إغلاق التسجيل.", show_alert=True)
        return

    if not attendance["active"]:
        await query.answer("التسجيل مغلق", show_alert=True)
        return

    if user.id in attendance["user_ids"]:
        await query.answer("تم تسجيلك مسبقًا", show_alert=True)
        return

    full_name = user.full_name or user.first_name or "بدون اسم"
    full_name = " ".join(full_name.split()).strip()
    normalized = normalize_name(full_name)

    # إذا الاسم جديد، نحفظه في الذاكرة فقط
    if normalized not in attendance["known_students"]:
        attendance["known_students"].add(normalized)
        attendance["all_students"].append(full_name)
        attendance["new_students_to_add"].append(full_name)

    attendance["user_ids"].add(user.id)
    attendance["records"].append({
        "full_name": full_name,
        "registered_time": time_str(),
    })

    try:
        await query.answer("تم تسجيل حضورك ✅", show_alert=True)
    except Exception:
        pass

    try:
        if query.message and query.message.photo:
            await query.message.edit_caption(
                caption=build_text(),
                reply_markup=build_button(True)
            )
        elif query.message:
            await query.message.edit_text(
                text=build_text(),
                reply_markup=build_button(True)
            )
    except Exception:
        pass


async def show_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(build_text())


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
        save_new_students_to_sheet()
        save_session_to_sheet()
    except Exception as e:
        await update.message.reply_text(f"تعذر حفظ البيانات في Google Sheets:\n{e}")
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

    await update.message.reply_text("تم إغلاق التسجيل ✅")

    if attendance["records"]:
        summary = "\n".join(
            f"{i + 1}. {record['full_name']} - {record['registered_time']}"
            for i, record in enumerate(attendance["records"])
        )
    else:
        summary = "لا يوجد حضور مسجل."

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "📋 الملخص النهائي\n\n"
                f"📅 التاريخ: {attendance['session_date']}\n"
                f"👥 العدد: {len(attendance['records'])}\n\n"
                f"{summary}"
            )
        )
    except Exception:
        pass

    # تصفير الجلسة
    attendance["records"] = []
    attendance["user_ids"] = set()
    attendance["started_at"] = None
    attendance["session_date"] = None
    attendance["message_chat_id"] = None
    attendance["message_id"] = None
    attendance["known_students"] = set()
    attendance["all_students"] = []
    attendance["new_students_to_add"] = []


# =========================================
# Telegram application
# =========================================
def build_telegram_app() -> Application:
    if not BOT_TOKEN:
        raise ValueError("ATTENDANCE_BOT_TOKEN غير موجود")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("start_attendance", start_attendance))
    app.add_handler(CommandHandler("show_attendance", show_attendance))
    app.add_handler(CommandHandler("end_attendance", end_attendance))
    app.add_handler(CallbackQueryHandler(register, pattern="^(register|closed)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_private_for_admin))

    return app


# =========================================
# Webhook server
# =========================================
async def home(request: Request):
    return PlainTextResponse("Bot is running")


async def healthcheck(request: Request):
    return PlainTextResponse("ok")


async def telegram_webhook(request: Request):
    global telegram_app

    if telegram_app is None:
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=500)

    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def on_startup():
    global telegram_app

    if not BOT_TOKEN:
        raise ValueError("ATTENDANCE_BOT_TOKEN غير موجود")
    if not WEBHOOK_URL:
        raise ValueError("WEBHOOK_URL غير موجود")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON غير موجود")

    telegram_app = build_telegram_app()
    await telegram_app.initialize()
    await telegram_app.start()

    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    await telegram_app.bot.set_webhook(WEBHOOK_URL)

    print("Bot is running with webhook...")
    print("ADMIN_ID =", ADMIN_ID)
    print("GOOGLE_SHEET_NAME =", GOOGLE_SHEET_NAME)
    print("WEBHOOK_URL =", WEBHOOK_URL)


async def on_shutdown():
    global telegram_app

    if telegram_app is not None:
        try:
            await telegram_app.bot.delete_webhook()
        except Exception:
            pass

        await telegram_app.stop()
        await telegram_app.shutdown()


starlette_app = Starlette(
    debug=False,
    routes=[
        Route("/", home, methods=["GET"]),
        Route("/healthz", healthcheck, methods=["GET"]),
        Route("/webhook", telegram_webhook, methods=["POST"]),
    ],
    on_startup=[on_startup],
    on_shutdown=[on_shutdown],
)


if __name__ == "__main__":
    uvicorn.run(starlette_app, host="0.0.0.0", port=PORT)
