from __future__ import annotations

import datetime
import hashlib
from email.utils import parseaddr

import pandas as pd
import streamlit as st
from postgrest import SyncPostgrestClient


def page_events(pg: SyncPostgrestClient):
    from app import (
        GLOBAL_PENDING_WIDGET_RESET_KEY,
        PUBLIC_MAILING_LIST_KEY,
        SCHEDULE_EMAIL_AUTO_ENABLED_KEY,
        SCHEDULE_EMAIL_AUTO_INTERVAL_MIN_KEY,
        SCHEDULE_EMAIL_AUTO_LAST_RESULT_KEY,
        SCHEDULE_EMAIL_AUTO_LAST_RUN_KEY,
        SCHEDULE_EMAIL_LIST_KEY,
        SCHEDULE_EMAIL_MEMBERS_KEY,
        SCHEDULE_EMAIL_TO_KEY,
        _bool_setting,
        _format_datetime_12h,
        _format_time_12h,
        _int_setting,
        _load_json_list_setting,
        _save_json_list_setting,
        _time_24h_to_parts,
        _time_parts_to_24h,
        get_setting,
        load_smtp_settings,
        load_store_events_schedule,
        queue_widget_reset,
        resolve_digest_recipients,
        save_setting,
        save_store_events_schedule,
        send_schedule_digest_email,
    )

    st.header("Events")
    st.caption("Track tastings, launches, and in-store events, and email them to members or your public list.")

    try:
        store_events = load_store_events_schedule(pg)
    except Exception as exc:
        st.error(f"Failed to load schedule data: {exc}")
        return

    today = datetime.date.today()
    upcoming_events = [
        event
        for event in store_events
        if str(event.get("event_date") or "") >= today.strftime("%Y-%m-%d")
    ]

    st.metric("Upcoming events", len(upcoming_events))

    st.divider()
    st.subheader("Schedule Email Digest")
    try:
        smtp_cfg = load_smtp_settings(pg)
    except Exception:
        smtp_cfg = {
            "host": "",
            "port": 0,
            "security": "SSL",
            "username": "",
            "password": "",
            "from_addr": "",
        }

    schedule_to = str(get_setting(pg, SCHEDULE_EMAIL_TO_KEY) or "").strip()
    auto_enabled = _bool_setting(get_setting(pg, SCHEDULE_EMAIL_AUTO_ENABLED_KEY), False)
    auto_interval = max(15, _int_setting(get_setting(pg, SCHEDULE_EMAIL_AUTO_INTERVAL_MIN_KEY), 1440))
    auto_last_run = str(get_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RUN_KEY) or "").strip()
    auto_last_result = str(get_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RESULT_KEY) or "").strip()

    smtp_ready = (
        bool(smtp_cfg.get("host"))
        and bool(smtp_cfg.get("port"))
        and bool(smtp_cfg.get("from_addr"))
        and bool(smtp_cfg.get("password"))
    )

    if not smtp_ready:
        st.caption("Configure SMTP in Settings before using schedule digest emails.")
    elif auto_enabled:
        st.caption(
            "Automatic schedule digests are disabled from page loads. "
            "Use Send Digest manually until a separate digest worker is configured."
        )

    s1, s2 = st.columns(2)
    digest_to_input = s1.text_input(
        "Extra recipient email(s)",
        value=schedule_to or str(smtp_cfg.get("from_addr") or smtp_cfg.get("username") or ""),
        key="schedule_digest_to",
        help="One or more addresses separated by commas.",
    )
    digest_auto_enabled = s2.checkbox(
        "Enable auto digest",
        value=auto_enabled,
        key="schedule_digest_auto_enabled",
    )
    r1, r2 = st.columns(2)
    digest_members = r1.checkbox(
        "Send to all active members",
        value=_bool_setting(get_setting(pg, SCHEDULE_EMAIL_MEMBERS_KEY), False),
        key="schedule_digest_members",
    )
    digest_list = r2.checkbox(
        "Send to public mailing list",
        value=_bool_setting(get_setting(pg, SCHEDULE_EMAIL_LIST_KEY), False),
        key="schedule_digest_list",
    )
    digest_interval = st.number_input(
        "Auto digest interval (minutes)",
        min_value=15,
        max_value=10080,
        value=int(auto_interval),
        step=15,
        key="schedule_digest_auto_interval",
        help="Use 1440 for daily digest delivery.",
    )

    with st.expander("Public Mailing List (non-members)", expanded=False):
        st.caption("People who aren't members but want to hear about public events.")
        list_rows = _load_json_list_setting(pg, PUBLIC_MAILING_LIST_KEY)
        list_df = pd.DataFrame(
            [{"name": str(r.get("name") or ""), "email": str(r.get("email") or "")} for r in list_rows],
            columns=["name", "email"],
        )
        edited_list = st.data_editor(
            list_df,
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            key="public_mailing_list_editor",
        )
        if st.button("Save Mailing List", key="public_mailing_list_save"):
            cleaned: list[dict] = []
            seen_emails: set[str] = set()
            for _, row in edited_list.iterrows():
                addr = parseaddr(str(row.get("email") or "").strip())[1].strip()
                if "@" not in addr or addr.lower() in seen_emails:
                    continue
                seen_emails.add(addr.lower())
                cleaned.append({"name": str(row.get("name") or "").strip(), "email": addr})
            _save_json_list_setting(pg, PUBLIC_MAILING_LIST_KEY, cleaned)
            st.success(f"Saved {len(cleaned)} address(es).")
            st.rerun()

    b1, b2 = st.columns(2)
    if b1.button("Save Digest Settings", key="schedule_digest_save"):
        try:
            save_setting(pg, SCHEDULE_EMAIL_TO_KEY, digest_to_input.strip())
            save_setting(pg, SCHEDULE_EMAIL_MEMBERS_KEY, "1" if digest_members else "0")
            save_setting(pg, SCHEDULE_EMAIL_LIST_KEY, "1" if digest_list else "0")
            save_setting(pg, SCHEDULE_EMAIL_AUTO_ENABLED_KEY, "1" if digest_auto_enabled else "0")
            save_setting(pg, SCHEDULE_EMAIL_AUTO_INTERVAL_MIN_KEY, str(int(digest_interval)))
            st.success("Schedule digest settings saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Failed to save digest settings: {exc}")

    if b2.button("Send Digest Now", key="schedule_digest_send_now"):
        recipients_now = resolve_digest_recipients(pg, digest_to_input, digest_members, digest_list)
        if not smtp_ready:
            st.warning("Configure SMTP in Settings before sending digest emails.")
        elif not recipients_now:
            st.warning("Add a recipient, or tick members / mailing list.")
        else:
            try:
                stats = send_schedule_digest_email(
                    smtp_cfg,
                    recipients_now,
                    store_events,
                    lookahead_days=31,
                )
                now_txt = datetime.datetime.now().isoformat(timespec="seconds")
                save_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RUN_KEY, now_txt)
                save_setting(
                    pg,
                    SCHEDULE_EMAIL_AUTO_LAST_RESULT_KEY,
                    (
                        f"sent={stats.get('sent', 0)}; recipient={stats.get('recipient', '')}; "
                        f"events={stats.get('events_count', 0)}"
                    ),
                )
                st.success(
                    f"Digest sent to {int(stats.get('sent', 0))} recipient(s). "
                    f"Included {int(stats.get('events_count', 0))} event(s)."
                )
                if stats.get("failed"):
                    st.warning(f"Failed for: {', '.join(stats['failed'][:10])}")
            except Exception as exc:
                st.error(f"Failed to send digest: {exc}")

    st.caption(f"Last digest run: {_format_datetime_12h(auto_last_run) or 'never'}")
    if auto_last_result:
        st.caption(f"Last digest result: {auto_last_result}")

    st.subheader("Store Events")
    st.caption("Track tastings, launches, and in-store events.")

    e1, e2, e3 = st.columns([2, 1, 1])
    event_title = e1.text_input("Event title", key="schedule_event_title")
    event_date = e2.date_input("Date", value=today, key="schedule_event_date")
    event_all_day = e3.checkbox("All day", value=True, key="schedule_event_all_day")
    if event_all_day:
        event_hour = 6
        event_minute = 0
        event_period = "PM"
        st.caption("All-day event: no start time required.")
    else:
        t1, t2, t3 = st.columns([1, 1, 1])
        event_hour = t1.selectbox("Hour", list(range(1, 13)), index=5, key="schedule_event_hour")
        event_minute = t2.selectbox(
            "Minute",
            list(range(0, 60)),
            index=0,
            format_func=lambda value: f"{value:02d}",
            key="schedule_event_minute",
        )
        event_period = t3.selectbox("AM/PM", ["AM", "PM"], index=1, key="schedule_event_period")
    event_location = st.text_input("Location (optional)", key="schedule_event_location")
    event_notes = st.text_area("Notes (optional)", height=100, key="schedule_event_notes")
    add_event_clicked = st.button("Add Event", type="primary", key="schedule_add_event_btn")

    if add_event_clicked:
        clean_title = event_title.strip()
        if not clean_title:
            st.warning("Event title is required.")
        else:
            event_date_text = event_date.strftime("%Y-%m-%d")
            start_time = "" if event_all_day else _time_parts_to_24h(event_hour, event_minute, event_period)
            new_event = {
                "id": hashlib.sha1(
                    f"{clean_title.lower()}|{event_date_text}|{start_time}|{datetime.datetime.now().isoformat()}".encode("utf-8")
                ).hexdigest()[:12],
                "title": clean_title,
                "event_date": event_date_text,
                "all_day": bool(event_all_day),
                "start_time": start_time,
                "location": event_location.strip(),
                "notes": event_notes.strip(),
            }
            store_events.append(new_event)
            save_store_events_schedule(pg, store_events)
            st.success("Event saved.")
            queue_widget_reset(
                {
                    "schedule_event_title": "",
                    "schedule_event_date": today,
                    "schedule_event_all_day": True,
                    "schedule_event_hour": 6,
                    "schedule_event_minute": 0,
                    "schedule_event_period": "PM",
                    "schedule_event_location": "",
                    "schedule_event_notes": "",
                },
                GLOBAL_PENDING_WIDGET_RESET_KEY,
            )
            st.rerun()

    show_past = st.checkbox("Show past events", value=False, key="schedule_show_past_events")
    visible_events = []
    for event in store_events:
        event_date_text = str(event.get("event_date") or "")
        if show_past or event_date_text >= today.strftime("%Y-%m-%d"):
            visible_events.append(event)

    if not visible_events:
        st.info("No events to show.")
    else:
        for event in visible_events:
            event_id = str(event.get("id") or "")
            date_text = str(event.get("event_date") or "")
            when_text = date_text
            if not bool(event.get("all_day")) and str(event.get("start_time") or "").strip():
                when_text = f"{date_text} at {_format_time_12h(str(event.get('start_time') or '').strip())}"

            c1, c2 = st.columns([4, 1])
            title = str(event.get("title") or "")
            location = str(event.get("location") or "").strip()
            if location:
                c1.markdown(f"**{title}**  \n{when_text} | {location}")
            else:
                c1.markdown(f"**{title}**  \n{when_text}")

            if c2.button("Delete", key=f"schedule_delete_event_{event_id}"):
                updated = [
                    row for row in store_events if str(row.get("id") or "") != event_id
                ]
                save_store_events_schedule(pg, updated)
                st.rerun()

            with st.expander("Edit event", expanded=False):
                try:
                    default_edit_date = datetime.datetime.strptime(date_text, "%Y-%m-%d").date()
                except Exception:
                    default_edit_date = today
                edit_all_day_default = bool(event.get("all_day"))
                default_hour, default_minute, default_period = _time_24h_to_parts(
                    str(event.get("start_time") or "18:00").strip() or "18:00",
                    default_hour=6,
                    default_minute=0,
                    default_period="PM",
                )

                ec1, ec2, ec3 = st.columns([2, 1, 1])
                edit_title = ec1.text_input(
                    "Title",
                    value=title,
                    key=f"schedule_edit_title_{event_id}",
                )
                edit_date = ec2.date_input(
                    "Date",
                    value=default_edit_date,
                    key=f"schedule_edit_date_{event_id}",
                )
                edit_all_day = ec3.checkbox(
                    "All day",
                    value=edit_all_day_default,
                    key=f"schedule_edit_all_day_{event_id}",
                )
                if edit_all_day:
                    edit_hour = default_hour
                    edit_minute = default_minute
                    edit_period = default_period
                    st.caption("All-day event: no start time required.")
                else:
                    et1, et2, et3 = st.columns([1, 1, 1])
                    edit_hour = et1.selectbox(
                        "Hour",
                        list(range(1, 13)),
                        index=max(0, min(11, default_hour - 1)),
                        key=f"schedule_edit_hour_{event_id}",
                    )
                    edit_minute = et2.selectbox(
                        "Minute",
                        list(range(0, 60)),
                        index=max(0, min(59, default_minute)),
                        format_func=lambda value: f"{value:02d}",
                        key=f"schedule_edit_minute_{event_id}",
                    )
                    edit_period = et3.selectbox(
                        "AM/PM",
                        ["AM", "PM"],
                        index=0 if default_period == "AM" else 1,
                        key=f"schedule_edit_period_{event_id}",
                    )
                edit_location = st.text_input(
                    "Location",
                    value=location,
                    key=f"schedule_edit_location_{event_id}",
                )
                edit_notes = st.text_area(
                    "Notes",
                    value=str(event.get("notes") or ""),
                    height=100,
                    key=f"schedule_edit_notes_{event_id}",
                )
                if st.button("Save Changes", key=f"schedule_save_event_{event_id}"):
                    clean_edit_title = edit_title.strip()
                    if not clean_edit_title:
                        st.warning("Event title is required.")
                    else:
                        updated = []
                        for row in store_events:
                            if str(row.get("id") or "") == event_id:
                                updated.append(
                                    {
                                        "id": event_id,
                                        "title": clean_edit_title,
                                        "event_date": edit_date.strftime("%Y-%m-%d"),
                                        "all_day": bool(edit_all_day),
                                        "start_time": "" if edit_all_day else _time_parts_to_24h(edit_hour, edit_minute, edit_period),
                                        "location": edit_location.strip(),
                                        "notes": edit_notes.strip(),
                                    }
                                )
                            else:
                                updated.append(row)
                        save_store_events_schedule(pg, updated)
                        st.success("Event updated.")
                        st.rerun()

            if str(event.get("notes") or "").strip():
                st.caption(str(event.get("notes") or "").strip())
            st.divider()
