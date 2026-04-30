import logging
import os
import json
from datetime import datetime, time
import pytz
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from supabase import create_client
import anthropic

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

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Conversation states
CHOOSE_TRACK, CHOOSE_TIME, DO_TASK = range(3)

TRACKS = {
    "💼 Business English": "business",
    "🗣 Conversational English": "conversational"
}

# ─── HELPERS ────────────────────────────────────────────────

def get_user(telegram_id: int):
    res = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    return res.data[0] if res.data else None

def upsert_user(telegram_id: int, data: dict):
    existing = get_user(telegram_id)
    if existing:
        supabase.table("users").update(data).eq("telegram_id", telegram_id).execute()
    else:
        supabase.table("users").insert({"telegram_id": telegram_id, **data}).execute()

def get_todays_task(track: str):
    today = datetime.now(pytz.utc).strftime("%Y-%m-%d")
    res = supabase.table("tasks").select("*").eq("track", track).eq("scheduled_date", today).execute()
    return res.data[0] if res.data else None

def get_ai_feedback(task_content: str, user_answer: str, skill: str) -> str:
    prompt = f"""You are a friendly and encouraging English teacher giving feedback to a student.

Task type: {skill}
Task: {task_content}
Student's answer: {user_answer}

Give concise feedback (3-5 sentences max):
1. Start with one thing they did well
2. Point out 1-2 specific improvements with examples
3. End with encouragement

Be warm, specific, and practical. Write in English."""

    message = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

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
        last_date = datetime.fromisoformat(last).date() if isinstance(last, str) else last
        diff = (today - last_date).days
        new_streak = user["streak"] + 1 if diff == 1 else (user["streak"] if diff == 0 else 1)
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

