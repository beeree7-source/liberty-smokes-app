-- Add gift card refill tracking to members table

-- Add column to track next gift card refill date
alter table public.members
  add column if not exists next_gift_card_refill_date date;

-- Add column to track the last date gift card was refilled
alter table public.members
  add column if not exists last_gift_card_refill_date date;

-- Create view to show members due for gift card refill
-- Annual members: beginning of month
-- Monthly members: after marked paid for next month
create or replace view public.gift_card_refill_due as
select 
  m.id,
  m.first_name,
  m.last_name,
  m.gift_card_number,
  m.tier,
  m.next_gift_card_refill_date,
  case 
    when m.next_gift_card_refill_date is not null then m.next_gift_card_refill_date
    when m.tier = 'Annual' then date_trunc('month', current_date)::date
    when m.tier = 'Monthly' then m.next_billing_date
    else null
  end as calculated_refill_date,
  case
    when m.next_gift_card_refill_date is null then 'Never refilled'
    when m.next_gift_card_refill_date <= current_date then 'Overdue'
    else 'On schedule'
  end as refill_status
from public.members m
where m.status = 'Active'
order by m.next_gift_card_refill_date asc nulls first;

-- Create function to calculate next gift card refill date
create or replace function public.calculate_next_gift_card_refill_date(
  tier text,
  next_billing_date date
)
returns date as $$
begin
  if tier = 'Annual' then
    -- For annual members, next refill is at the beginning of the month
    return date_trunc('month', current_date)::date;
  elsif tier = 'Monthly' then
    -- For monthly members, next refill is after they're marked paid for next month
    return next_billing_date;
  else
    return null;
  end if;
end;
$$ language plpgsql;

-- Create function to update gift card refill date when member is marked paid
create or replace function public.update_gift_card_refill_on_payment()
returns trigger as $$
begin
  if new.status = 'Active' and coalesce(old.status, '') != 'Active' then
    if new.tier = 'Annual' then
      new.next_gift_card_refill_date = date_trunc('month', current_date)::date;
    else
      new.next_gift_card_refill_date = current_date;
    end if;
  end if;
  return new;
end;
$$ language plpgsql;

-- Create trigger to update gift card refill when membership is paid
drop trigger if exists trg_update_gift_card_refill on public.members;
create trigger trg_update_gift_card_refill
before update on public.members
for each row
execute function public.update_gift_card_refill_on_payment();
