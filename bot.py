import logging
import os
import json
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

FREE_DAYS = 7
MAX_NEW_CHUNKS_PER_DAY = 5
MAX_CHUNKS_TOTAL = 200
FLASHCARD_SESSION_SIZE = 5

CHOOSE_TRACK, CHOOSE_TIMEZONE, CHOOSE_TIME = range(3)
FLASHCARD_ANSWER = 10

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

# Spaced repetition intervals in days
SR_INTERVALS = [1, 3, 7, 14, 30]

# ─── SUPABASE HELPERS ────────────────────────────────────────

def get_user(telegram_id: int):
    res = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    return res.data[0] if res.data else None

def get_all_users():
    res = supabase.table("users").select("*").not_.is_("track", "null").execute()
    return res.data or []

def upsert_user(telegram_id: int, data: dict):
    existing = get_user(telegram_id)
    if existing:
        supabase.table("users").update(data).eq("telegram_id", telegram_id).execute()
    else:
        supabase.table("users").insert({"telegram_id": telegram_id, **data}).execute()

def get_task_for_today(track: str, skill: str):
    today = datetime.now(pytz.utc).strftime("%Y-%m-%d")
    res = supabase.table("tasks").select("*")\
        .eq("track", track).eq("skill", skill).eq("scheduled_date", today).execute()
    return res.data[0] if res.data else None

def get_task_by_id(task_id: int):
    res = supabase.table("tasks").select("*").eq("id", task_id).execute()
    return res.data[0] if res.data else None

def is_free_period_over(user: dict) -> bool:
    if user.get("is_paid"):
        return False
    created = user.get("created_at")
    if not created:
        return False
    created_date = datetime.fromisoformat(str(created)).replace(tzinfo=pytz.utc)
    return (datetime.now(pytz.utc) - created_date).days >= FREE_DAYS

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
        new_streak = user["streak"] if diff == 0 else (user["streak"] + 1 if diff == 1 else 1)
    else:
        new_streak = 1
    xp_gained = calculate_xp(new_streak)
    new_xp = user.get("xp", 0) + xp_gained
    upsert_user(telegram_id, {"streak": new_streak, "xp": new_xp, "last_task_date": today.isoformat()})
    return new_streak, xp_gained, new_xp

# ─── FLASHCARD HELPERS ───────────────────────────────────────

def save_chunks(telegram_id: int, chunks: list, task_id: int):
    """Save new chunks respecting daily and total limits."""
    today = datetime.now(pytz.utc).strftime("%Y-%m-%d")

    # Count chunks added today
    added_today = supabase.table("flashcards").select("id")\
        .eq("telegram_id", telegram_id)\
        .gte("created_at", today)\
        .execute()
    slots_left = MAX_NEW_CHUNKS_PER_DAY - len(added_today.data or [])
    if slots_left <= 0:
        return

    # Count total chunks
    total = supabase.table("flashcards").select("id")\
        .eq("telegram_id", telegram_id)\
        .eq("archived", False)\
        .execute()
    if len(total.data or []) >= MAX_CHUNKS_TOTAL:
        return

    for chunk in chunks[:slots_left]:
        try:
            supabase.table("flashcards").insert({
                "telegram_id": telegram_id,
                "chunk": chunk["phrase"],
                "original": chunk["original"],
                "example": chunk["example"],
                "task_id": task_id,
                "review_count": 0,
                "correct_streak": 0,
                "next_review_date": today,
                "archived": False,
                "created_at": datetime.now(pytz.utc).isoformat()
            }).execute()
        except Exception as e:
            logger.error(f"Chunk save error: {e}")

def get_flashcards_for_review(telegram_id: int) -> list:
    """Get up to 5 chunks due for review, prioritizing overdue ones."""
    today = datetime.now(pytz.utc).strftime("%Y-%m-%d")

    # Overdue first
    overdue = supabase.table("flashcards").select("*")\
        .eq("telegram_id", telegram_id)\
        .eq("archived", False)\
        .lte("next_review_date", today)\
        .order("next_review_date")\
        .limit(FLASHCARD_SESSION_SIZE)\
        .execute()

    return overdue.data or []

