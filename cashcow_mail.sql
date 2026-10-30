-- CashCow Mail: subscribers, campaigns, sends and the scanned inbox.
-- Run once in the Supabase project the CashCow apps use for sign-in:
-- SQL Editor > New query > paste everything > Run. Safe to run again.
-- Only the CashCow server (secret key) can read or write these tables.

create extension if not exists pgcrypto;

create table if not exists public.mail_subscribers (
  id uuid primary key default gen_random_uuid(),
  email text not null unique check (email = lower(email) and char_length(email) between 3 and 254),
  name text check (char_length(name) <= 80),
  status text not null default 'pending'
    check (status in ('pending', 'subscribed', 'unsubscribed', 'bounced', 'complained')),
  tags text[] not null default '{}',
  source text check (char_length(source) <= 40),
  token text not null unique default encode(gen_random_bytes(18), 'hex'),
  consent_at timestamptz,
  consent_ip text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists mail_subscribers_status on public.mail_subscribers (status);

create table if not exists public.mail_campaigns (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 1 and 100),
  subject text not null default '' check (char_length(subject) <= 200),
  preheader text not null default '' check (char_length(preheader) <= 200),
  body text not null default '' check (char_length(body) <= 50000),
  audience text[] not null default '{}',            -- empty = everyone subscribed
  status text not null default 'draft'
    check (status in ('draft', 'scheduled', 'sending', 'sent', 'canceled')),
  scheduled_at timestamptz,
  scan jsonb,
  sent_count int not null default 0,
  failed_count int not null default 0,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.mail_sends (
  campaign_id uuid not null references public.mail_campaigns (id) on delete cascade,
  subscriber_id uuid not null references public.mail_subscribers (id) on delete cascade,
  status text not null default 'sending' check (status in ('sending', 'sent', 'failed', 'unknown')),
  message_id text,
  error text,
  sent_at timestamptz not null default now(),
  primary key (campaign_id, subscriber_id)
);

create table if not exists public.mail_inbox (
  id uuid primary key default gen_random_uuid(),
  received_at timestamptz not null default now(),
  from_addr text,
  from_name text,
  subject text,
  snippet text,
  verdict text not null default 'safe' check (verdict in ('safe', 'suspicious', 'dangerous')),
  category text not null default 'reply' check (category in ('reply', 'lead', 'unsubscribe', 'auto', 'spam')),
  flags jsonb not null default '[]',
  status text not null default 'new' check (status in ('new', 'done', 'safe')),
  forwarded boolean not null default false
);
create index if not exists mail_inbox_received on public.mail_inbox (received_at desc);

alter table public.mail_subscribers enable row level security;
alter table public.mail_campaigns enable row level security;
alter table public.mail_sends enable row level security;
alter table public.mail_inbox enable row level security;
revoke all on public.mail_subscribers, public.mail_campaigns, public.mail_sends, public.mail_inbox from anon, authenticated;
grant all on public.mail_subscribers, public.mail_campaigns, public.mail_sends, public.mail_inbox to service_role;

-- ---------- subscribers ----------

create or replace function public.mail_clean_tags(p text[]) returns text[]
language sql immutable as $$
  select coalesce(array_agg(distinct t order by t), '{}')
  from (select lower(left(trim(x), 30)) t from unnest(coalesce(p, '{}')) x) s
  where t ~ '^[a-z0-9-]{1,30}$'
$$;

-- Someone signs up on the website. New or previously unsubscribed people start as
-- 'pending' until they click the confirmation link. Bounced or complained addresses
-- are never mailed again.
create or replace function public.mail_subscribe(p_email text, p_name text, p_tags text[], p_source text, p_ip text)
returns json language plpgsql as $$
declare
  e text := lower(trim(p_email));
  r public.mail_subscribers%rowtype;
begin
  if e !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' or char_length(e) > 254 then raise exception 'bad_email'; end if;
  select * into r from public.mail_subscribers where email = e for update;
  if r.id is null then
    insert into public.mail_subscribers (email, name, tags, source, status)
    values (e, nullif(left(trim(coalesce(p_name, '')), 80), ''), public.mail_clean_tags(p_tags), left(p_source, 40), 'pending')
    returning * into r;
    return json_build_object('status', 'pending', 'token', r.token, 'send_confirm', true);
  end if;
  update public.mail_subscribers set
    tags = public.mail_clean_tags(tags || coalesce(p_tags, '{}')),
    name = coalesce(name, nullif(left(trim(coalesce(p_name, '')), 80), '')),
    status = case when status in ('unsubscribed', 'pending') then 'pending' else status end,
    updated_at = now()
  where id = r.id returning * into r;
  return json_build_object('status', r.status, 'token', r.token,
                           'send_confirm', r.status = 'pending');
end $$;

create or replace function public.mail_confirm(p_token text, p_ip text) returns json
language plpgsql as $$
declare r public.mail_subscribers%rowtype;
begin
  update public.mail_subscribers
     set status = 'subscribed', consent_at = now(), consent_ip = left(p_ip, 60), updated_at = now()
   where token = p_token and status in ('pending', 'subscribed', 'unsubscribed')
  returning * into r;
  if r.id is null then return json_build_object('ok', false); end if;
  return json_build_object('ok', true, 'email', r.email);
end $$;

create or replace function public.mail_unsubscribe(p_token text) returns json
language plpgsql as $$
declare r public.mail_subscribers%rowtype;
begin
  update public.mail_subscribers
     set status = case when status in ('bounced', 'complained') then status else 'unsubscribed' end, updated_at = now()
   where token = p_token
  returning * into r;
  if r.id is null then return json_build_object('ok', false); end if;
  return json_build_object('ok', true, 'email', r.email);
end $$;

-- From bounces, complaints, and "unsubscribe" replies. Never upgrades a status.
create or replace function public.mail_set_status_by_email(p_email text, p_status text) returns boolean
language plpgsql as $$
declare n int;
begin
  if p_status not in ('unsubscribed', 'bounced', 'complained') then raise exception 'bad_status'; end if;
  update public.mail_subscribers set status = p_status, updated_at = now()
   where email = lower(trim(p_email))
     and not (status = 'complained' or (status = 'bounced' and p_status = 'unsubscribed'));
  get diagnostics n = row_count;
  return n > 0;
end $$;

-- Admin adds people who already agreed to get email. Existing addresses keep their
-- status, so anyone who unsubscribed, bounced or complained is never re-added.
create or replace function public.mail_admin_add(p_rows jsonb, p_source text) returns json
language plpgsql as $$
declare
  x jsonb;
  e text;
  added int := 0; skipped int := 0; invalid int := 0;
begin
  if jsonb_typeof(p_rows) <> 'array' or jsonb_array_length(p_rows) > 5000 then raise exception 'bad_rows'; end if;
  for x in select * from jsonb_array_elements(p_rows) loop
    e := lower(trim(coalesce(x->>'email', '')));
    if e !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' or char_length(e) > 254 then invalid := invalid + 1; continue; end if;
    insert into public.mail_subscribers (email, name, tags, source, status, consent_at)
    values (e, nullif(left(trim(coalesce(x->>'name', '')), 80), ''),
            public.mail_clean_tags(array(select jsonb_array_elements_text(coalesce(x->'tags', '[]')))),
            left(p_source, 40), 'subscribed', now())
    on conflict (email) do nothing;
    if found then added := added + 1; else skipped := skipped + 1; end if;
  end loop;
  return json_build_object('added', added, 'skipped', skipped, 'invalid', invalid);
end $$;

create or replace function public.mail_list_subscribers(p_q text, p_status text, p_limit int, p_offset int) returns json
language sql stable as $$
  select json_build_object(
    'total', (select count(*) from public.mail_subscribers s
              where (coalesce(p_status, '') = '' or s.status = p_status)
                and (coalesce(p_q, '') = '' or s.email ilike '%' || p_q || '%' or s.name ilike '%' || p_q || '%')),
    'rows', coalesce((select json_agg(x) from (
      select id, email, name, status, tags, source, consent_at, created_at
      from public.mail_subscribers s
      where (coalesce(p_status, '') = '' or s.status = p_status)
        and (coalesce(p_q, '') = '' or s.email ilike '%' || p_q || '%' or s.name ilike '%' || p_q || '%')
      order by created_at desc
      limit least(greatest(p_limit, 1), 200) offset greatest(p_offset, 0)) x), '[]'::json))
$$;

create or replace function public.mail_update_subscriber(p_id uuid, p_status text, p_tags text[]) returns void
language plpgsql as $$
begin
  -- The admin can unsubscribe someone, but can't turn a person back on: they must
  -- sign up again themselves.
  if p_status is not null and p_status <> 'unsubscribed' then raise exception 'bad_status'; end if;
  update public.mail_subscribers set
    status = case when p_status is null or status in ('bounced', 'complained') then status else p_status end,
    tags = case when p_tags is null then tags else public.mail_clean_tags(p_tags) end,
    updated_at = now()
  where id = p_id;
  if not found then raise exception 'not_found'; end if;
end $$;

create or replace function public.mail_audience_count(p_audience text[]) returns int
language sql stable as $$
  select count(*)::int from public.mail_subscribers
  where status = 'subscribed' and (coalesce(cardinality(p_audience), 0) = 0 or tags && p_audience)
$$;

create or replace function public.mail_summary() returns json
language sql stable as $$
  select json_build_object(
    'subscribers', (select coalesce(json_object_agg(status, n), '{}'::json)
                    from (select status, count(*) n from public.mail_subscribers group by status) s),
    'tags', (select coalesce(json_object_agg(t, n), '{}'::json)
             from (select unnest(tags) t, count(*) n from public.mail_subscribers where status = 'subscribed' group by 1) s),
    'inbox_new', (select count(*) from public.mail_inbox where status = 'new'),
    'campaigns', (select coalesce(json_agg(x), '[]'::json) from (
       select id, name, status, sent_count, failed_count, scheduled_at, finished_at, updated_at
       from public.mail_campaigns order by updated_at desc limit 5) x))
$$;

-- ---------- campaigns ----------

create or replace function public.mail_save_campaign(p jsonb) returns json
language plpgsql as $$
declare
  cid uuid := nullif(p->>'id', '')::uuid;
  r public.mail_campaigns%rowtype;
begin
  if cid is null then
    insert into public.mail_campaigns (name, subject, preheader, body, audience)
    values (coalesce(nullif(left(trim(p->>'name'), 100), ''), 'Untitled campaign'),
            left(coalesce(p->>'subject', ''), 200), left(coalesce(p->>'preheader', ''), 200),
            left(coalesce(p->>'body', ''), 50000),
            public.mail_clean_tags(array(select jsonb_array_elements_text(coalesce(p->'audience', '[]')))))
    returning * into r;
  else
    update public.mail_campaigns set
      name = coalesce(nullif(left(trim(p->>'name'), 100), ''), name),
      subject = left(coalesce(p->>'subject', subject), 200),
      preheader = left(coalesce(p->>'preheader', preheader), 200),
      body = left(coalesce(p->>'body', body), 50000),
      audience = case when p ? 'audience'
                      then public.mail_clean_tags(array(select jsonb_array_elements_text(p->'audience')))
                      else audience end,
      status = 'draft', scheduled_at = null, scan = null,   -- any edit needs a fresh scan
      updated_at = now()
    where id = cid and status in ('draft', 'scheduled', 'canceled')
    returning * into r;
    if r.id is null then raise exception 'locked'; end if;
  end if;
  return row_to_json(r);
end $$;

create or replace function public.mail_get_campaign(p_id uuid) returns json
language sql stable as $$
  select json_build_object('campaign', row_to_json(c), 'audience_count', public.mail_audience_count(c.audience))
  from public.mail_campaigns c where c.id = p_id
$$;

create or replace function public.mail_list_campaigns() returns json
language sql stable as $$
  select coalesce(json_agg(x order by x.updated_at desc), '[]'::json) from (
    select id, name, subject, status, audience, scheduled_at, sent_count, failed_count, finished_at, updated_at,
           (scan->>'grade') as grade
    from public.mail_campaigns) x
$$;

create or replace function public.mail_delete_campaign(p_id uuid) returns void
language plpgsql as $$
begin
  delete from public.mail_campaigns where id = p_id and status in ('draft', 'canceled');
  if not found then raise exception 'locked'; end if;
end $$;

create or replace function public.mail_set_scan(p_id uuid, p_scan jsonb) returns void
language plpgsql as $$
begin
  update public.mail_campaigns set scan = p_scan, updated_at = now() where id = p_id and status = 'draft';
end $$;

-- Schedule (or send now when p_at is null or in the past). Needs a scan with no blockers.
create or replace function public.mail_schedule(p_id uuid, p_at timestamptz) returns json
language plpgsql as $$
declare r public.mail_campaigns%rowtype;
begin
  select * into r from public.mail_campaigns where id = p_id for update;
  if r.id is null then raise exception 'not_found'; end if;
  if r.status <> 'draft' then raise exception 'locked'; end if;
  if r.scan is null or coalesce(jsonb_array_length(r.scan->'blockers'), 0) > 0 then raise exception 'needs_scan'; end if;
  if public.mail_audience_count(r.audience) = 0 then raise exception 'no_audience'; end if;
  update public.mail_campaigns set status = 'scheduled', scheduled_at = coalesce(p_at, now()), updated_at = now()
   where id = p_id returning * into r;
  return row_to_json(r);
end $$;

create or replace function public.mail_cancel(p_id uuid) returns void
language plpgsql as $$
begin
  update public.mail_campaigns set status = case when status = 'scheduled' then 'draft' else 'canceled' end,
         scheduled_at = null, updated_at = now(),
         finished_at = case when status = 'sending' then now() else finished_at end
   where id = p_id and status in ('scheduled', 'sending');
  if not found then raise exception 'locked'; end if;
end $$;

-- The sender picks up campaigns that are due. Rows left 'sending' from a restart are
-- marked 'unknown' so nobody gets the same email twice.
create or replace function public.mail_claim_due() returns json
language plpgsql as $$
declare ids uuid[];
begin
  with due as (
    select id from public.mail_campaigns
    where status = 'scheduled' and scheduled_at <= now()
    for update skip locked)
  update public.mail_campaigns c set status = 'sending', started_at = coalesce(started_at, now()), updated_at = now()
  from due where c.id = due.id;
  select coalesce(array_agg(id), '{}') into ids from public.mail_campaigns where status = 'sending';
  update public.mail_sends set status = 'unknown'
   where status = 'sending' and campaign_id = any(ids) and sent_at < now() - interval '10 minutes';
  return to_json(ids);
end $$;

-- Reserve the next recipients. Inserting the send row first means a person is never
-- picked twice for the same campaign, even if two senders run at once.
create or replace function public.mail_next_batch(p_campaign uuid, p_limit int) returns json
language plpgsql as $$
declare
  aud text[];
  st text;
  rows json;
begin
  select audience, status into aud, st from public.mail_campaigns where id = p_campaign;
  if st is distinct from 'sending' then return '[]'::json; end if;
  with picked as (
    select s.id, s.email, s.name, s.token from public.mail_subscribers s
    where s.status = 'subscribed'
      and (coalesce(cardinality(aud), 0) = 0 or s.tags && aud)
      and not exists (select 1 from public.mail_sends m where m.campaign_id = p_campaign and m.subscriber_id = s.id)
    order by s.created_at
    limit least(greatest(p_limit, 1), 200)
    for update of s skip locked),
  ins as (
    insert into public.mail_sends (campaign_id, subscriber_id, status)
    select p_campaign, id, 'sending' from picked on conflict do nothing returning subscriber_id)
  select coalesce(json_agg(json_build_object('id', p.id, 'email', p.email, 'name', p.name, 'token', p.token)), '[]'::json)
    into rows from picked p join ins on ins.subscriber_id = p.id;
  return rows;
end $$;

create or replace function public.mail_mark_send(p_campaign uuid, p_sub uuid, p_ok boolean, p_message_id text, p_error text)
returns void language plpgsql as $$
begin
  update public.mail_sends set status = case when p_ok then 'sent' else 'failed' end,
         message_id = left(p_message_id, 200), error = left(p_error, 300), sent_at = now()
   where campaign_id = p_campaign and subscriber_id = p_sub;
  update public.mail_campaigns set
    sent_count = sent_count + case when p_ok then 1 else 0 end,
    failed_count = failed_count + case when p_ok then 0 else 1 end
   where id = p_campaign;
end $$;

create or replace function public.mail_finish(p_campaign uuid) returns void
language plpgsql as $$
begin
  update public.mail_campaigns set status = 'sent', finished_at = now(), updated_at = now()
   where id = p_campaign and status = 'sending';
end $$;

-- ---------- scanned inbox ----------

create or replace function public.mail_inbox_add(p jsonb) returns uuid
language plpgsql as $$
declare nid uuid;
begin
  insert into public.mail_inbox (from_addr, from_name, subject, snippet, verdict, category, flags, forwarded)
  values (left(p->>'from_addr', 254), left(p->>'from_name', 120), left(p->>'subject', 300), left(p->>'snippet', 1000),
          coalesce(p->>'verdict', 'safe'), coalesce(p->>'category', 'reply'), coalesce(p->'flags', '[]'),
          coalesce((p->>'forwarded')::boolean, false))
  returning id into nid;
  -- Keep the latest 2,000 messages.
  delete from public.mail_inbox where id in (
    select id from public.mail_inbox order by received_at desc offset 2000);
  return nid;
end $$;

create or replace function public.mail_inbox_list(p_limit int) returns json
language sql stable as $$
  select coalesce(json_agg(x), '[]'::json) from (
    select * from public.mail_inbox order by received_at desc limit least(greatest(p_limit, 1), 200)) x
$$;

create or replace function public.mail_inbox_update(p_id uuid, p_status text) returns void
language plpgsql as $$
begin
  if p_status not in ('new', 'done', 'safe') then raise exception 'bad_status'; end if;
  update public.mail_inbox set status = p_status where id = p_id;
end $$;

-- Only the server may call these.
do $$
declare f text;
begin
  for f in select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.proname like 'mail\_%' loop
    execute format('revoke all on function %s from public, anon, authenticated', f);
    execute format('grant execute on function %s to service_role', f);
  end loop;
end $$;

-- ================================================================ ScamShield Email Guard
-- People forward email to check@ (or set their mail to auto-forward). Each address must be
-- verified by its owner before Email Guard will act on mail from it.

create table if not exists public.guard_addresses (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  email text not null unique check (email = lower(email) and char_length(email) between 3 and 254),
  verified boolean not null default false,
  token text not null unique default encode(gen_random_bytes(18), 'hex'),
  alerts text not null default 'risky' check (alerts in ('risky', 'all')),
  created_at timestamptz not null default now(),
  verified_at timestamptz
);
create index if not exists guard_addresses_user on public.guard_addresses (user_id);

create table if not exists public.guard_checks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  checked_at timestamptz not null default now(),
  mode text not null check (mode in ('manual', 'auto')),
  verdict text not null check (verdict in ('safe', 'suspicious', 'dangerous')),
  from_addr text,
  subject text,
  flags jsonb not null default '[]',
  alerted boolean not null default false
);
create index if not exists guard_checks_user on public.guard_checks (user_id, checked_at desc);

