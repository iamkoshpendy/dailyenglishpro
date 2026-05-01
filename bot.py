import logging
import os
from datetime import datetime, time, timedelta
import pytz
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from supabase import create_client
import anthropic
import openai
import tempfile

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ENV
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Conversation states
CHOOSE_TRACK, CHOOSE_TIMEZONE, CHOOSE_TIME = range(3)

TRACKS = {
    "💼 Business English": "business",
    "🗣 Conversational English": "conversational"
}

TIMEZONES = {
    "🇧🇷 Brazil (UTC-3)": "America/Sao_Paulo",
    "🇬🇧 UK (UTC+0)": "Europe/London",
    "🇪🇺 Europe (UTC+1)": "Europe/Berlin",
    "🇷🇺 Moscow (UTC+3)": "Europe/Moscow",
    "🇰🇿 Kazakhstan (UTC+5)": "Asia/Almaty",
    "🇺🇸 US East (UTC-5)": "America/New_York",
    "🇺🇸 US West (UTC-8)": "America/Los_Angeles",
}

TIMES = ["07:00", "08:00", "09:00", "10:00", "12:00",
         "15:00", "18:00", "19:00", "20:00", "21:00", "22:00"]

# ─── SUPABASE HELPERS ────────────────────────────────────────

def get_user(telegram_id: int):
    res = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    return res.data[0] if res.data else None

def upsert_user(telegram_id: int, data: dict):
    existing = get_user(telegram_id)
    if existing:
        supabase.table("users").update(data).eq("telegram_id", telegram_id).execute()
    else:
        supabase.table("users").insert({"telegram_id": telegram_id, **data}).execute()

def get_task_for_today(track: str, skill: str):
    today = datetime.now(pytz.utc).strftime("%Y-%m-%d")
    res = supabase.table("tasks").select("*")\
        .eq("track", track)\
        .eq("skill", skill)\
        .eq("scheduled_date", today)\
        .execute()
    return res.data[0] if res.data else None

def get_task_by_id(task_id: int):
    res = supabase.table("tasks").select("*").eq("id", task_id).execute()
    return res.data[0] if res.data else None

def set_active_task(telegram_id: int, task_id: int):
    upsert_user(telegram_id, {"current_task_id": task_id})

def clear_active_task(telegram_id: int):
    upsert_user(telegram_id, {"current_task_id": None})

def calculate_xp(streak: int) -> int:
    base = 10
    if streak >= 30:
        return base + 300
    elif streak >= 7:
        return base + 70
    elif streak >= 3:
        return base + 30
    return base

def update_streak_and_xp(telegram_id: int):
    user = get_user(telegram_id)
    today = datetime.now(pytz.utc).date()
    last = user.get("last_task_date")

    if last:
        last_date = datetime.fromisoformat(str(last)).date()
        diff = (today - last_date).days
        if diff == 0:
            # Already completed a task today, still give XP but don't change streak
            new_streak = user["streak"]
        elif diff == 1:
            new_streak = user["streak"] + 1
        else:
            new_streak = 1
    else:
        new_streak = 1

    xp_gained = calculate_xp(new_streak)
    new_xp = user.get("xp", 0) + xp_gained

    upsert_user(telegram_id, {
        "streak": new_streak,
        "xp": new_xp,
        "last_task_date": today.isoformat()
    })
    return new_streak, xp_gained, new_xp

# ─── VOICE TRANSCRIPTION ─────────────────────────────────────

async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="en"
        )
    return transcript.text

# ─── AI FEEDBACK ─────────────────────────────────────────────

def get_ai_feedback(task_content: str, user_answer: str, skill: str, is_transcribed: bool = False) -> str:
    if skill == "speaking":
        prompt = f"""You are a warm, encouraging English teacher giving feedback on a speaking exercise.
The student recorded a voice message which was transcribed to text.

Task: {task_content}
Transcribed answer: {user_answer}

Give feedback using EXACTLY this format — each point on its own line:

✅ [One specific thing they said well — content or language]

💡 [One grammar or vocabulary improvement with a corrected example in quotes]

🗣 [One tip specifically for speaking — pronunciation, fluency, or natural phrasing]

💪 [One short encouraging closing sentence]

Rules:
- Keep each point to 1-2 sentences max
- Be concrete and specific, never generic
- Always include a real corrected example in the 💡 point"""
    else:
        prompt = f"""You are a warm, encouraging English teacher giving feedback to a B1-B2 student.

Task type: {skill}
Task: {task_content}
Student's answer: {user_answer}

Give feedback using EXACTLY this format — each point on its own line, never merged into one paragraph:

✅ [One specific thing they did well — be concrete, not generic]

💡 [One clear improvement with a corrected example in quotes]

💪 [One short encouraging closing sentence]

Rules:
- Keep each point to 1-2 sentences max
- Never start with "Great job!" or "Well done!"
- Always include a real corrected example in the 💡 point"""

    message = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ─── TASK DELIVERY ───────────────────────────────────────────

