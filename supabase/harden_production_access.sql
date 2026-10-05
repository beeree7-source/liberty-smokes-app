-- Run once in the Supabase SQL Editor after adding SUPABASE_SERVICE_ROLE_KEY
-- and APP_ENCRYPTION_KEY to the Streamlit deployment secrets.
--
-- The Streamlit server uses the service-role key; browsers never receive it.
-- This removes the public REST access that would otherwise expose member data,
-- password hashes, SMTP credentials, and operational settings.

alter table public.members enable row level security;
alter table public.seats enable row level security;
alter table public.settings enable row level security;
alter table public.member_monthly_drinks enable row level security;
alter table public.member_monthly_refills enable row level security;
alter table public.daily_sales_ledger enable row level security;
alter table public.daily_sales_cash_deductions enable row level security;
alter table public.daily_sales_ledger_audit enable row level security;

drop policy if exists "members_all_access" on public.members;
drop policy if exists "settings_all_access" on public.settings;
drop policy if exists "member_monthly_drinks_all_access" on public.member_monthly_drinks;
drop policy if exists "member_monthly_refills_all_access" on public.member_monthly_refills;
drop policy if exists "daily_sales_ledger_all_access" on public.daily_sales_ledger;
drop policy if exists "daily_sales_cash_deductions_all_access" on public.daily_sales_cash_deductions;
drop policy if exists "daily_sales_ledger_audit_all_access" on public.daily_sales_ledger_audit;

revoke all on table public.members from anon, authenticated;
revoke all on table public.seats from anon, authenticated;
revoke all on table public.settings from anon, authenticated;
revoke all on table public.member_monthly_drinks from anon, authenticated;
revoke all on table public.member_monthly_refills from anon, authenticated;
revoke all on table public.daily_sales_ledger from anon, authenticated;
revoke all on table public.daily_sales_cash_deductions from anon, authenticated;
revoke all on table public.daily_sales_ledger_audit from anon, authenticated;

drop policy if exists "liberty files access" on storage.objects;

create or replace function public.complete_pos_sale(
  p_cart jsonb,
  p_sale jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_inventory jsonb;
  v_sales jsonb;
  v_line jsonb;
  v_item jsonb;
  v_updated_inventory jsonb;
  v_sku text;
  v_sale_id text;
  v_quantity integer;
  v_stock integer;
  v_found boolean;
begin
  if jsonb_typeof(p_cart) <> 'array' or jsonb_array_length(p_cart) = 0 then
    raise exception 'A non-empty cart is required.';
  end if;
  if jsonb_typeof(p_sale) <> 'object' then
    raise exception 'A sale record is required.';
  end if;

  v_sale_id := nullif(btrim(p_sale->>'id'), '');
  if v_sale_id is null then
    raise exception 'Sale id is required.';
  end if;

  insert into public.settings (key, value)
  values ('pos_sales_v1', '[]')
  on conflict (key) do nothing;

  -- Lock in a deterministic order, so concurrent checkouts serialize safely.
  perform 1
  from public.settings
  where key in ('pos_inventory_v1', 'pos_sales_v1')
  order by key
  for update;

  select value::jsonb into v_inventory
  from public.settings
  where key = 'pos_inventory_v1';
  select value::jsonb into v_sales
  from public.settings
  where key = 'pos_sales_v1';

  if v_inventory is null or jsonb_typeof(v_inventory) <> 'array' then
    raise exception 'POS inventory is not configured.';
  end if;
  if v_sales is null or jsonb_typeof(v_sales) <> 'array' then
    v_sales := '[]'::jsonb;
  end if;

  if exists (
    select 1
    from jsonb_array_elements(v_sales) as sale
    where sale->>'id' = v_sale_id
  ) then
    return jsonb_build_object('inventory', v_inventory, 'sales', v_sales);
  end if;

  for v_line in select value from jsonb_array_elements(p_cart) loop
    if jsonb_typeof(v_line) <> 'object' then
      raise exception 'Invalid cart line.';
    end if;
    v_sku := nullif(btrim(v_line->>'sku'), '');
    v_quantity := nullif(v_line->>'qty', '')::integer;
    if v_sku is null or v_quantity is null or v_quantity <= 0 then
      raise exception 'Each cart item must have a SKU and positive quantity.';
    end if;

    v_found := false;
    v_updated_inventory := '[]'::jsonb;
    for v_item in select value from jsonb_array_elements(v_inventory) loop
      if v_item->>'sku' = v_sku then
        v_found := true;
        v_stock := coalesce(nullif(v_item->>'stock', '')::integer, 0);
        if v_stock < v_quantity then
          raise exception 'Insufficient stock for SKU %.', v_sku;
        end if;
        v_item := jsonb_set(v_item, '{stock}', to_jsonb(v_stock - v_quantity), true);
      end if;
      v_updated_inventory := v_updated_inventory || jsonb_build_array(v_item);
    end loop;
    if not v_found then
      raise exception 'Inventory item not found for SKU %.', v_sku;
    end if;
    v_inventory := v_updated_inventory;
  end loop;

  v_sales := v_sales || jsonb_build_array(p_sale);
  select coalesce(jsonb_agg(value order by ordinality), '[]'::jsonb)
    into v_sales
  from jsonb_array_elements(v_sales) with ordinality
  where ordinality > greatest(jsonb_array_length(v_sales) - 500, 0);

  update public.settings set value = v_inventory::text where key = 'pos_inventory_v1';
  update public.settings set value = v_sales::text where key = 'pos_sales_v1';

  return jsonb_build_object('inventory', v_inventory, 'sales', v_sales);
end;
$$;

revoke all on function public.complete_pos_sale(jsonb, jsonb) from public, anon, authenticated;