alter table public.guard_addresses enable row level security;
alter table public.guard_checks enable row level security;
revoke all on public.guard_addresses, public.guard_checks from anon, authenticated;
grant all on public.guard_addresses, public.guard_checks to service_role;

create or replace function public.guard_list(p_user uuid) returns json
language sql stable as $$
  select json_build_object(
    'addresses', coalesce((select json_agg(json_build_object('id', id, 'email', email, 'verified', verified,
                    'alerts', alerts, 'created_at', created_at) order by created_at)
                  from public.guard_addresses where user_id = p_user), '[]'::json),
    'checks', coalesce((select json_agg(x) from (
                  select id, checked_at, mode, verdict, from_addr, subject, flags, alerted
                  from public.guard_checks where user_id = p_user order by checked_at desc limit 50) x), '[]'::json),
    'month', (select json_build_object('total', count(*), 'risky', count(*) filter (where verdict <> 'safe'))
              from public.guard_checks where user_id = p_user and checked_at > now() - interval '30 days'),
    'this_month', (select count(*) from public.guard_checks where user_id = p_user and checked_at >= date_trunc('month', now())))
$$;

-- Add an address (max 3 per person). Returns the token to email a verification link.
create or replace function public.guard_add(p_user uuid, p_email text) returns json
language plpgsql as $$
declare
  e text := lower(trim(p_email));
  r public.guard_addresses%rowtype;
