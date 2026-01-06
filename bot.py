import os
import time
import logging
import json
from datetime import datetime, time as dtime
from typing import Dict
import io

from telegram import (
    Update,
    InputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI
import matplotlib.pyplot as plt

# ------------------------
# CONFIGURACIÓN
# ------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_AI_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ASSISTANT_ID = os.environ.get("OPENAI_ASSISTANT_ID")
ADMIN_USER_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_AI_BOT_TOKEN no encontrado!")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY no encontrado!")
if not ASSISTANT_ID:
    raise ValueError("OPENAI_ASSISTANT_ID no encontrado!")
if ADMIN_USER_ID == 0:
    raise ValueError("ADMIN_TELEGRAM_ID no definido o inválido!")

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------
# LOGGING
# ------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------
# THREADS POR USUARIO
# ------------------------
user_threads: Dict[int, str] = {}

# ------------------------
# LIMITES DE USO
# ------------------------
USAGE_FILE = "usage.json"

DAILY_MSG_LIMIT = 50
MONTHLY_MSG_LIMIT = 500
MONTHLY_TOKEN_LIMIT = 200000


def load_usage():
    if not os.path.exists(USAGE_FILE):
        return {}
    with open(USAGE_FILE, "r") as f:
        return json.load(f)


def save_usage(data):
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _ensure_usage_structure(usage, user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")

    if str(user_id) not in usage:
        usage[str(user_id)] = {
            "daily": 0,
            "monthly": 0,
            "last_day": today,
            "last_month": month,
            "tokens_today": 0,
            "tokens_month": 0,
        }

    user = usage[str(user_id)]

    if user["last_day"] != today:
        user["daily"] = 0
        user["tokens_today"] = 0
        user["last_day"] = today

    if user["last_month"] != month:
        user["monthly"] = 0
        user["tokens_month"] = 0
        user["last_month"] = month

    return usage, user


def can_user_continue(user_id: int) -> bool:
    usage = load_usage()
    usage, user = _ensure_usage_structure(usage, user_id)

    if user["daily"] >= DAILY_MSG_LIMIT:
        return False
    if user["monthly"] >= MONTHLY_MSG_LIMIT:
        return False
    if user["tokens_month"] >= MONTHLY_TOKEN_LIMIT:
        return False

    return True


def register_usage_after_response(user_id: int, tokens_used: int):
    usage = load_usage()
    usage, user = _ensure_usage_structure(usage, user_id)

    user["daily"] += 1
    user["monthly"] += 1
    user["tokens_today"] += tokens_used
    user["tokens_month"] += tokens_used

    save_usage(usage)


# ------------------------
# LOGS POR USUARIO
# ------------------------
def log_message(user_id: int, role: str, text: str):
    os.makedirs("logs", exist_ok=True)
    filename = f"logs/{user_id}.log"
    with open(filename, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now()}] {role.upper()}: {text}\n")


# ------------------------
# ADMIN CHECK
# ------------------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID


# ------------------------
# OPENAI HELPERS
# ------------------------
def get_or_create_thread(user_id: int) -> str:
    if user_id in user_threads:
        return user_threads[user_id]

    thread = client.beta.threads.create()
    user_threads[user_id] = thread.id
    logger.info(f"Nuevo thread creado para usuario {user_id}: {thread.id}")
    return thread.id


def reset_thread(user_id: int):
    thread = client.beta.threads.create()
    user_threads[user_id] = thread.id
    logger.info(f"Thread reiniciado para usuario {user_id}: {thread.id}")


def run_assistant(thread_id: str):
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
    )

    while run.status in ("queued", "in_progress"):
        time.sleep(1)
        run = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id,
        )

    if run.status != "completed":
        return "⚠️ Hubo un problema generando la respuesta. Inténtalo de nuevo.", 0

    messages = client.beta.threads.messages.list(thread_id=thread_id)

    tokens_used = 0
    try:
        tokens_used = run.usage.total_tokens
    except:
        tokens_used = 0

    for msg in messages.data:
        if msg.role == "assistant":
            parts = []
            for c in msg.content:
                if c.type == "text":
                    parts.append(c.text.value)
            return "\n".join(parts), tokens_used

    return "⚠️ No encontré respuesta del asistente.", tokens_used