async def send_task(chat_id: int, track: str, skill: str, context: ContextTypes.DEFAULT_TYPE, is_second=False):
    task = get_task_for_today(track, skill)

    if not task:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Today's task isn't ready yet. The admin will generate tasks soon — try again in a bit!"
        )
        return

    set_active_task(chat_id, task["id"])

    skill_emoji = "✍️" if skill == "writing" else "🗣"
    intro = "And here's your second task for today! 💪" if is_second else "Here's your task for today!"

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{intro}\n\n"
             f"{skill_emoji} *{skill.capitalize()} Task*\n\n"
             f"{task['content']}\n\n"
             f"_Reply with your answer below 👇_",
        parse_mode="Markdown"
    )

# ─── ONBOARDING ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    if user and user.get("track"):
        await update.message.reply_text(
            "👋 Welcome back! Use /task to get today's tasks or /stats to see your progress."
        )
        return ConversationHandler.END

    keyboard = [[track] for track in TRACKS.keys()]
    await update.message.reply_text(
        "👋 *Welcome to DailyEnglish Bot!*\n\n"
        "Practice English just *10 minutes a day* and feel real progress every week.\n\n"
        "Every day you'll get *2 tasks* — one writing, one speaking. "
        "Answer them, get instant AI feedback, earn XP and build your streak.\n\n"
        "━━━━━━━━━━━━━━━\n"
        "*Step 1 of 3:* Choose your learning track 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return CHOOSE_TRACK

async def choose_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chosen = update.message.text
    if chosen not in TRACKS:
        await update.message.reply_text("Please choose one of the options below 👇")
        return CHOOSE_TRACK

    context.user_data["track"] = TRACKS[chosen]

    keyboard = [[tz] for tz in TIMEZONES.keys()]
    await update.message.reply_text(
        f"✅ *{chosen}* — great choice!\n\n"
        "━━━━━━━━━━━━━━━\n"
        "*Step 2 of 3:* Where are you located? 🌍",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return CHOOSE_TIMEZONE

async def choose_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chosen = update.message.text
    if chosen not in TIMEZONES:
        await update.message.reply_text("Please choose one of the options below 👇")
        return CHOOSE_TIMEZONE

    context.user_data["timezone"] = TIMEZONES[chosen]

    # Build time keyboard (2 per row)
    time_buttons = [TIMES[i:i+2] for i in range(0, len(TIMES), 2)]
    await update.message.reply_text(
        "✅ Got it!\n\n"
        "━━━━━━━━━━━━━━━\n"
        "*Step 3 of 3:* What time should I send your daily tasks? ⏰\n\n"
        "_Pick a time when you know you'll have 10 free minutes and a clear head._",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(time_buttons, one_time_keyboard=True, resize_keyboard=True)
    )
    return CHOOSE_TIME

async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chosen = update.message.text.strip()
    if chosen not in TIMES:
        await update.message.reply_text("Please choose one of the time options below 👇")
        return CHOOSE_TIME

    telegram_id = update.effective_user.id
    track = context.user_data["track"]
    timezone_str = context.user_data["timezone"]

    # Convert local time to UTC for scheduling
    tz = pytz.timezone(timezone_str)
    hour, minute = map(int, chosen.split(":"))
    local_time = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    utc_time = local_time.astimezone(pytz.utc)

    upsert_user(telegram_id, {
        "telegram_id": telegram_id,
        "track": track,
        "timezone": timezone_str,
        "notification_time": chosen,
        "notification_utc": f"{utc_time.hour:02d}:{utc_time.minute:02d}",
        "xp": 0,
        "streak": 0,
        "last_task_date": None,
        "current_task_id": None,
        "tasks_completed_today": 0
    })

    # Schedule daily notification
    context.job_queue.run_daily(
        send_daily_tasks,
        time=time(hour=utc_time.hour, minute=utc_time.minute, tzinfo=pytz.utc),
        chat_id=telegram_id,
        name=str(telegram_id)
    )

    track_name = "Business English" if track == "business" else "Conversational English"

    await update.message.reply_text(
        f"🎉 *You're all set!*\n\n"
        f"📚 Track: *{track_name}*\n"
        f"⏰ Daily tasks at: *{chosen}* your time\n\n"
        f"Let's start right now — here's your first task! 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )

    # Send first task immediately
    await send_task(telegram_id, track, "writing", context)
    return ConversationHandler.END

# ─── DAILY PUSH ──────────────────────────────────────────────

async def send_daily_tasks(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    user = get_user(chat_id)
    if not user:
        return

    # Reset daily counter
    upsert_user(chat_id, {"tasks_completed_today": 0, "current_task_id": None})

    await context.bot.send_message(
        chat_id=chat_id,
        text="🌅 *Good morning! Your daily English practice is ready.*\n\n"
             "2 tasks today — writing + speaking. Takes about 10 minutes. Let's go! 💪",
        parse_mode="Markdown"
    )
    await send_task(chat_id, user["track"], "writing", context)

# ─── ANSWER HANDLER ──────────────────────────────────────────

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)

    # Not registered yet
    if not user or not user.get("track"):
        await update.message.reply_text(
            "👋 Looks like you haven't started yet! Send /start to set up your account."
        )
        return

    # No active task check for voice
    task_id = user.get("current_task_id")

    # Voice message — transcribe with Whisper
    is_transcribed = False
    if update.message.voice:
        if not task_id:
            await update.message.reply_text(
                "🎙 Use /task to get your speaking task first, then send a voice message!"
            )
            return
        task = get_task_by_id(task_id)
        if not task or task["skill"] != "speaking":
            await update.message.reply_text(
                "🎙 Voice messages are only for speaking tasks.\n\nFinish the writing task first, then you'll get the speaking task!"
            )
            return
        await update.message.reply_text("🎙 Transcribing your voice message...")
        try:
            voice_file = await update.message.voice.get_file()
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                await voice_file.download_to_drive(tmp.name)
                user_answer = await transcribe_voice(tmp.name)
                os.unlink(tmp.name)
            is_transcribed = True
            await update.message.reply_text(
                f"📝 *I heard:*\n_{user_answer}_\n\n⏳ Analyzing...",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            await update.message.reply_text(
                "❌ Couldn't process your voice message. Please try again or type your answer."
            )
            return
    else:
        user_answer = update.message.text

    # No active task
    if not task_id:
        completed = user.get("tasks_completed_today", 0)
        if completed >= 2:
            await update.message.reply_text(
                "✅ You've already completed both tasks for today! Come back tomorrow.\n\n"
                "Check your progress with /stats 📊"
            )
        else:
            await update.message.reply_text(
                "Use /task to get your daily tasks! 📚"
            )
        return

    task = get_task_by_id(task_id)
    if not task:
        await update.message.reply_text("Something went wrong. Use /task to try again.")
        return

    if not is_transcribed:
        await update.message.reply_text("⏳ Reviewing your answer...")

    try:
        feedback = get_ai_feedback(task["content"], user_answer, task["skill"], is_transcribed)
    except Exception as e:
        logger.error(f"AI feedback error: {e}")
        feedback = "Good effort! Keep practicing — consistency is what builds real progress."

    # Update streak and XP
    new_streak, xp_gained, total_xp = update_streak_and_xp(telegram_id)

    # Save answer
    supabase.table("answers").insert({
        "telegram_id": telegram_id,
        "task_id": task_id,
        "answer": user_answer,
        "feedback": feedback,
        "created_at": datetime.now(pytz.utc).isoformat()
    }).execute()

    # Clear active task and update completed count
    completed_today = (user.get("tasks_completed_today") or 0) + 1
    upsert_user(telegram_id, {
        "current_task_id": None,
        "tasks_completed_today": completed_today
    })

    # Send feedback
    await update.message.reply_text(
        f"📝 *Feedback:*\n\n{feedback}",
        parse_mode="Markdown"
    )

    # Send congratulation separately
    streak_emoji = "🔥" if new_streak > 1 else "✅"
    bonus_note = f" _(+{xp_gained - 10} streak bonus!)_" if xp_gained > 10 else ""

    await update.message.reply_text(
        f"{streak_emoji} *Task complete!*\n\n"
        f"⭐ +{xp_gained} XP{bonus_note}\n"
        f"📈 Total: {total_xp} XP\n"
        f"🔥 Streak: {new_streak} {'day' if new_streak == 1 else 'days'}",
        parse_mode="Markdown"
    )

    # Send second task if first is done
    if completed_today == 1:
        await update.message.reply_text("Now let's do the speaking task! 🗣")
        await send_task(telegram_id, user["track"], "speaking", context, is_second=True)

# ─── COMMANDS ────────────────────────────────────────────────

async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)

    if not user or not user.get("track"):
        await update.message.reply_text("Please start with /start first!")
        return

    completed = user.get("tasks_completed_today", 0)

    if completed >= 2:
        await update.message.reply_text(
            "✅ You've completed both tasks for today! Great job.\n\n"
            "Come back tomorrow or check /stats 📊"
        )
        return

    if completed == 0:
        upsert_user(telegram_id, {"current_task_id": None})
        await send_task(telegram_id, user["track"], "writing", context)
    else:
        upsert_user(telegram_id, {"current_task_id": None})
        await send_task(telegram_id, user["track"], "speaking", context, is_second=True)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please start with /start first!")
        return

    track_name = "Business English" if user.get("track") == "business" else "Conversational English"
    streak = user.get("streak", 0)
    xp = user.get("xp", 0)
    completed_today = user.get("tasks_completed_today", 0)

    if xp < 100:
        level, next_level = "🌱 Beginner", 100
    elif xp < 500:
        level, next_level = "📗 Elementary", 500
    elif xp < 1500:
        level, next_level = "📘 Intermediate", 1500
    else:
        level, next_level = "📙 Advanced", None

    progress = f"{xp}/{next_level} XP to next level" if next_level else "Max level reached! 🏆"
    streak_emoji = "🔥" if streak >= 3 else "✅" if streak > 0 else "💤"

    await update.message.reply_text(
        f"📊 *Your Progress*\n\n"
        f"🎯 Track: {track_name}\n"
        f"{streak_emoji} Streak: {streak} {'day' if streak == 1 else 'days'}\n"
        f"⭐ XP: {xp}\n"
        f"🏆 Level: {level}\n"
        f"📈 {progress}\n\n"
        f"Today: {'✅✅ Both tasks done!' if completed_today >= 2 else '✅⬜ 1/2 done' if completed_today == 1 else '⬜⬜ No tasks yet'}",
        parse_mode="Markdown"
    )

async def generate_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    await update.message.reply_text("⏳ Generating tasks for the next 7 days...")

    tracks = ["business", "conversational"]
    skills = ["writing", "speaking"]
    today = datetime.now(pytz.utc).date()

    generated = 0
    for day_offset in range(7):
        task_date = today + timedelta(days=day_offset)
        for track in tracks:
            for skill in skills:
                existing = supabase.table("tasks").select("id")\
                    .eq("track", track)\
                    .eq("skill", skill)\
                    .eq("scheduled_date", task_date.isoformat())\
                    .execute()
                if existing.data:
                    continue

                prompt = f"""Generate a {skill} task for an English learner (B1-B2 level).
Track: {track} English
Skill: {skill}

{'For writing: give a realistic scenario and ask them to write 3-5 sentences (email, report snippet, message, feedback).' if skill == 'writing' else 'For speaking: give a dialogue scenario or open question to respond to in 3-5 sentences.'}

Format the task EXACTLY like this — two parts separated by a blank line:

[One sentence describing the scenario or context]

[Clear instruction telling them what to write or say]

Rules:
- Each part on its own line with a blank line between them
- Keep it practical and relevant to real work or daily life
- No labels, headers, or meta-commentary
- Total length: under 60 words"""

                try:
                    message = anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=200,
                        messages=[{"role": "user", "content": prompt}]
                    )
                    task_content = message.content[0].text.strip()

                    supabase.table("tasks").insert({
                        "track": track,
                        "skill": skill,
                        "scheduled_date": task_date.isoformat(),
                        "content": task_content
                    }).execute()
                    generated += 1
                except Exception as e:
                    logger.error(f"Task generation error: {e}")

    await update.message.reply_text(
        f"✅ Done! Generated {generated} tasks for the next 7 days.\n\n"
        f"Check them in Supabase → tasks table."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ─── MAIN ────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_TRACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_track)],
            CHOOSE_TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_timezone)],
            CHOOSE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("generate", generate_tasks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer))
    app.add_handler(MessageHandler(filters.VOICE, handle_answer))

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
