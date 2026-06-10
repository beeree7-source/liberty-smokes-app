create table if not exists public.daily_sales_ledger (
  sale_date date primary key,
  cash_sales numeric(10,2) not null default 0,
  credit_sales numeric(10,2) not null default 0,
  cash_taken numeric(10,2) not null default 0,
  cash_deposit numeric(10,2) not null default 0,
  notes text default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_daily_sales_ledger_sale_date
  on public.daily_sales_ledger(sale_date desc);

create table if not exists public.daily_sales_cash_deductions (
  id bigserial primary key,
  sale_date date not null,
  source_sale_date date,
  withdrawal_date date,
  amount numeric(10,2) not null default 0,
  note text default '',
  created_at timestamptz not null default now()
);

alter table public.daily_sales_cash_deductions
  add column if not exists source_sale_date date;

alter table public.daily_sales_cash_deductions
  add column if not exists withdrawal_date date;

update public.daily_sales_cash_deductions
set source_sale_date = sale_date
where source_sale_date is null;

update public.daily_sales_cash_deductions
set withdrawal_date = coalesce(withdrawal_date, (created_at at time zone 'utc')::date, sale_date)
where withdrawal_date is null;

alter table public.daily_sales_cash_deductions
  alter column source_sale_date set not null;

alter table public.daily_sales_cash_deductions
  alter column withdrawal_date set not null;

create index if not exists idx_daily_sales_cash_deductions_sale_date
  on public.daily_sales_cash_deductions(sale_date desc, created_at desc);

create index if not exists idx_daily_sales_cash_deductions_source_withdrawal
  on public.daily_sales_cash_deductions(source_sale_date desc, withdrawal_date desc, created_at desc);

create table if not exists public.daily_sales_ledger_audit (
  id bigserial primary key,
  sale_date date not null,
  action text not null,
  entity_type text not null,
  snapshot jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_daily_sales_ledger_audit_sale_date
  on public.daily_sales_ledger_audit(sale_date desc, created_at desc);

create or replace function public.set_daily_sales_ledger_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_daily_sales_ledger_updated_at on public.daily_sales_ledger;
create trigger trg_daily_sales_ledger_updated_at
before update on public.daily_sales_ledger
for each row
execute function public.set_daily_sales_ledger_updated_at();

alter table public.daily_sales_ledger enable row level security;
alter table public.daily_sales_cash_deductions enable row level security;
alter table public.daily_sales_ledger_audit enable row level security;

drop policy if exists "daily_sales_ledger_all_access" on public.daily_sales_ledger;
create policy "daily_sales_ledger_all_access"
on public.daily_sales_ledger
for all
to anon, authenticated
using (true)
with check (true);

drop policy if exists "daily_sales_cash_deductions_all_access" on public.daily_sales_cash_deductions;
create policy "daily_sales_cash_deductions_all_access"
on public.daily_sales_cash_deductions
for all
to anon, authenticated
using (true)
with check (true);

drop policy if exists "daily_sales_ledger_audit_all_access" on public.daily_sales_ledger_audit;
create policy "daily_sales_ledger_audit_all_access"
on public.daily_sales_ledger_audit
for all
to anon, authenticated
using (true)
with check (true);