def update_flashcard_after_review(card_id: int, correct: bool):
    """Update spaced repetition interval based on answer."""
    card = supabase.table("flashcards").select("*").eq("id", card_id).execute()
    if not card.data:
        return
    card = card.data[0]

    if correct:
        new_correct_streak = card.get("correct_streak", 0) + 1
        interval_idx = min(new_correct_streak, len(SR_INTERVALS) - 1)
        interval = SR_INTERVALS[interval_idx]
        next_review = (datetime.now(pytz.utc) + timedelta(days=interval)).strftime("%Y-%m-%d")
        archived = new_correct_streak >= len(SR_INTERVALS)
    else:
        new_correct_streak = 0
        next_review = datetime.now(pytz.utc).strftime("%Y-%m-%d")
        archived = False

    supabase.table("flashcards").update({
        "correct_streak": new_correct_streak,
        "next_review_date": next_review,
        "review_count": card.get("review_count", 0) + 1,
        "archived": archived,
        "last_reviewed_at": datetime.now(pytz.utc).isoformat()
    }).eq("id", card_id).execute()

# ─── VOICE TRANSCRIPTION ─────────────────────────────────────

async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, language="en"
        )
    return transcript.text

# ─── AI: FEEDBACK + CHUNKS (unified) ────────────────────────

def get_feedback_and_chunks(task_content: str, user_answer: str, skill: str, is_transcribed: bool = False) -> dict:
    """Single AI call that returns feedback AND chunk suggestions from the user's answer."""

    if skill == "speaking":
        skill_instructions = """For speaking feedback:
- Note natural phrasing and fluency
- Add a 🗣 tip about how to say it more naturally"""
    else:
        skill_instructions = """For writing feedback:
- Focus on structure and professional tone"""

    prompt = f"""You are an English teacher analyzing a B1-B2 student's answer.

Task: {task_content}
Student's answer: {user_answer}
Skill: {skill}
{skill_instructions}

Return a JSON object with exactly this structure:
{{
  "feedback": {{
    "good": "One specific thing they did well (1-2 sentences, concrete)",
    "improve": "One grammar/vocabulary improvement with corrected example in quotes",
    "tip": "One natural phrasing tip (for speaking) or style tip (for writing)",
    "encouragement": "One short encouraging sentence"
  }},
  "chunks": [
    {{
      "original": "exact phrase the student used",
      "phrase": "better/more natural English phrase",
      "example": "full sentence showing the chunk in context"
    }}
  ]
}}

For chunks:
- Find 2-3 places where the student used weak, unnatural, or basic phrasing
- Suggest a more advanced/natural replacement
- Only include chunks where the replacement is clearly better
- If the answer is already very good, return fewer chunks or empty array

Return ONLY valid JSON, no other text."""

    message = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        text = message.content[0].text.strip()
        # Remove markdown code blocks if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.error(f"Failed to parse AI response: {e}\nResponse: {message.content[0].text}")
        return {
            "feedback": {
                "good": "Good effort on this task!",
                "improve": "Keep practicing for more natural phrasing.",
                "tip": "Try to vary your vocabulary.",
                "encouragement": "Consistency is key — keep going! 💪"
            },
            "chunks": []
        }

def generate_flashcard_question(chunk: str, example: str) -> dict:
    """Generate 3 multiple choice options for a flashcard."""
    prompt = f"""Create a multiple choice question to test understanding of this English chunk.

Chunk: "{chunk}"
Example usage: "{example}"

Generate 3 options (A, B, C) where:
- One is CORRECT (natural, proper usage of the chunk)
- Two are WRONG (plausible but incorrect usage)

Return ONLY this JSON:
{{
  "question": "Choose the sentence that uses '{chunk}' correctly:",
  "options": {{
    "A": "sentence using the chunk",
    "B": "sentence with wrong usage",
    "C": "sentence with wrong usage"
  }},
  "correct": "A",
  "explanation": "Brief explanation of why A is correct and others are wrong (1-2 sentences)"
}}

Randomize which letter is correct. Return ONLY valid JSON."""

    message = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.error(f"Failed to parse flashcard question: {e}")
        return None

