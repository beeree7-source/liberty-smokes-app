from postgrest import SyncPostgrestClient
import streamlit as st

from domain.date_utils import month_start_for
from pos.local import _parse_drink_breakdown, load_drink_catalog


def render_seat_card(
    pg: SyncPostgrestClient,
    seat: dict,
    member_drink_limit: int = 3,
    non_member_limit: int = 1,
    members: list = None,
    monthly_drinks_by_member: dict | None = None,
    drink_catalog: list[dict] | None = None,
):
    from app import (
        add_drink_type,
        add_named_drink,
        check_in,
        clear_seat,
        find_member_by_customer_name,
        increment_member_monthly_drinks,
        is_member,
    )

    seat_number = int(seat.get("seat_number", 0))
    customer_name = seat.get("customer_name") or ""
    drinks_consumed = int(seat.get("drinks_consumed") or 0)
    alcoholic_drinks = int(seat.get("alcoholic_drinks") or 0)
    non_alcoholic_drinks = int(seat.get("non_alcoholic_drinks") or 0)
    if (alcoholic_drinks + non_alcoholic_drinks) == 0 and drinks_consumed > 0:
        non_alcoholic_drinks = drinks_consumed
    drink_breakdown = _parse_drink_breakdown(seat.get("drink_breakdown"))

    total_drinks = alcoholic_drinks + non_alcoholic_drinks
    is_occupied = bool(seat.get("is_occupied"))

    with st.container(border=True):
        st.subheader(f"Seat {seat_number}")

        if not is_occupied:
            name_key = f"name_input_{seat_number}"
            if st.session_state.pop(f"clear_input_{seat_number}", False):
                st.session_state.pop(name_key, None)
                st.session_state.pop(f"seat_limit_{seat_number}", None)
            st.text_input("Customer name", key=name_key)
            can_check_in = bool(st.session_state.get(name_key, "").strip())
            if st.button("Check In", key=f"check_in_{seat_number}", disabled=not can_check_in):
                check_in(pg, seat_number, st.session_state[name_key])
                st.session_state[f"clear_input_{seat_number}"] = True
                # Pre-set limit for the incoming customer based on membership.
                incoming_name = st.session_state[name_key]
                st.session_state[f"seat_limit_{seat_number}"] = (
                    member_drink_limit
                    if is_member(incoming_name, members or [])
                    else non_member_limit
                )
                st.rerun()
        else:
            seat_member = find_member_by_customer_name(customer_name, members or [])
            seat_is_member = seat_member is not None
            default_limit = member_drink_limit if seat_is_member else non_member_limit
            limit_key = f"seat_limit_{seat_number}"
            if limit_key not in st.session_state:
                st.session_state[limit_key] = default_limit

            badge = "Member" if seat_is_member else "Non-member"
            st.write(f"Name: **{customer_name}** ({badge})")
            st.write(f"Alcoholic: **{alcoholic_drinks}**")
            st.write(f"Non-alcoholic: **{non_alcoholic_drinks}**")
            st.write(f"Total drinks: **{total_drinks}**")

            catalog = drink_catalog or []
            cost_by_name = {
                str(item.get("name")): float(item.get("cost") or 0.0)
                for item in catalog
                if str(item.get("name") or "").strip()
            }
            seat_total_cost = 0.0
            for drink_name, qty in drink_breakdown.items():
                seat_total_cost += float(cost_by_name.get(drink_name, 0.0)) * int(qty)

            if drink_breakdown:
                breakdown_rows = []
                for name, qty in sorted(drink_breakdown.items(), key=lambda x: x[1], reverse=True):
                    unit_cost = float(cost_by_name.get(name, 0.0))
                    breakdown_rows.append(
                        {
                            "Drink": name,
                            "Qty": int(qty),
                            "Unit Cost": f"${unit_cost:,.2f}" if unit_cost > 0 else "",
                            "Total Cost": f"${(unit_cost * int(qty)):,.2f}" if unit_cost > 0 else "",
                        }
                    )
                st.dataframe(breakdown_rows, width="stretch", hide_index=True)
                st.caption(f"Estimated seat drink cost: ${seat_total_cost:,.2f}")

            if seat_member is not None:
                stats = (monthly_drinks_by_member or {}).get(str(seat_member.get("id")), {})
                m_alc = int(stats.get("alcoholic_drinks") or 0)
                m_non_alc = int(stats.get("non_alcoholic_drinks") or 0)
                m_total = int(stats.get("total_drinks") or (m_alc + m_non_alc))
                st.caption(
                    f"Member this month: {m_alc} alcoholic, {m_non_alc} non-alcoholic, {m_total} total"
                )

            effective_limit = st.number_input(
                "Seat limit",
                min_value=0,
                max_value=20,
                step=1,
                key=limit_key,
                help=f"Default for {badge}: {default_limit}. Adjust here to override.",
            )

            plus_disabled = total_drinks >= effective_limit

            if catalog:
                option_labels = [
                    f"{item.get('name', '')} ({'Alcoholic' if item.get('category') == 'alcoholic' else 'Non-Alcoholic'}) - ${float(item.get('cost') or 0.0):,.2f}"
                    for item in catalog
                ]
                option_to_item = {
                    option_labels[idx]: catalog[idx] for idx in range(len(option_labels))
                }
                selected_drink_label = st.selectbox(
                    "Drink",
                    option_labels,
                    key=f"seat_drink_pick_{seat_number}",
                )
                if st.button("Add Selected Drink", key=f"add_selected_drink_{seat_number}", disabled=plus_disabled):
                    selected_drink = option_to_item[selected_drink_label]
                    selected_name = str(selected_drink.get("name") or "").strip()
                    selected_type = str(selected_drink.get("category") or "non_alcoholic").strip().lower()
                    add_named_drink(
                        pg,
                        seat_number,
                        alcoholic_drinks,
                        non_alcoholic_drinks,
                        "alcoholic" if selected_type == "alcoholic" else "non_alcoholic",
                        selected_name,
                        drink_breakdown,
                    )
                    if seat_member is not None:
                        increment_member_monthly_drinks(
                            pg,
                            seat_member.get("id"),
                            "alcoholic" if selected_type == "alcoholic" else "non_alcoholic",
                            1,
                        )
                    st.rerun()
            else:
                st.caption("No drinks configured yet.")
                c1, c2 = st.columns(2)
                if c1.button("+ Alcoholic", key=f"plus_alc_{seat_number}", disabled=plus_disabled):
                    add_drink_type(
                        pg,
                        seat_number,
                        alcoholic_drinks,
                        non_alcoholic_drinks,
                        "alcoholic",
                    )
                    if seat_member is not None:
                        increment_member_monthly_drinks(pg, seat_member.get("id"), "alcoholic", 1)
                    st.rerun()

                if c2.button("+ Non-Alcoholic", key=f"plus_nonalc_{seat_number}", disabled=plus_disabled):
                    add_drink_type(
                        pg,
                        seat_number,
                        alcoholic_drinks,
                        non_alcoholic_drinks,
                        "non_alcoholic",
                    )
                    if seat_member is not None:
                        increment_member_monthly_drinks(pg, seat_member.get("id"), "non_alcoholic", 1)
                    st.rerun()

            if plus_disabled:
                st.caption(f"Limit reached ({effective_limit} total).")
            if st.button("Clear", key=f"clear_{seat_number}"):
                clear_seat(pg, seat_number)
                st.rerun()


