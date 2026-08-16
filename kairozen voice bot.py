"""
Kairozen Voice Clone Bot
=========================
Telegram bot (pyTelegramBotAPI) that lets users clone a voice from a short
sample and generate new speech in that voice, using the XTTS-v2 API running
on Colab (or RunPod / any server) via voice_clone_api.py.

FLOW:
    /clonevoice          -> bot asks for a voice sample
    user sends voice/audio -> bot saves it, asks for text
    user sends text       -> bot calls the API, sends back cloned audio

DEPLOY:
    This bot itself can run on Render (like your other bots) since it's
    lightweight — it just relays requests to the heavy XTTS-v2 server.

REQUIREMENTS:
    pip install pyTelegramBotAPI flask requests

ENV VARS (set these in Render dashboard, don't hardcode secrets):
    BOT_TOKEN       - your Telegram bot token from @BotFather
    VOICE_API_URL   - the ngrok/RunPod URL of voice_clone_api.py, e.g.
                       https://contents-zoning-spongy.ngrok-free.dev
                       (NOTE: ngrok free URLs change on every restart —
                       update this env var each time you redeploy Colab)
"""

import os
import logging
import tempfile

import requests
import telebot
from telebot import types
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kairozen_voice_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
VOICE_API_BASE = os.environ.get("VOICE_API_URL", "https://contents-zoning-spongy.ngrok-free.dev").rstrip("/")
VOICE_API_URL = f"{VOICE_API_BASE}/clone"
VOICE_API_HEALTH = f"{VOICE_API_BASE}/health"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory per-user state (fine for small bots; use DATA_DIR/JSON if you
# want it to survive restarts, matching your other Kairozen bots' pattern)
# ---------------------------------------------------------------------------
# user_id -> path to their saved reference voice sample
user_reference_audio = {}
# user_id -> language code they picked (default "en")
user_language = {}


LANGUAGE_CHOICES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "zh-cn": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    bot.reply_to(
        message,
        "👋 <b>Kairozen Voice Clone Bot</b>\n\n"
        "ខ្ញុំអាចយកសំឡេងគំរូខ្លីៗរបស់អ្នក ហើយបង្កើតសំឡេងនិយាយអត្ថបទថ្មីតាមសំឡេងនោះបាន!\n\n"
        "ប្រើ /clonevoice ដើម្បីចាប់ផ្តើម។"
    )


@bot.message_handler(commands=["clonevoice"])
def handle_clonevoice(message):
    user_reference_audio.pop(message.from_user.id, None)
    msg = bot.reply_to(
        message,
        "🎤 ផ្ញើសំឡេងគំរូមក (voice note ឬ audio file, ប្រវែង 6-30 វិនាទី)។",
    )
    bot.register_next_step_handler(msg, receive_reference_audio)


def receive_reference_audio(message):
    user_id = message.from_user.id

    file_id = None
    if message.content_type == "voice":
        file_id = message.voice.file_id
    elif message.content_type == "audio":
        file_id = message.audio.file_id
    elif message.content_type == "document" and message.document.mime_type and "audio" in message.document.mime_type:
        file_id = message.document.file_id

    if not file_id:
        msg = bot.reply_to(message, "⚠️ សូមផ្ញើ voice note ឬ audio file។ សាកម្តងទៀត:")
        bot.register_next_step_handler(msg, receive_reference_audio)
        return

    file_info = bot.get_file(file_id)
    downloaded = bot.download_file(file_info.file_path)

    ref_path = os.path.join(tempfile.gettempdir(), f"ref_{user_id}.ogg")
    with open(ref_path, "wb") as f:
        f.write(downloaded)

    user_reference_audio[user_id] = ref_path

    # ask for language
    markup = types.InlineKeyboardMarkup(row_width=3)
    buttons = [types.InlineKeyboardButton(name, callback_data=f"lang_{code}") for code, name in LANGUAGE_CHOICES.items()]
    markup.add(*buttons)

    bot.reply_to(
        message,
        "✅ ទទួលបានសំឡេងគំរូហើយ! ជ្រើសរើសភាសាសម្រាប់អត្ថបទ:",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def handle_language_choice(call):
    user_id = call.from_user.id
    lang_code = call.data.replace("lang_", "")
    user_language[user_id] = lang_code

    bot.answer_callback_query(call.id, f"ភាសា: {LANGUAGE_CHOICES.get(lang_code, lang_code)}")
    msg = bot.send_message(call.message.chat.id, "✍️ ឥឡូវផ្ញើអត្ថបទដែលអ្នកចង់ឱ្យនិយាយ:")
    bot.register_next_step_handler(msg, receive_text_and_generate)


def receive_text_and_generate(message):
    user_id = message.from_user.id
    text = (message.text or "").strip()

    if not text:
        msg = bot.reply_to(message, "⚠️ សូមផ្ញើអត្ថបទជា text។ សាកម្តងទៀត:")
        bot.register_next_step_handler(msg, receive_text_and_generate)
        return

    ref_path = user_reference_audio.get(user_id)
    if not ref_path or not os.path.exists(ref_path):
        bot.reply_to(message, "⚠️ រកមិនឃើញសំឡេងគំរូទេ សូម /clonevoice ម្តងទៀត។")
        return

    language = user_language.get(user_id, "en")

    status_msg = bot.reply_to(message, "🔄 កំពុងបង្កើតសំឡេង... (អាចចំណាយពេលរហូតដល់មួយនាទី)")

    # ngrok free tier shows an HTML "warning" interstitial to non-browser
    # clients unless this header is passed — without it we'd get an HTML
    # page back instead of the actual API response.
    ngrok_headers = {"ngrok-skip-browser-warning": "true"}

    try:
        with open(ref_path, "rb") as f:
            files = {"speaker_wav": f}
            data = {"text": text, "language": language}
            resp = requests.post(VOICE_API_URL, files=files, data=data, headers=ngrok_headers, timeout=180)

        if resp.status_code != 200:
            logger.error(f"API error: {resp.status_code} {resp.text[:500]}")
            # Response body might be HTML (e.g. ngrok warning page, or a
            # server error page) — never forward it raw into an HTML-parsed
            # Telegram message. Strip it down to plain text safely.
            safe_snippet = resp.text[:200].replace("<", "‹").replace(">", "›")
            bot.edit_message_text(
                f"❌ បរាជ័យ (status {resp.status_code}): {safe_snippet}",
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                parse_mode=None,
            )
            return

        out_path = os.path.join(tempfile.gettempdir(), f"cloned_{user_id}.wav")
        with open(out_path, "wb") as f:
            f.write(resp.content)

        with open(out_path, "rb") as audio_file:
            bot.send_voice(message.chat.id, audio_file)

        bot.delete_message(status_msg.chat.id, status_msg.message_id)
        os.remove(out_path)

    except requests.exceptions.ConnectionError:
        bot.edit_message_text(
            "❌ មិនអាចភ្ជាប់ទៅ voice server បានទេ។ Server (Colab) អាចនឹងបានបិទ ឬ URL ផ្លាស់ប្តូរ — សូមពិនិត្យ VOICE_API_URL។",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
            parse_mode=None,
        )
    except Exception as e:
        logger.exception("Voice generation failed")
        safe_err = str(e)[:200].replace("<", "‹").replace(">", "›")
        bot.edit_message_text(
            f"❌ មានបញ្ហា: {safe_err}",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
            parse_mode=None,
        )


@bot.message_handler(commands=["checkserver"])
def handle_checkserver(message):
    try:
        resp = requests.get(VOICE_API_HEALTH, headers={"ngrok-skip-browser-warning": "true"}, timeout=10)
        if resp.status_code == 200:
            bot.reply_to(message, f"✅ Voice server ដំណើរការធម្មតា\n{VOICE_API_BASE}")
        else:
            bot.reply_to(message, f"⚠️ Server ឆ្លើយតប status {resp.status_code}")
    except Exception as e:
        bot.reply_to(message, f"❌ Server មិនឆ្លើយតបទេ: {e}\n\nប្រហែលជា Colab notebook បានផ្តាច់ — ត្រូវ restart និងធ្វើបច្ចុប្បន្នភាព VOICE_API_URL។")


# ---------------------------------------------------------------------------
# Flask keep-alive + webhook (Render-style, matching your other bots)
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return "Kairozen Voice Clone Bot is running.", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200


def set_webhook():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")  # Render sets this automatically
    if render_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"{render_url}/{BOT_TOKEN}")
        logger.info(f"Webhook set to {render_url}/{BOT_TOKEN}")
    else:
        logger.warning("RENDER_EXTERNAL_URL not set — falling back to polling.")


if __name__ == "__main__":
    if os.environ.get("RENDER_EXTERNAL_URL"):
        set_webhook()
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        # local testing
        bot.remove_webhook()
        logger.info("Starting bot in polling mode (local dev)...")
        bot.infinity_polling()