# ─── TASK DELIVERY ───────────────────────────────────────────

async def send_task(chat_id: int, track: str, skill: str, context: ContextTypes.DEFAULT_TYPE, is_second=False):
    task = get_task_for_today(track, skill)
    if not task:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Today's task isn't ready yet. The admin will generate tasks soon!"
        )
        return

    supabase.table("users").update({"current_task_id": task["id"]})\
        .eq("telegram_id", chat_id).execute()

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

# ─── SCHEDULING ──────────────────────────────────────────────

def schedule_user_jobs(user: dict, app: Application):
    telegram_id = user["telegram_id"]
    notif_utc = user.get("notification_utc")
    if not notif_utc:
        return

    for job in app.job_queue.get_jobs_by_name(str(telegram_id)):
        job.schedule_removal()
    for job in app.job_queue.get_jobs_by_name(f"{telegram_id}_reminder"):
        job.schedule_removal()
    for job in app.job_queue.get_jobs_by_name(f"{telegram_id}_weekly"):
        job.schedule_removal()

    hour, minute = map(int, notif_utc.split(":"))

    app.job_queue.run_daily(
        send_daily_tasks,
        time=time(hour=hour, minute=minute, tzinfo=pytz.utc),
        chat_id=telegram_id, name=str(telegram_id)
    )
    app.job_queue.run_daily(
        send_reminder,
        time=time(hour=22, minute=0, tzinfo=pytz.utc),
        chat_id=telegram_id, name=f"{telegram_id}_reminder"
    )
    app.job_queue.run_daily(
        send_weekly_summary,
        time=time(hour=18, minute=0, tzinfo=pytz.utc),
        days=(6,), chat_id=telegram_id, name=f"{telegram_id}_weekly"
    )

async def restore_all_schedules(app: Application):
    users = get_all_users()
    logger.info(f"Restoring schedules for {len(users)} users...")
    for user in users:
        try:
            schedule_user_jobs(user, app)
        except Exception as e:
            logger.error(f"Schedule restore error for {user['telegram_id']}: {e}")
    logger.info("Schedules restored!")

# ─── SCHEDULED JOBS ──────────────────────────────────────────

async def send_daily_tasks(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    user = get_user(chat_id)
    if not user:
        return
    if is_free_period_over(user):
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏰ Your 7-day free trial has ended!\n\nUse /subscribe to continue. 👇"
        )
        return
    upsert_user(chat_id, {"tasks_completed_today": 0, "current_task_id": None})
    await context.bot.send_message(
        chat_id=chat_id,
        text="🌅 *Good morning! Your daily English practice is ready.*\n\n"
             "2 tasks today — writing + speaking. Takes about 10 minutes. Let's go! 💪",
        parse_mode="Markdown"
    )
    await send_task(chat_id, user["track"], "writing", context)

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    user = get_user(chat_id)
    if not user or is_free_period_over(user):
        return
    completed = user.get("tasks_completed_today", 0)
    if completed >= 2:
        return
    notif_hour = int(user.get("notification_utc", "09:00").split(":")[0])
    if datetime.now(pytz.utc).hour - notif_hour < 2:
        return
    if completed == 0:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏰ *Don't forget your daily practice!*\n\n"
                 "You haven't done today's tasks yet — there's still time to keep your streak! 🔥\n\n"
                 "Use /task to start.",
            parse_mode="Markdown"
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔥 *Almost there!*\n\n"
                 "One more task to complete your streak today!\n\nUse /task to continue.",
            parse_mode="Markdown"
        )

