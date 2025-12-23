import os
import time
import logging
from typing import Dict

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# ------------------------
# LOGGING
# ------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------
# CONFIG / CLIENTES
# ------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_AI_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ASSISTANT_ID = os.environ.get("OPENAI_ASSISTANT_ID")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_AI_BOT_TOKEN no encontrado!")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY no encontrado!")
if not ASSISTANT_ID:
    raise ValueError("OPENAI_ASSISTANT_ID no encontrado!")

client = OpenAI(api_key=OPENAI_API_KEY)

# Mapa: telegram_user_id -> thread_id
user_threads: Dict[int, str] = {}


# ------------------------
# OPENAI HELPERS
# ------------------------
def get_or_create_thread(user_id: int) -> str:
    if user_id in user_threads:
        return user_threads[user_id]

    thread = client.beta.threads.create()
    user_threads[user_id] = thread.id
    logger.info(f"Nuevo thread creado para user {user_id}: {thread.id}")
    return thread.id


def reset_thread(user_id: int):
    thread = client.beta.threads.create()
    user_threads[user_id] = thread.id
    logger.info(f"Thread reiniciado para user {user_id}: {thread.id}")


def run_assistant(thread_id: str) -> str:
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
        return "⚠️ Hubo un problema generando la respuesta. Intenta de nuevo."

    messages = client.beta.threads.messages.list(thread_id=thread_id)

    for msg in messages.data:
        if msg.role == "assistant":
            parts = []
            for c in msg.content:
                if c.type == "text":
                    parts.append(c.text.value)
            return "\n".join(parts)

    return "⚠️ No encontré respuesta del asistente."


# ------------------------
# TELEGRAM HANDLERS
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_or_create_thread(user_id)

    await update.message.reply_text(
        "🛍️ ¡Hola! Soy el asistente oficial de la tienda online.\n"
        "Puedo ayudarte con productos, pedidos, envíos, devoluciones y más.\n\n"
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

    await update.message.chat.send_action("typing")

    try:
        thread_id = get_or_create_thread(user_id)

        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=text,
        )

        respuesta = run_assistant(thread_id)
        await update.message.reply_text(respuesta)

    except Exception as e:
        logger.exception("Error procesando mensaje")
        await update.message.reply_text(
            "⚠️ Ocurrió un error al comunicar con el asistente. Intenta nuevamente."
        )


# ------------------------
# MAIN
# ------------------------
def main():
    logger.info("Bot IA iniciado!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()