def page_seats(pg: SyncPostgrestClient):
    from app import (
        add_seat,
        fetch_member_monthly_drinks,
        fetch_members,
        fetch_seats,
    )

    st.header("Seat Check-In")
    member_drink_limit = st.session_state.get("drink_limit", 3)
    non_member_limit = st.session_state.get("non_member_limit", 1)

    try:
        seats = fetch_seats(pg)
    except Exception as exc:
        st.error(f"Failed to load seats: {exc}")
        return

    try:
        members = fetch_members(pg)
    except Exception:
        members = []

    try:
        drink_catalog = load_drink_catalog(pg)
    except Exception:
        drink_catalog = []

    current_month_map = {}
    month_rows = fetch_member_monthly_drinks(pg, month_start=month_start_for())
    for row in month_rows:
        current_month_map[str(row.get("member_id"))] = row

    next_seat = (max(int(s["seat_number"]) for s in seats) + 1) if seats else 1
    if st.button(f"+ Add Seat {next_seat}"):
        try:
            add_seat(pg, next_seat)
            st.rerun()
        except Exception as exc:
            st.error(f"Failed to add seat: {exc}")

    if not seats:
        st.info("No seat rows found in table 'seats'.")
        return

    columns = st.columns(4)
    for idx, seat in enumerate(seats):
        with columns[idx % 4]:
            render_seat_card(
                pg,
                seat,
                member_drink_limit,
                non_member_limit,
                members,
                current_month_map,
                drink_catalog,
            )