# ------------------------
# INFORME SEMANAL (ADMIN)
# ------------------------
def generate_weekly_report_text():
    usage = load_usage()
    lines = ["📊 *Informe semanal*", ""]

    for user_id, data in usage.items():
        lines.append(
            f"👤 Usuario {user_id}\n"
            f"• Mensajes este mes: {data.get('monthly', 0)}\n"
            f"• Tokens este mes: {data.get('tokens_month', 0)}\n"
        )

    if len(lines) == 2:
        lines.append("Aún no hay datos de uso.")

    return "\n".join(lines)


async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        report = generate_weekly_report_text()
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=report, parse_mode="Markdown")

        if os.path.exists("logs"):
            for filename in os.listdir("logs"):
                if filename.endswith(".log"):
                    await context.bot.send_document(
                        chat_id=ADMIN_USER_ID,
                        document=InputFile(os.path.join("logs", filename)),
                        caption=f"Archivo de log: {filename}",
                    )
    except Exception as e:
        logger.exception("Error enviando informe semanal")


# ------------------------
# GRÁFICOS (ADMIN)
# ------------------------
def generate_bar_chart(usage):
    if not usage:
        return None

    users = list(usage.keys())
    tokens = [usage[u].get("tokens_month", 0) for u in users]

    plt.figure(figsize=(10, 5))
    plt.bar(users, tokens, color="skyblue")
    plt.title("Tokens por usuario (mes actual)")
    plt.xlabel("Usuario")
    plt.ylabel("Tokens")
    plt.xticks(rotation=45, ha="right")

    buffer = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buffer, format="png")
    buffer.seek(0)
    plt.close()
    return buffer


def generate_line_chart(usage):
    if not usage:
        return None

    days = {}
    for u in usage.values():
        day = u.get("last_day")
        count = u.get("daily", 0)
        if day:
            days[day] = days.get(day, 0) + count

    x = list(days.keys())
    y = list(days.values())

    if not x:
        return None

    plt.figure(figsize=(10, 5))
    plt.plot(x, y, marker="o", color="green")
    plt.title("Mensajes por día (instantánea)")
    plt.xlabel("Día")
    plt.ylabel("Mensajes")
    plt.xticks(rotation=45, ha="right")

    buffer = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buffer, format="png")
    buffer.seek(0)
    plt.close()
    return buffer


def generate_pie_chart(usage):
    if not usage:
        return None

    users = []
    tokens = []
    for u_id, data in usage.items():
        t = data.get("tokens_month", 0)
        if t > 0:
            users.append(u_id)
            tokens.append(t)

    if not users:
        return None

    plt.figure(figsize=(8, 8))
    plt.pie(tokens, labels=users, autopct="%1.1f%%", startangle=140)
    plt.title("Distribución de tokens por usuario")

    buffer = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buffer, format="png")
    buffer.seek(0)
    plt.close()
    return buffer


