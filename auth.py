import hashlib
import hmac
import json
import os
import time

import streamlit as st
from postgrest import SyncPostgrestClient

from data.settings import get_setting, save_setting


APP_USERS_KEY = "app_users_v1"
AUTH_SESSION_KEY = "auth_user"
EXTRA_PERMISSION_PAGES = ["Settings"]
PASSWORD_HASH_ITERATIONS = 600_000

NAV_PAGE_SETTING_KEYS = {
    "Seats": "nav_show_seats_v1",
    "Members": "nav_show_members_v1",
    "Sales Ledger": "nav_show_sales_ledger_v1",
    "Schedule": "nav_show_schedule_v1",
    "Events": "nav_show_events_v1",
    "Ordering": "nav_show_ordering_v1",
    "Inbox": "nav_show_inbox_v1",
    "Files": "nav_show_files_v1",
    "POS": "nav_show_pos_v1",
    "Scanner": "nav_show_scanner_v1",
}


def _all_permission_pages() -> list[str]:
    return list(NAV_PAGE_SETTING_KEYS.keys()) + EXTRA_PERMISSION_PAGES


def _hash_password(password: str, salt: str, iterations: int = PASSWORD_HASH_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    ).hex()


def _validate_password(password: str) -> str:
    if len(password) < 12:
        return "Use at least 12 characters."
    if not any(char.islower() for char in password):
        return "Include a lowercase letter."
    if not any(char.isupper() for char in password):
        return "Include an uppercase letter."
    if not any(char.isdigit() for char in password):
        return "Include a number."
    return ""


def load_app_users(pg: SyncPostgrestClient) -> list[dict]:
    try:
        data = json.loads(get_setting(pg, APP_USERS_KEY) or "[]")
    except (TypeError, ValueError):
        return []
    return [u for u in data if isinstance(u, dict) and u.get("username")] if isinstance(data, list) else []


def save_app_users(pg: SyncPostgrestClient, users: list[dict]):
    save_setting(pg, APP_USERS_KEY, json.dumps(users))


def _make_user(username: str, name: str, password: str, role: str, pages: list[str]) -> dict:
    salt = os.urandom(16).hex()
    return {
        "username": username.strip().lower(),
        "name": name.strip() or username.strip(),
        "salt": salt,
        "hash": _hash_password(password, salt),
        "password_iterations": PASSWORD_HASH_ITERATIONS,
        "role": role,
        "pages": pages,
        "active": True,
    }


def _verify_user(users: list[dict], username: str, password: str) -> dict | None:
    user = next((u for u in users if u["username"] == username.strip().lower()), None)
    iterations = int(user.get("password_iterations") or 200_000) if user else 200_000
    candidate = (
        _hash_password(password, user["salt"], iterations)
        if user
        else _hash_password(password, "00" * 16, iterations)
    )
    if user and user.get("active", True) and hmac.compare_digest(candidate, str(user.get("hash") or "")):
        return user
    return None


def _require_login(pg: SyncPostgrestClient) -> dict | None:
    users = load_app_users(pg)
    current = st.session_state.get(AUTH_SESSION_KEY)
    if current:
        fresh = next((u for u in users if u["username"] == current["username"]), None)
        if fresh and fresh.get("active", True):
            st.session_state[AUTH_SESSION_KEY] = fresh
            return fresh
        st.session_state.pop(AUTH_SESSION_KEY, None)
    _, center, _ = st.columns([1, 2, 1])
    with center:
        if not users:
            st.subheader("Set up the admin account")
            st.caption("This is the first run. Create the owner login. You can add employee accounts afterwards.")
            setup_code = str(st.secrets.get("ADMIN_SETUP_CODE") or "")
            if not setup_code:
                st.error(
                    "First-run setup is locked. Set ADMIN_SETUP_CODE in Streamlit secrets, "
                    "then reload to create the first admin account."
                )
                return None
            with st.form("auth_setup"):
                name = st.text_input("Your name")
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")
                confirm = st.text_input("Confirm password", type="password")
                code = st.text_input("Setup code", type="password")
                if st.form_submit_button("Create admin account", type="primary"):
                    if setup_code and not hmac.compare_digest(code, setup_code):
                        st.error("Wrong setup code.")
                    elif not username.strip():
                        st.error("Enter a username.")
                    elif password_error := _validate_password(password):
                        st.error(password_error)
                    elif password != confirm:
                        st.error("Passwords do not match.")
                    else:
                        admin = _make_user(username, name, password, "admin", _all_permission_pages())
                        save_app_users(pg, [admin])
                        st.session_state[AUTH_SESSION_KEY] = admin
                        st.rerun()
            return None

        st.subheader("Sign in")
        with st.form("auth_login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            fails = int(st.session_state.get("auth_fails", 0))
            if fails >= 5:
                st.error("Too many attempts. Reload the page and try again later.")
                time.sleep(2)
            else:
                user = _verify_user(users, username, password)
                if user:
                    st.session_state["auth_fails"] = 0
                    st.session_state[AUTH_SESSION_KEY] = user
                    st.rerun()
                st.session_state["auth_fails"] = fails + 1
                st.error("Incorrect username or password.")
    return None


def user_can_access(user: dict, page_name: str) -> bool:
    return user.get("role") == "admin" or page_name in (user.get("pages") or [])