begin
  if e !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' or char_length(e) > 254 then raise exception 'bad_email'; end if;
  perform pg_advisory_xact_lock(hashtext('guard_' || p_user::text));
  select * into r from public.guard_addresses where email = e;
  if r.id is not null and r.user_id <> p_user then
    if r.verified then raise exception 'address_taken'; end if;
    delete from public.guard_addresses where id = r.id;   -- unverified claim by someone else: replace it
    r := null;
  end if;
  if r.id is null then
    if (select count(*) from public.guard_addresses where user_id = p_user) >= 3 then raise exception 'too_many'; end if;
    insert into public.guard_addresses (user_id, email) values (p_user, e) returning * into r;
  else
    update public.guard_addresses set token = encode(gen_random_bytes(18), 'hex') where id = r.id and not verified returning * into r;
    if r.id is null then select * into r from public.guard_addresses where email = e; end if;
  end if;
  return json_build_object('id', r.id, 'email', r.email, 'verified', r.verified, 'token', case when r.verified then null else r.token end);
end $$;

create or replace function public.guard_verify(p_token text) returns json
language plpgsql as $$
declare r public.guard_addresses%rowtype;
begin
  update public.guard_addresses set verified = true, verified_at = now()
   where token = p_token returning * into r;
  if r.id is null then return json_build_object('ok', false); end if;
  return json_build_object('ok', true, 'email', r.email);
