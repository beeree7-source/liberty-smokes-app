create table if not exists public.members (
  id serial primary key,
  first_name text not null,
  last_name text not null,
  email text,
  phone text,
  gift_card_number text,
  tier text not null default 'Monthly',
  status text not null default 'Active',
  locker text default '—',
  join_date date not null,
  next_billing_date date not null,
  last_reminder text default 'None'
);

alter table public.members
  add column if not exists phone text;

alter table public.members
  add column if not exists gift_card_number text;

create table if not exists public.settings (
  key text primary key,
  value text not null
);

create table if not exists public.member_monthly_drinks (
  member_id int not null references public.members(id) on delete cascade,
  month_start date not null,
  alcoholic_drinks int not null default 0,
  non_alcoholic_drinks int not null default 0,
  total_drinks int generated always as (alcoholic_drinks + non_alcoholic_drinks) stored,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (member_id, month_start)
);

create index if not exists idx_member_monthly_drinks_month
  on public.member_monthly_drinks(month_start desc);

create or replace function public.set_member_monthly_drinks_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_member_monthly_drinks_updated_at on public.member_monthly_drinks;
create trigger trg_member_monthly_drinks_updated_at
before update on public.member_monthly_drinks
for each row
execute function public.set_member_monthly_drinks_updated_at();

create table if not exists public.member_monthly_refills (
  member_id int not null references public.members(id) on delete cascade,
  month_start date not null,
  amount numeric(10,2) not null default 25.00,
  refilled_at timestamptz not null default now(),
  notes text default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (member_id, month_start)
);

create index if not exists idx_member_monthly_refills_month
  on public.member_monthly_refills(month_start desc);

create or replace function public.set_member_monthly_refills_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_member_monthly_refills_updated_at on public.member_monthly_refills;
create trigger trg_member_monthly_refills_updated_at
before update on public.member_monthly_refills
for each row
execute function public.set_member_monthly_refills_updated_at();

alter table public.members enable row level security;
alter table public.settings enable row level security;
alter table public.member_monthly_drinks enable row level security;
alter table public.member_monthly_refills enable row level security;

drop policy if exists "members_all_access" on public.members;
create policy "members_all_access"
on public.members
for all
to anon, authenticated
using (true)
with check (true);

drop policy if exists "settings_all_access" on public.settings;
create policy "settings_all_access"
on public.settings
for all
to anon, authenticated
using (true)
with check (true);

drop policy if exists "member_monthly_drinks_all_access" on public.member_monthly_drinks;
create policy "member_monthly_drinks_all_access"
on public.member_monthly_drinks
for all
to anon, authenticated
using (true)
with check (true);

drop policy if exists "member_monthly_refills_all_access" on public.member_monthly_refills;
create policy "member_monthly_refills_all_access"
on public.member_monthly_refills
for all
to anon, authenticated
using (true)
with check (true);