async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    user = get_user(chat_id)
    if not user:
        return
    week_ago = (datetime.now(pytz.utc) - timedelta(days=7)).isoformat()
    answers = supabase.table("answers").select("id")\
        .eq("telegram_id", chat_id).gte("created_at", week_ago).execute()
    chunks_learned = supabase.table("flashcards").select("id")\
        .eq("telegram_id", chat_id).gte("created_at", week_ago).execute()
    tasks_done = len(answers.data or [])
    chunks_count = len(chunks_learned.data or [])

    if tasks_done == 14:
        summary = "🏆 Perfect week! Every single task completed!"
    elif tasks_done >= 10:
        summary = f"🔥 Great week — {tasks_done}/14 tasks done!"
    elif tasks_done >= 6:
        summary = f"👍 Decent week — {tasks_done}/14 tasks. Push harder next week!"
    else:
        summary = f"📈 {tasks_done}/14 tasks this week. Every bit counts — keep going!"

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📊 *Weekly Summary*\n\n"
             f"{summary}\n\n"
             f"📚 New phrases learned: *{chunks_count}*\n"
             f"🔥 Current streak: *{user.get('streak', 0)} days*\n"
             f"⭐ Total XP: *{user.get('xp', 0)}*\n\n"
             f"New week starts tomorrow — let's make it count! 💪",
        parse_mode="Markdown"
    )