end $$;

create or replace function public.guard_remove(p_user uuid, p_id uuid) returns void
language plpgsql as $$
begin
  delete from public.guard_addresses where id = p_id and user_id = p_user;
  if not found then raise exception 'not_found'; end if;
end $$;

create or replace function public.guard_set_alerts(p_user uuid, p_id uuid, p_alerts text) returns void
language plpgsql as $$
begin
  if p_alerts not in ('risky', 'all') then raise exception 'bad_status'; end if;
  update public.guard_addresses set alerts = p_alerts where id = p_id and user_id = p_user;
  if not found then raise exception 'not_found'; end if;
end $$;

-- Who owns these addresses (first verified match wins), plus how many checks they used today.
create or replace function public.guard_owner(p_emails text[]) returns json
language sql stable as $$
  select json_build_object('user_id', a.user_id, 'email', a.email, 'alerts', a.alerts,
                           'today', (select count(*) from public.guard_checks c
                                     where c.user_id = a.user_id and c.checked_at > now() - interval '1 day'),
                           'month', (select count(*) from public.guard_checks c
                                     where c.user_id = a.user_id and c.checked_at >= date_trunc('month', now())))
  from public.guard_addresses a
  where a.verified and a.email = any(select lower(trim(x)) from unnest(p_emails) x)
  order by array_position(array(select lower(trim(x)) from unnest(p_emails) x), a.email)
  limit 1
$$;

create or replace function public.guard_log(p jsonb) returns void
language plpgsql as $$
begin
  insert into public.guard_checks (user_id, mode, verdict, from_addr, subject, flags, alerted)
  values ((p->>'user_id')::uuid, p->>'mode', p->>'verdict', left(p->>'from_addr', 254), left(p->>'subject', 300),
          coalesce(p->'flags', '[]'), coalesce((p->>'alerted')::boolean, false));
  -- Keep each person's latest 500 checks.
  delete from public.guard_checks where id in (
    select id from public.guard_checks where user_id = (p->>'user_id')::uuid order by checked_at desc offset 500);
end $$;

do $$
declare f text;
begin
  for f in select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.proname like 'guard\_%' loop
    execute format('revoke all on function %s from public, anon, authenticated', f);
    execute format('grant execute on function %s to service_role', f);
  end loop;
end $$;
