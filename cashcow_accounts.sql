-- CashCow accounts: remembers which Premium plans each account has bought,
-- so Premium works on every device the person signs in on.
-- Run once in the Supabase project the apps use for sign-in (the ScamShield project):
-- SQL Editor > New query > paste everything > Run. Safe to run again.
-- Only the CashCow server (secret key) can read or write this table.

create table if not exists public.cashcow_entitlements (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  app text not null,                      -- healthscan, mybillpilot, allaccess, ...
  plan text not null default '',          -- e.g. hcs_yearly
  stripe_session_id text not null unique, -- the Stripe checkout
  stripe_subscription_id text,            -- empty for one-time (lifetime) purchases
  mode text,                              -- subscription or payment
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists cashcow_entitlements_user on public.cashcow_entitlements (user_id, app);

alter table public.cashcow_entitlements enable row level security;
revoke all on public.cashcow_entitlements from anon, authenticated;
grant all on public.cashcow_entitlements to service_role;
