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

alter table public.member_monthly_refills enable row level security;

drop policy if exists "member_monthly_refills_all_access" on public.member_monthly_refills;
create policy "member_monthly_refills_all_access"
on public.member_monthly_refills
for all
to anon, authenticated
using (true)
with check (true);
