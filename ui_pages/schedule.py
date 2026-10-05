import datetime
from email.utils import parseaddr

import pandas as pd
import streamlit as st
from postgrest import SyncPostgrestClient


def load_schedule_employees(pg: SyncPostgrestClient) -> list[dict]:
    from app import SCHEDULE_EMPLOYEES_KEY, _load_json_list_setting

    return [
        {"name": str(r.get("name") or "").strip(), "email": str(r.get("email") or "").strip()}
        for r in _load_json_list_setting(pg, SCHEDULE_EMPLOYEES_KEY)
        if str(r.get("name") or "").strip() or str(r.get("email") or "").strip()
    ]


def schedule_week_start(ref: datetime.date | None = None) -> datetime.date:
    ref = ref or datetime.date.today()
    return ref - datetime.timedelta(days=(ref.weekday() + 1) % 7)


def schedule_day_date(week_start: datetime.date, day: str) -> datetime.date:
    names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    return week_start + datetime.timedelta(days=names.index(day))


def send_weekly_schedule_to_employees(
    pg: SyncPostgrestClient,
    smtp: dict,
    week_start: datetime.date | None = None,
) -> dict:
    from app import (
        SCHEDULE_STAFF_LAST_SENT_KEY,
        load_weekly_employee_shifts,
        save_setting,
        send_email,
    )

    employees = [e for e in load_schedule_employees(pg) if "@" in e["email"]]
    if not employees:
        raise ValueError("Add at least one employee with an email address first.")
    shifts = load_weekly_employee_shifts(pg)
    week_start = week_start or schedule_week_start()
    lines = [f"Here is the employee schedule for the week of {week_start.strftime('%B %d, %Y')}:", ""]
    current_day = ""
    for row in shifts:
        if row["Day"] != current_day:
            current_day = row["Day"]
            lines.append(f"{current_day}, {schedule_day_date(week_start, current_day).strftime('%b %d')}")
        lines.append(f"  {row['Shift']} ({row['Hours']}): {row['Employee'] or 'Unassigned'}")
    body = "\n".join(lines)
    sent = 0
    failed: list[str] = []
    for employee in employees:
        try:
            send_email(
                smtp["host"],
                int(smtp["port"]),
                smtp.get("username", ""),
                smtp.get("password", ""),
                employee["email"],
                "Liberty Smokes Weekly Employee Schedule",
                body,
                security=smtp.get("security", "SSL"),
                from_addr=smtp.get("from_addr", ""),
            )
            sent += 1
        except Exception:
            failed.append(employee["email"])
    if sent:
        save_setting(pg, SCHEDULE_STAFF_LAST_SENT_KEY, datetime.date.today().isoformat())
    return {"sent": sent, "failed": failed}


