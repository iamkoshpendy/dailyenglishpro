-- Run this in Supabase SQL Editor

create table users (
  id bigserial primary key,
  telegram_id bigint unique not null,
  track text,
  notification_time text,
  xp integer default 0,
  streak integer default 0,
  last_task_date date,
  created_at timestamptz default now()
);

create table tasks (
  id bigserial primary key,
  track text not null,
  skill text not null,
  scheduled_date date not null,
  content text not null,
  created_at timestamptz default now()
);

create table answers (
  id bigserial primary key,
  telegram_id bigint references users(telegram_id),
  task_id bigint references tasks(id),
  answer text,
  feedback text,
  created_at timestamptz default now()
);