# ─── HANDLERS ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    if user and user.get("track"):
        await update.message.reply_text(
            f"👋 Welcome back! Use /task to get today's task or /stats to see your progress."
        )
        return ConversationHandler.END

    keyboard = [[track] for track in TRACKS.keys()]
    await update.message.reply_text(
        "👋 *Welcome to DailyEnglish Bot!*\n\n"
        "Practice English just 5–10 minutes a day and feel real progress every week.\n\n"
        "First, choose your learning track:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return CHOOSE_TRACK

async def choose_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chosen = update.message.text
    if chosen not in TRACKS:
        await update.message.reply_text("Please choose one of the options below.")
        return CHOOSE_TRACK

    context.user_data["track"] = TRACKS[chosen]

    await update.message.reply_text(
        f"Great choice! ✅\n\n"
        f"Now, what time should I send you your daily task?\n\n"
        f"Send me the time in *HH:MM* format (24h), for example: `09:00` or `20:30`\n\n"
        f"_All times are in UTC. Brazil (Brasília) = UTC-3, so 9am local = 12:00 UTC_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return CHOOSE_TIME

async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        hour, minute = map(int, text.split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except:
        await update.message.reply_text("❌ Invalid format. Please send time like `09:00`", parse_mode="Markdown")
        return CHOOSE_TIME

    telegram_id = update.effective_user.id
    track = context.user_data["track"]

    upsert_user(telegram_id, {
        "telegram_id": telegram_id,
        "track": track,
        "notification_time": text,
        "xp": 0,
        "streak": 0,
        "last_task_date": None
    })

    # Schedule daily notification
    context.job_queue.run_daily(
        send_daily_task,
        time=time(hour=hour, minute=minute, tzinfo=pytz.utc),
        chat_id=telegram_id,
        name=str(telegram_id)
    )

    await update.message.reply_text(
        f"✅ All set!\n\n"
        f"Track: *{'Business English' if track == 'business' else 'Conversational English'}*\n"
        f"Daily reminder: *{text} UTC*\n\n"
        f"I'll send you a task every day. Use /task anytime to get today's task right now!",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def send_daily_task(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    user = get_user(chat_id)
    if not user:
        return

    task = get_todays_task(user["track"])
    if not task:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Today's task is being prepared. Check back in a few minutes!"
        )
        return

    context.user_data[chat_id] = {"current_task": task}

    skill_emoji = "✍️" if task["skill"] == "writing" else "🗣"
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🌅 *Good morning! Time for your daily English practice.*\n\n"
             f"{skill_emoji} *{task['skill'].capitalize()} Task*\n\n"
             f"{task['content']}\n\n"
             f"_Reply with your answer (text or voice message)_",
        parse_mode="Markdown"
    )

async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)

    if not user or not user.get("track"):
        await update.message.reply_text("Please start first with /start")
        return

    task = get_todays_task(user["track"])
    if not task:
        await update.message.reply_text(
            "⏳ No task available for today yet. Tasks are generated every Sunday for the week ahead.\n\n"
            "Use /generate to create this week's tasks (admin only)."
        )
        return

    context.user_data["current_task"] = task
    skill_emoji = "✍️" if task["skill"] == "writing" else "🗣"

    await update.message.reply_text(
        f"{skill_emoji} *{task['skill'].capitalize()} Task*\n\n"
        f"{task['content']}\n\n"
        f"_Reply with your answer (text or voice message)_",
        parse_mode="Markdown"
    )

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    task = context.user_data.get("current_task")

    if not task:
        await update.message.reply_text(
            "Use /task to get today's task first!"
        )
        return

    # Handle voice message
    if update.message.voice:
        await update.message.reply_text("🎙 Voice messages coming soon! Please send a text answer for now.")
        return

    user_answer = update.message.text
    await update.message.reply_text("⏳ Analyzing your answer...")

    try:
        feedback = get_ai_feedback(task["content"], user_answer, task["skill"])
    except Exception as e:
        logger.error(f"AI feedback error: {e}")
        feedback = "Great effort! Keep practicing every day."

    new_streak, xp_gained, total_xp = update_streak_and_xp(telegram_id)

    # Save answer
    supabase.table("answers").insert({
        "telegram_id": telegram_id,
        "task_id": task["id"],
        "answer": user_answer,
        "feedback": feedback,
        "created_at": datetime.now(pytz.utc).isoformat()
    }).execute()

    streak_msg = f"🔥 {new_streak} day streak!" if new_streak > 1 else "✅ Task complete!"
    bonus_msg = f" (+{xp_gained - 10} bonus)" if xp_gained > 10 else ""

    await update.message.reply_text(
        f"📝 *Feedback:*\n\n{feedback}\n\n"
        f"───────────────\n"
        f"{streak_msg}\n"
        f"⭐ +{xp_gained} XP{bonus_msg} → Total: {total_xp} XP",
        parse_mode="Markdown"
    )

    context.user_data.pop("current_task", None)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please start with /start first!")
        return

    track_name = "Business English" if user.get("track") == "business" else "Conversational English"
    streak = user.get("streak", 0)
    xp = user.get("xp", 0)

    streak_emoji = "🔥" if streak > 0 else "💤"
    level = "Beginner" if xp < 100 else "Intermediate" if xp < 500 else "Advanced" if xp < 1500 else "Expert"

    await update.message.reply_text(
        f"📊 *Your Progress*\n\n"
        f"🎯 Track: {track_name}\n"
        f"{streak_emoji} Current streak: {streak} days\n"
        f"⭐ Total XP: {xp}\n"
        f"🏆 Level: {level}\n\n"
        f"{'Keep going! 💪' if streak > 0 else 'Start your streak today with /task!'}",
        parse_mode="Markdown"
    )

async def generate_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to generate tasks for the week"""
    await update.message.reply_text("⏳ Generating tasks for this week...")

    tracks = ["business", "conversational"]
    skills = ["writing", "speaking", "writing", "speaking", "writing", "speaking", "writing"]

    from datetime import timedelta
    today = datetime.now(pytz.utc).date()

    generated = 0
    for track in tracks:
        for day_offset, skill in enumerate(skills):
            task_date = today + timedelta(days=day_offset)

            # Check if task already exists
            existing = supabase.table("tasks").select("id")\
                .eq("track", track).eq("scheduled_date", task_date.isoformat()).execute()
            if existing.data:
                continue

            prompt = f"""Generate a {skill} task for an English learner (B1-B2 level).
Track: {track} English
Skill: {skill}

For writing tasks: give a realistic professional/conversational scenario and ask them to write 2-4 sentences.
For speaking tasks: give a dialogue prompt or question to respond to in 2-4 sentences.

Format: Just the task text itself, no labels or headers. Be specific and practical.
Keep it under 100 words."""

            try:
                message = anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                )
                task_content = message.content[0].text

                supabase.table("tasks").insert({
                    "track": track,
                    "skill": skill,
                    "scheduled_date": task_date.isoformat(),
                    "content": task_content
                }).execute()
                generated += 1
            except Exception as e:
                logger.error(f"Task generation error: {e}")

    await update.message.reply_text(f"✅ Generated {generated} tasks for the week!")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ─── MAIN ───────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_TRACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_track)],
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
EOF