def page_schedule(pg: SyncPostgrestClient):
    from app import (
        SCHEDULE_EMPLOYEES_KEY,
        SCHEDULE_STAFF_AUTO_DAY_KEY,
        SCHEDULE_STAFF_AUTO_ENABLED_KEY,
        SCHEDULE_STAFF_LAST_SENT_KEY,
        _bool_setting,
        _load_json_list_setting,
        _save_json_list_setting,
        get_setting,
        load_smtp_settings,
        load_weekly_employee_shifts,
        save_setting,
        save_weekly_employee_shifts,
    )

    st.header("Schedule")
    st.caption("The recurring weekly shift schedule for your employees.")

    try:
        weekly_shifts = load_weekly_employee_shifts(pg)
        employees = load_schedule_employees(pg)
        smtp_cfg = load_smtp_settings(pg)
    except Exception as exc:
        st.error(f"Failed to load schedule data: {exc}")
        return

    smtp_ready = all(smtp_cfg.get(k) for k in ("host", "port", "from_addr", "password"))
    auto_enabled = _bool_setting(get_setting(pg, SCHEDULE_STAFF_AUTO_ENABLED_KEY), False)
    days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    auto_day = str(get_setting(pg, SCHEDULE_STAFF_AUTO_DAY_KEY) or "Sunday")
    if auto_day not in days:
        auto_day = "Sunday"
    last_sent = str(get_setting(pg, SCHEDULE_STAFF_LAST_SENT_KEY) or "").strip()

    if auto_enabled and smtp_ready and employees:
        today = datetime.date.today()
        if today.strftime("%A") == auto_day and last_sent != today.isoformat():
            try:
                stats = send_weekly_schedule_to_employees(pg, smtp_cfg, schedule_week_start())
                st.success(f"Weekly schedule auto-sent to {stats['sent']} employee(s).")
                last_sent = today.isoformat()
            except Exception as exc:
                st.warning(f"Auto-send failed: {exc}")

    m1, m2 = st.columns(2)
    m1.metric("Shifts assigned", sum(bool(str(r.get("Employee") or "").strip()) for r in weekly_shifts))
    m2.metric("Employees", len(employees))

    st.subheader("Weekly Employee Shifts")
    st.caption("The shifts repeat every week. Pick a week to see its dates, edit the hours or employee, then save.")
    picked = st.date_input("Week of", value=schedule_week_start(), key="schedule_week_of")
    week_start = schedule_week_start(picked)
    st.caption(f"Sunday {week_start.strftime('%b %d')} - Saturday {(week_start + datetime.timedelta(days=6)).strftime('%b %d, %Y')}")
    weekly_shifts = [
        {"Day": r["Day"], "Date": schedule_day_date(week_start, r["Day"]).strftime("%a %b %d"), **{k: v for k, v in r.items() if k != "Day"}}
        for r in weekly_shifts
    ]
    names = [e["name"] for e in employees if e["name"]]
    options = [""] + names + sorted(
        {str(r["Employee"]) for r in weekly_shifts if r["Employee"] and r["Employee"] not in names}
    )
    employee_column = (
        st.column_config.SelectboxColumn("Employee", options=options, help="Choose the employee for this shift.")
        if names
        else st.column_config.TextColumn("Employee", help="Add employees below to pick from a list.")
    )
    edited_shifts = st.data_editor(
        weekly_shifts,
        key="weekly_employee_shifts_editor",
        width="stretch",
        hide_index=True,
        num_rows="fixed",
        disabled=["Day", "Date", "Shift"],
        column_config={
            "Hours": st.column_config.TextColumn(
                "Hours",
                help="Edit the hours for this shift, e.g. 10:00 AM - 6:00 PM.",
            ),
            "Employee": employee_column,
        },
    )
    if st.button("Save Weekly Schedule", type="primary", key="save_weekly_employee_shifts"):
        try:
            save_weekly_employee_shifts(pg, edited_shifts)
            st.success("Weekly employee schedule saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Failed to save weekly employee schedule: {exc}")

    st.divider()
    st.subheader("Employees")
    emp_df = pd.DataFrame(employees, columns=["name", "email"])
    edited_emps = st.data_editor(
        emp_df,
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        key="schedule_employees_editor",
    )
    if st.button("Save Employees", key="schedule_employees_save"):
        cleaned: list[dict] = []
        for _, row in edited_emps.iterrows():
            name = str(row.get("name") or "").strip()
            addr = parseaddr(str(row.get("email") or "").strip())[1].strip()
            if name or addr:
                cleaned.append({"name": name, "email": addr})
        _save_json_list_setting(pg, SCHEDULE_EMPLOYEES_KEY, cleaned)
        st.success(f"Saved {len(cleaned)} employee(s).")
        st.rerun()

    st.divider()
    st.subheader("Email Schedule to Employees")
    st.caption("Sends the full weekly schedule to every employee with an email address.")
    a1, a2 = st.columns(2)
    auto_on = a1.checkbox("Auto-send weekly", value=auto_enabled, key="schedule_staff_auto_enabled")
    auto_day_pick = a2.selectbox(
        "Send on", days, index=days.index(auto_day), key="schedule_staff_auto_day"
    )
    b1, b2 = st.columns(2)
    if b1.button("Save Auto-Send Settings", key="schedule_staff_auto_save"):
        save_setting(pg, SCHEDULE_STAFF_AUTO_ENABLED_KEY, "1" if auto_on else "0")
        save_setting(pg, SCHEDULE_STAFF_AUTO_DAY_KEY, auto_day_pick)
        st.success("Auto-send settings saved.")
        st.rerun()
    if b2.button("Email Schedule Now", key="schedule_staff_send_now"):
        if not smtp_ready:
            st.warning("Configure SMTP in Settings before sending.")
        else:
            try:
                stats = send_weekly_schedule_to_employees(pg, smtp_cfg, week_start)
                st.success(f"Schedule sent to {stats['sent']} employee(s).")
                if stats["failed"]:
                    st.warning(f"Failed for: {', '.join(stats['failed'])}")
            except Exception as exc:
                st.error(f"Failed to send schedule: {exc}")
    st.caption(f"Last sent: {last_sent or 'never'}")