# ------------------------
# HANDLERS DE USUARIO
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_or_create_thread(user_id)

    await update.message.reply_text(
        "🛍️ ¡Hola! Soy el asistente oficial de la tienda online.\n"
        "Puedo ayudarte con productos, pedidos, envíos, devoluciones y mucho más.\n\n"
        "Comandos disponibles:\n"
        "/start – Mostrar este mensaje\n"
        "/reset – Reiniciar el contexto de la conversación"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_thread(user_id)
    await update.message.reply_text("🔄 El contexto ha sido reiniciado. Empecemos de nuevo.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if not can_user_continue(user_id):
        await update.message.reply_text("⚠️ Has alcanzado el límite de uso. Inténtalo más tarde.")
        return

    await update.message.chat.send_action("typing")
    log_message(user_id, "user", text)

    try:
        thread_id = get_or_create_thread(user_id)

        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=text,
        )

        respuesta, tokens = run_assistant(thread_id)

        register_usage_after_response(user_id, tokens)

        log_message(user_id, "assistant", respuesta)
        await update.message.reply_text(respuesta)

    except Exception as e:
        logger.exception("Error procesando mensaje")
        await update.message.reply_text(
            "⚠️ Ocurrió un error al comunicar con el asistente. Inténtalo de nuevo."
        )


# ------------------------
# HANDLERS DE ADMIN
# ------------------------
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ No tienes permiso para acceder al panel.")
        return

    keyboard = [
        [InlineKeyboardButton("📊 Estadísticas generales", callback_data="stats")],
        [InlineKeyboardButton("📁 Enviar logs", callback_data="send_logs")],
        [InlineKeyboardButton("👤 Estadísticas de usuario", callback_data="ask_user_id")],
        [InlineKeyboardButton("📈 Gráficos", callback_data="charts")],
    ]

    await update.message.reply_text(
        "Panel administrativo:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ No tienes permiso para ver logs.")
        return

    if not os.path.exists("logs"):
        await update.message.reply_text("No hay logs disponibles.")
        return

    for filename in os.listdir("logs"):
        if filename.endswith(".log"):
            await update.message.reply_document(
                document=InputFile(os.path.join("logs", filename)),
                caption=f"Archivo de log: {filename}",
            )


async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ No tienes permiso.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /userstats <telegram_id>")
        return

    target = context.args[0]
    usage_all = load_usage()
    usage = usage_all.get(target)

    if not usage:
        await update.message.reply_text("Usuario no encontrado en el registro.")
        return

    await update.message.reply_text(
        f"📊 *Estadísticas del usuario {target}*\n\n"
        f"Mensajes hoy: {usage.get('daily', 0)} / {DAILY_MSG_LIMIT}\n"
        f"Mensajes este mes: {usage.get('monthly', 0)} / {MONTHLY_MSG_LIMIT}\n"
        f"Tokens hoy: {usage.get('tokens_today', 0)}\n"
        f"Tokens este mes: {usage.get('tokens_month', 0)} / {MONTHLY_TOKEN_LIMIT}",
        parse_mode="Markdown",
    )


async def charts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ No tienes permiso para ver gráficos.")
        return

    usage = load_usage()

    if not usage:
        await update.message.reply_text("Aún no hay datos para generar gráficos.")
        return

    bar = generate_bar_chart(usage)
    if bar:
        await update.message.reply_photo(bar, caption="📊 Tokens por usuario (mes)")

    line = generate_line_chart(usage)
    if line:
        await update.message.reply_photo(line, caption="📈 Mensajes por día (instantánea)")

    pie = generate_pie_chart(usage)
    if pie:
        await update.message.reply_photo(pie, caption="🧩 Distribución de tokens por usuario")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.edit_message_text("⛔ No tienes permiso.")
        return

    data = query.data

    if data == "stats":
        usage = load_usage()
        total_users = len(usage)
        total_tokens = sum(u.get("tokens_month", 0) for u in usage.values())
        total_msgs = sum(u.get("monthly", 0) for u in usage.values())

        await query.edit_message_text(
            f"📊 Estadísticas generales\n\n"
            f"👥 Usuarios registrados: {total_users}\n"
            f"💬 Mensajes este mes (total): {total_msgs}\n"
            f"🔢 Tokens este mes (total): {total_tokens}"
        )

    elif data == "send_logs":
        if not os.path.exists("logs"):
            await query.edit_message_text("No hay logs disponibles.")
            return

        await query.edit_message_text("Enviando logs...")
        for filename in os.listdir("logs"):
            if filename.endswith(".log"):
                await query.message.reply_document(
                    document=InputFile(os.path.join("logs", filename)),
                    caption=f"Archivo de log: {filename}",
                )

    elif data == "ask_user_id":
        await query.edit_message_text("Envía el comando:\n/userstats <telegram_id>")

    elif data == "charts":
        usage = load_usage()
        if not usage:
            await query.edit_message_text("Aún no hay datos para generar gráficos.")
            return

        await query.edit_message_text("Generando gráficos...")

        bar = generate_bar_chart(usage)
        if bar:
            await query.message.reply_photo(bar, caption="📊 Tokens por usuario (mes)")

        line = generate_line_chart(usage)
        if line:
            await query.message.reply_photo(line, caption="📈 Mensajes por día (instantánea)")

        pie = generate_pie_chart(usage)
        if pie:
            await query.message.reply_photo(pie, caption="🧩 Distribución de tokens por usuario")


# ------------------------
# MAIN
# ------------------------
def main():
    logger.info("Bot IA 2.0 ES iniciado!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_handler(CommandHandler("dashboard", admin_dashboard))
    app.add_handler(CommandHandler("logs", send_logs_command))
    app.add_handler(CommandHandler("userstats", userstats))
    app.add_handler(CommandHandler("charts", charts_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_daily(
        weekly_report,
        time=dtime(hour=8, minute=0),
        days=(0,),
    )

    app.run_polling()


if __name__ == "__main__":
    main()
