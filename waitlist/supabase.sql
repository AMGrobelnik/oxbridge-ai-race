-- Crophecy waitlist. Paste into Supabase -> SQL Editor -> Run.
-- The public (anon) key can INSERT rows and call waitlist_count(); it can never read the list.

create table if not exists public.waitlist (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  kind        text not null check (kind in ('human', 'agent')),
  email       text not null check (length(email) <= 254 and email ~* '^[^@\s]+@[^@\s]+\.[^@\s]{2,}$'),
  role        text check (length(role) <= 60),
  agent       text check (length(agent) <= 60),
  world_model text check (length(world_model) <= 400),
  webhook     text check (webhook is null or (length(webhook) <= 300 and webhook ~* '^https://\S+$')),
  wallet      text check (wallet is null or wallet ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$'),  -- Solana devnet address
  source      text check (length(source) <= 120),
  notified_at timestamptz,                                                             -- set when you send the launch email/webhook
  check (kind = 'human' or (agent is not null and world_model is not null))
);

-- one sign-up per email for people, one per (email, agent name) for agents; duplicates return HTTP 409
create unique index if not exists waitlist_unique
  on public.waitlist (lower(email), kind, coalesce(lower(agent), ''));

alter table public.waitlist enable row level security;

drop policy if exists "anyone can join" on public.waitlist;
create policy "anyone can join" on public.waitlist
  for insert to anon
  with check (notified_at is null);
-- No select/update/delete policies: nobody but you (dashboard or service-role key) can read or change rows.

-- Public counter for the page, without exposing any row.
create or replace function public.waitlist_count()
returns json
language sql
security definer
set search_path = public
as $$
  select json_build_object(
    'total',  count(*),
    'agents', count(*) filter (where kind = 'agent')
  ) from public.waitlist;
$$;
revoke all on function public.waitlist_count() from public;
grant execute on function public.waitlist_count() to anon;