# ─── ONBOARDING ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user and user.get("track"):
        await update.message.reply_text(
            "👋 Welcome back!\n\nUse /task for today's tasks or /stats to see your progress."
        )
        return ConversationHandler.END

    keyboard = [[track] for track in TRACKS.keys()]
    await update.message.reply_text(
        "👋 *Welcome to DailyEnglish Bot!*\n\n"
        "Practice English just *10 minutes a day* and feel real progress every week.\n\n"
        "Every day you'll get *2 tasks* — writing + speaking.\n"
        "Answer them, get AI feedback, earn XP, and build your streak.\n\n"
        "You have *7 days free* to try it out.\n\n"
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
    time_buttons = [TIMES[i:i+2] for i in range(0, len(TIMES), 2)]
    await update.message.reply_text(
        "✅ Got it!\n\n"
        "━━━━━━━━━━━━━━━\n"
        "*Step 3 of 3:* What time should I send your daily tasks? ⏰\n\n"
        "_Pick a time when you'll have 10 free minutes and a clear head._",
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
    tz = pytz.timezone(timezone_str)
    hour, minute = map(int, chosen.split(":"))
    utc_time = datetime.now(tz).replace(hour=hour, minute=minute).astimezone(pytz.utc)

    upsert_user(telegram_id, {
        "telegram_id": telegram_id, "track": track, "timezone": timezone_str,
        "notification_time": chosen,
        "notification_utc": f"{utc_time.hour:02d}:{utc_time.minute:02d}",
        "xp": 0, "streak": 0, "last_task_date": None,
        "current_task_id": None, "tasks_completed_today": 0, "is_paid": False
    })

    schedule_user_jobs(get_user(telegram_id), context.application)

    track_name = "Business English" if track == "business" else "Conversational English"
    await update.message.reply_text(
        f"🎉 *You're all set!*\n\n"
        f"📚 Track: *{track_name}*\n"
        f"⏰ Daily tasks at: *{chosen}* your time\n"
        f"🆓 Free trial: *7 days*\n\n"
        f"Let's start right now! 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await send_task(telegram_id, track, "writing", context)
    return ConversationHandler.END

# ─── ANSWER HANDLER ──────────────────────────────────────────

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)

    if not user or not user.get("track"):
        await update.message.reply_text("👋 Send /start to set up your account.")
        return

    if is_free_period_over(user):
        await update.message.reply_text("⏰ Your free trial has ended. Use /subscribe to continue.")
        return

    task_id = user.get("current_task_id")
    is_transcribed = False

    # Voice message
    if update.message.voice:
        if not task_id:
            await update.message.reply_text("🎙 Use /task to get your speaking task first!")
            return
        task = get_task_by_id(task_id)
        if not task or task["skill"] != "speaking":
            await update.message.reply_text("🎙 Voice messages are only for speaking tasks. Finish writing first!")
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
            await update.message.reply_text("❌ Couldn't process voice. Please type your answer.")
            return
    else:
        user_answer = update.message.text

    if not task_id:
        completed = user.get("tasks_completed_today", 0)
        if completed >= 2:
            await update.message.reply_text("✅ Both tasks done today! Come back tomorrow or check /stats 📊")
        else:
            await update.message.reply_text("Use /task to get your daily tasks! 📚")
        return

    task = get_task_by_id(task_id)
    if not task:
        await update.message.reply_text("Something went wrong. Use /task to try again.")
        return

    if not is_transcribed:
        await update.message.reply_text("⏳ Reviewing your answer...")

    # Single AI call for feedback + chunks
    try:
        result = get_feedback_and_chunks(task["content"], user_answer, task["skill"], is_transcribed)
        fb = result.get("feedback", {})
        chunks = result.get("chunks", [])
    except Exception as e:
        logger.error(f"AI error: {e}")
        fb = {"good": "Good effort!", "improve": "Keep practicing.", "tip": "Stay consistent.", "encouragement": "You're doing great! 💪"}
        chunks = []

    new_streak, xp_gained, total_xp = update_streak_and_xp(telegram_id)

    supabase.table("answers").insert({
        "telegram_id": telegram_id, "task_id": task_id,
        "answer": user_answer, "feedback": json.dumps(fb),
        "created_at": datetime.now(pytz.utc).isoformat()
    }).execute()

    completed_today = (user.get("tasks_completed_today") or 0) + 1
    upsert_user(telegram_id, {"current_task_id": None, "tasks_completed_today": completed_today})

    # Feedback message
    feedback_text = (
        f"📝 *Feedback:*\n\n"
        f"✅ {fb.get('good', '')}\n\n"
        f"💡 {fb.get('improve', '')}\n\n"
    )
    if task["skill"] == "speaking":
        feedback_text += f"🗣 {fb.get('tip', '')}\n\n"
    feedback_text += f"💪 {fb.get('encouragement', '')}"

    await update.message.reply_text(feedback_text, parse_mode="Markdown")

    # XP + streak
    streak_emoji = "🔥" if new_streak > 1 else "✅"
    bonus = f" _(+{xp_gained - 10} streak bonus!)_" if xp_gained > 10 else ""
    await update.message.reply_text(
        f"{streak_emoji} *Task complete!*\n\n"
        f"⭐ +{xp_gained} XP{bonus}\n"
        f"📈 Total: {total_xp} XP\n"
        f"🔥 Streak: {new_streak} {'day' if new_streak == 1 else 'days'}",
        parse_mode="Markdown"
    )

    # Chunks message
    if chunks:
        save_chunks(telegram_id, chunks, task_id)
        chunks_text = ""
        for c in chunks:
            chunks_text += f"• ~~{c['original']}~~ → *{c['phrase']}*\n"
            chunks_text += f"  _{c['example']}_\n\n"
        await update.message.reply_text(
            f"📚 *Phrases to remember:*\n\n{chunks_text}"
            f"Use /flashcards to practice these later.",
            parse_mode="Markdown"
        )

    # Second task
    if completed_today == 1:
        await update.message.reply_text("Now let's do the speaking task! 🗣")
        await send_task(telegram_id, user["track"], "speaking", context, is_second=True)

# ─── FLASHCARDS ──────────────────────────────────────────────

async def flashcards_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)

    if not user or not user.get("track"):
        await update.message.reply_text("Please start with /start first!")
        return

    cards = get_flashcards_for_review(telegram_id)
    if not cards:
        await update.message.reply_text(
            "📚 No flashcards due for review yet!\n\n"
            "Complete some tasks first — phrases from your answers will appear here."
        )
        return

    # Store session in user_data
    context.user_data["flashcard_session"] = {
        "cards": cards,
        "current_index": 0,
        "correct": 0,
        "wrong": 0,
        "awaiting_retry": False
    }

    await update.message.reply_text(
        f"🃏 *Flashcard Session*\n\n"
        f"{len(cards)} phrases to review. Let's go!\n\n"
        f"_Choose the sentence that uses each phrase correctly._"
        , parse_mode="Markdown"
    )
    await send_flashcard(update.effective_chat.id, context)

async def send_flashcard(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = context.user_data.get("flashcard_session")
    if not session:
        return

    cards = session["cards"]
    idx = session["current_index"]

    if idx >= len(cards):
        # Session complete
        correct = session["correct"]
        total = len(cards)
        xp = correct * 5
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎉 *Session Complete!*\n\n"
                 f"✅ Correct: {correct}/{total}\n"
                 f"⭐ +{xp} XP earned\n\n"
                 f"{'Perfect score! 🏆' if correct == total else 'Keep practicing — you will get there! 💪'}",
            parse_mode="Markdown"
        )
        context.user_data.pop("flashcard_session", None)
        return

    card = cards[idx]

    # Generate question
    question_data = generate_flashcard_question(card["chunk"], card.get("example", ""))
    if not question_data:
        # Skip this card if generation failed
        session["current_index"] += 1
        await send_flashcard(chat_id, context)
        return

    # Store question data for answer checking
    session["current_question"] = question_data
    session["current_card_id"] = card["id"]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"A) {question_data['options']['A']}", callback_data="fc_A")],
        [InlineKeyboardButton(f"B) {question_data['options']['B']}", callback_data="fc_B")],
        [InlineKeyboardButton(f"C) {question_data['options']['C']}", callback_data="fc_C")],
    ])

    progress = f"{idx + 1}/{len(cards)}"
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🃏 *Card {progress}*\n\n"
             f"Phrase: *{card['chunk']}*\n\n"
             f"{question_data['question']}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def handle_flashcard_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("fc_"):
        return

    session = context.user_data.get("flashcard_session")
    if not session:
        await query.edit_message_text("Session expired. Use /flashcards to start again.")
        return

    chosen = query.data.replace("fc_", "")
    question_data = session.get("current_question")
    card_id = session.get("current_card_id")
    correct_answer = question_data["correct"]
    is_correct = chosen == correct_answer
    awaiting_retry = session.get("awaiting_retry", False)

    if is_correct:
        update_flashcard_after_review(card_id, correct=True)
        session["correct"] += 1
        session["current_index"] += 1
        session["awaiting_retry"] = False

        await query.edit_message_text(
            f"✅ *Correct!*\n\n"
            f"{question_data['explanation']}",
            parse_mode="Markdown"
        )
        await send_flashcard(query.message.chat_id, context)

    elif not awaiting_retry:
        # First wrong attempt — give hint and retry
        session["awaiting_retry"] = True
        wrong_option = question_data["options"][chosen]

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"A) {question_data['options']['A']}", callback_data="fc_A")],
            [InlineKeyboardButton(f"B) {question_data['options']['B']}", callback_data="fc_B")],
            [InlineKeyboardButton(f"C) {question_data['options']['C']}", callback_data="fc_C")],
        ])

        await query.edit_message_text(
            f"❌ *Not quite.*\n\n"
            f"_{wrong_option}_ — that's not the right usage.\n\n"
            f"Think about what *{session['cards'][session['current_index']]['chunk']}* means and try again 👇",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    else:
        # Second wrong attempt — show answer and move on
        update_flashcard_after_review(card_id, correct=False)
        session["wrong"] += 1
        session["current_index"] += 1
        session["awaiting_retry"] = False
        correct_option = question_data["options"][correct_answer]

        await query.edit_message_text(
            f"❌ *The correct answer is {correct_answer}.*\n\n"
            f"✍️ _{correct_option}_\n\n"
            f"{question_data['explanation']}\n\n"
            f"_This phrase will appear again tomorrow for review._",
            parse_mode="Markdown"
        )
        await send_flashcard(query.message.chat_id, context)

# ─── COMMANDS ────────────────────────────────────────────────

async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or not user.get("track"):
        await update.message.reply_text("Please start with /start first!")
        return
    if is_free_period_over(user):
        await update.message.reply_text("⏰ Free trial ended. Use /subscribe to continue.")
        return
    completed = user.get("tasks_completed_today", 0)
    if completed >= 2:
        await update.message.reply_text("✅ Both tasks done today! Check /stats or come back tomorrow.")
        return
    upsert_user(telegram_id, {"current_task_id": None})
    skill = "writing" if completed == 0 else "speaking"
    await send_task(telegram_id, user["track"], skill, context, is_second=(completed == 1))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please start with /start first!")
        return
    track_name = "Business English" if user.get("track") == "business" else "Conversational English"
    streak = user.get("streak", 0)
    xp = user.get("xp", 0)
    completed_today = user.get("tasks_completed_today", 0)

    if xp < 100: level, next_level = "🌱 Beginner", 100
    elif xp < 500: level, next_level = "📗 Elementary", 500
    elif xp < 1500: level, next_level = "📘 Intermediate", 1500
    else: level, next_level = "📙 Advanced", None

    today_status = "✅✅ Both done!" if completed_today >= 2 else "✅⬜ 1/2 done" if completed_today == 1 else "⬜⬜ Not started"
    cards_due = len(get_flashcards_for_review(update.effective_user.id))

    await update.message.reply_text(
        f"📊 *Your Progress*\n\n"
        f"🎯 Track: {track_name}\n"
        f"🔥 Streak: {streak} {'day' if streak == 1 else 'days'}\n"
        f"⭐ XP: {xp}\n"
        f"🏆 Level: {level}\n"
        f"📈 Progress: {xp}/{next_level if next_level else '∞'} XP\n\n"
        f"Today: {today_status}\n"
        f"🃏 Flashcards due: {cards_due}",
        parse_mode="Markdown"
    )

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 *Subscribe to DailyEnglish*\n\n"
        "Continue your streak with full access:\n\n"
        "• Daily writing + speaking tasks\n"
        "• AI feedback on every answer\n"
        "• Vocabulary flashcards with spaced repetition\n"
        "• Weekly progress summaries\n\n"
        "_Payment coming soon — stay tuned!_",
        parse_mode="Markdown"
    )

