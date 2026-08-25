-- Wyckoff paper shadow ledger. Isolated from USER_LIVE portfolios / positions / orders / daily_nav.

create table if not exists public.shadow_account (
  account_id text primary key,
  cash numeric not null default 100000,
  equity numeric not null default 100000,
  market_value numeric not null default 0,
  initial_capital numeric not null default 100000,
  as_of date,
  updated_at timestamptz not null default now()
);

create table if not exists public.shadow_positions (
  account_id text not null references public.shadow_account (account_id),
  code text not null,
  name text not null default '',
  shares int not null default 0 check (shares >= 0),
  sellable_shares int not null default 0,
  avg_cost numeric not null default 0,
  buy_dt date,
  last_mark numeric,
  stop_loss numeric,
  opened_at timestamptz not null default now(),
  primary key (account_id, code)
);

create table if not exists public.shadow_events (
  event_key text primary key,
  account_id text not null references public.shadow_account (account_id),
  as_of date not null,
  code text not null,
  name text not null default '',
  event_type text not null,
  price numeric,
  qty int not null default 0,
  fees jsonb not null default '{}',
  reason text not null default '',
  payload jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create table if not exists public.shadow_nav_daily (
  account_id text not null references public.shadow_account (account_id),
  as_of date not null,
  cash numeric not null default 0,
  market_value numeric not null default 0,
  equity numeric not null default 0,
  pnl_day numeric not null default 0,
  pnl_total numeric not null default 0,
  primary key (account_id, as_of)
);

create table if not exists public.shadow_trade_plans (
  plan_key text primary key,
  account_id text not null references public.shadow_account (account_id),
  code text not null,
  name text not null default '',
  action text not null,
  status text not null default 'planned',
  signal_date date not null,
  entry_mode text not null default 'next_open',
  suggested_price numeric,
  stop_price numeric,
  shares_hint int not null default 0,
  reason text not null default '',
  trigger_date date,
  entry_date date,
  entry_price numeric,
  fill_reason text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.shadow_account enable row level security;
alter table public.shadow_positions enable row level security;
alter table public.shadow_events enable row level security;
alter table public.shadow_nav_daily enable row level security;
alter table public.shadow_trade_plans enable row level security;

insert into public.shadow_account (account_id, cash, equity, market_value, initial_capital)
values (
  'USER_SHADOW:e66942b7-be66-46fe-95ed-ebc7f3b47928',
  100000,
  100000,
  0,
  100000
)
on conflict (account_id) do nothing;
