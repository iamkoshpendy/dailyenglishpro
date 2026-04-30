# DailyEnglish Bot — Setup Guide

## Step 1 — Supabase (database)

1. Go to supabase.com → your project
2. Click **SQL Editor** in the left menu
3. Paste the contents of `schema.sql` and click **Run**
4. Go to **Settings → API** and copy:
   - `Project URL` → this is your SUPABASE_URL
   - `anon public` key → this is your SUPABASE_KEY

## Step 2 — Anthropic API key

1. Go to console.anthropic.com
2. Click **API Keys → Create Key**
3. Copy the key → this is your ANTHROPIC_API_KEY

## Step 3 — Railway deployment

1. Go to railway.app
2. Click **New Project → Deploy from GitHub repo**
   - OR: **New Project → Empty project → Add service → GitHub repo**
3. Connect your GitHub and upload this folder as a repo
   - If you don't have GitHub: go to github.com → New repository → upload files
4. In Railway, go to your service → **Variables** tab
5. Add these environment variables:
   ```
   BOT_TOKEN = your telegram bot token
   SUPABASE_URL = your supabase project url
   SUPABASE_KEY = your supabase anon key
   ANTHROPIC_API_KEY = your anthropic api key
   ```
6. Go to **Settings** tab → set Start Command:
   ```
   python bot.py
   ```
7. Railway will deploy automatically

## Step 4 — Generate first tasks

1. Open your bot in Telegram
2. Send `/generate` — this creates tasks for the next 7 days
3. Send `/task` to test the first task

## Daily workflow

- Every Sunday, send `/generate` to create tasks for the next week
- Users get a daily push at their chosen time
- They answer → AI gives feedback → XP and streak update

## Commands

- `/start` — onboarding (choose track + notification time)
- `/task` — get today's task immediately
- `/stats` — see XP, streak, level
- `/generate` — generate tasks for the week (you run this manually every Sunday)
