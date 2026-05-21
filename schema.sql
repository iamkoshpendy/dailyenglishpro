-- Run this in Supabase SQL Editor
-- If you already ran the first schema, run only the ALTER TABLE commands at the bottom

create table if not exists users (
  id bigserial primary key,
  telegram_id bigint unique not null,
  track text,
  timezone text,
  notification_time text,
  notification_utc text,
  xp integer default 0,
  streak integer default 0,
  last_task_date date,
  current_task_id bigint,
  tasks_completed_today integer default 0,
  created_at timestamptz default now()
);

create table if not exists tasks (
  id bigserial primary key,
  track text not null,
  skill text not null,
  scheduled_date date not null,
  content text not null,
  created_at timestamptz default now()
);

create table if not exists answers (
  id bigserial primary key,
  telegram_id bigint references users(telegram_id),
  task_id bigint references tasks(id),
  answer text,
  feedback text,
  created_at timestamptz default now()
);

-- If tables already exist, just add the new columns:
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone text;
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_utc text;
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS current_task_id bigint;
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS tasks_completed_today integer default 0;
