create table if not exists public.seats (
  seat_number int primary key,
  customer_name text,
  drinks_consumed int not null default 0,
  alcoholic_drinks int not null default 0,
  non_alcoholic_drinks int not null default 0,
  drink_breakdown jsonb not null default '{}'::jsonb,
  is_occupied boolean not null default false
);

alter table public.seats
  add column if not exists alcoholic_drinks int not null default 0;

alter table public.seats
  add column if not exists non_alcoholic_drinks int not null default 0;

alter table public.seats
  add column if not exists drink_breakdown jsonb not null default '{}'::jsonb;

update public.seats
set non_alcoholic_drinks = drinks_consumed
where drinks_consumed > 0
  and alcoholic_drinks = 0
  and non_alcoholic_drinks = 0;

-- Seed rows (1..26):
insert into public.seats (seat_number)
select gs
from generate_series(1, 26) as gs
on conflict (seat_number) do nothing;