async def generate_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admins only.")
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
                    .eq("track", track).eq("skill", skill)\
                    .eq("scheduled_date", task_date.isoformat()).execute()
                if existing.data:
                    continue
                prompt = f"""Generate a {skill} task for an English learner (B1-B2 level).
Track: {track} English

{'For writing: realistic scenario, ask to write 3-5 sentences.' if skill == 'writing' else 'For speaking: dialogue scenario or question, respond in 3-5 sentences.'}

Format with a blank line between:

[One sentence: scenario/context]

[Clear instruction: what to write or say]

Under 60 words. No labels."""
                try:
                    msg = anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001", max_tokens=200,
                        messages=[{"role": "user", "content": prompt}]
                    )
                    supabase.table("tasks").insert({
                        "track": track, "skill": skill,
                        "scheduled_date": task_date.isoformat(),
                        "content": msg.content[0].text.strip()
                    }).execute()
                    generated += 1
                except Exception as e:
                    logger.error(f"Task gen error: {e}")
    await update.message.reply_text(f"✅ Generated {generated} tasks for the next 7 days!")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ─── MAIN ────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(restore_all_schedules).build()

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
    app.add_handler(CommandHandler("flashcards", flashcards_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("generate", generate_tasks))
    app.add_handler(CallbackQueryHandler(handle_flashcard_answer, pattern="^fc_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer))
    app.add_handler(MessageHandler(filters.VOICE, handle_answer))

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
