import argparse
import datetime
import os
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
import smtplib
import tomllib

from postgrest import SyncPostgrestClient

EMAIL_TEMPLATE_DEFAULTS = {
    "welcome_subject": "Welcome to Liberty Smokes, {first_name}!",
    "welcome_body": (
        "Hi {first_name},\n\n"
        "Welcome to Liberty Smokes. You're enrolled in our {tier} membership plan.\n"
        "Your next payment due date is {next_billing_date}.\n\n"
        "We look forward to seeing you in the shop."
    ),
    "renewal_subject": "Liberty Smokes Renewal Reminder",
    "renewal_body": (
        "Hi {first_name}, your {tier} membership renews on {next_billing_date}. "
        "If payment is missed, you have a 7-day grace period before your membership is marked inactive."
    ),
    "past_due_subject": "Payment Past Due",
    "past_due_body": (
        "Hi {first_name}, your membership payment is now past due. "
        "You have a 7-day grace period before your membership is marked inactive."
    ),
}

EMAIL_REMINDERS_AUTO_ENABLED_KEY = "email_reminders_auto_enabled_v1"
EMAIL_REMINDERS_AUTO_INTERVAL_MIN_KEY = "email_reminders_auto_interval_min_v1"
EMAIL_REMINDERS_AUTO_LAST_RUN_KEY = "email_reminders_auto_last_run_v1"
EMAIL_REMINDERS_AUTO_LAST_RESULT_KEY = "email_reminders_auto_last_result_v1"


def _bool_setting(value: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_setting(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def load_supabase_credentials(repo_root: Path) -> tuple[str, str]:
    env_url = str(os.getenv("SUPABASE_URL") or "").strip()
    env_key = str(os.getenv("SUPABASE_KEY") or "").strip()
    if env_url and env_key:
        return env_url, env_key

    secrets_path = repo_root / ".streamlit" / "secrets.toml"
    if secrets_path.exists():
        with secrets_path.open("rb") as fh:
            data = tomllib.load(fh)
        file_url = str(data.get("SUPABASE_URL") or "").strip()
        file_key = str(data.get("SUPABASE_KEY") or "").strip()
        if file_url and file_key:
            return file_url, file_key

    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY not found in env or .streamlit/secrets.toml")


def get_postgrest_client(url: str, key: str) -> SyncPostgrestClient:
    return SyncPostgrestClient(
        f"{url.rstrip('/')}/rest/v1",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )


def get_setting(pg: SyncPostgrestClient, key: str) -> str:
    try:
        rows = pg.from_("settings").select("value").eq("key", key).execute().data
        if not rows:
            return ""
        return str(rows[0].get("value") or "")
    except Exception:
        return ""


def save_setting(pg: SyncPostgrestClient, key: str, value: str):
    existing = pg.from_("settings").select("key").eq("key", key).execute().data
    if existing:
        pg.from_("settings").update({"value": value}).eq("key", key).execute()
    else:
        pg.from_("settings").insert({"key": key, "value": value}).execute()


def fetch_members(pg: SyncPostgrestClient) -> list[dict]:
    try:
        members = (
            pg.from_("members")
            .select("id, first_name, last_name, email, phone, tier, status, locker, join_date, next_billing_date, last_reminder")
            .order("last_name")
            .order("first_name")
            .execute()
            .data
            or []
        )
    except Exception:
        return []
    return mark_overdue_members_inactive(pg, members)


def mark_overdue_members_inactive(pg: SyncPostgrestClient, members: list[dict]) -> list[dict]:
    today = datetime.date.today()
    normalized_members: list[dict] = []
    status_updates: list[tuple[int, str]] = []

    for member in members:
        normalized_member = dict(member)
        status = str(normalized_member.get("status") or "").strip()
        if status.lower() in {"active", "past due"}:
            try:
                due = datetime.datetime.strptime(
                    str(normalized_member.get("next_billing_date") or "").strip(),
                    "%Y-%m-%d",
                ).date()
            except Exception:
                due = None
            if due and due < today:
                overdue_days = (today - due).days
                next_status = "Inactive" if overdue_days > 7 else "Past Due"
                normalized_member["status"] = next_status
                member_id = normalized_member.get("id")
                if member_id not in {None, ""}:
                    status_updates.append((int(member_id), next_status))
        normalized_members.append(normalized_member)

    for member_id, next_status in status_updates:
        try:
            pg.from_("members").update({"status": next_status}).eq("id", member_id).execute()
        except Exception:
            pass

    return normalized_members


def member_template_context(member: dict) -> dict:
    return {
        "first_name": str(member.get("first_name") or "").strip(),
        "last_name": str(member.get("last_name") or "").strip(),
        "full_name": f"{str(member.get('first_name') or '').strip()} {str(member.get('last_name') or '').strip()}".strip(),
        "email": str(member.get("email") or "").strip(),
        "phone": str(member.get("phone") or "").strip(),
        "tier": str(member.get("tier") or "").strip(),
        "status": str(member.get("status") or "").strip(),
        "locker": str(member.get("locker") or "").strip(),
        "join_date": str(member.get("join_date") or "").strip(),
        "next_billing_date": str(member.get("next_billing_date") or "").strip(),
    }


class SafeTemplateDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def format_email_template(template: str, member: dict) -> str:
    try:
        return (template or "").format_map(SafeTemplateDict(member_template_context(member)))
    except Exception:
        return template or ""


def load_email_templates(pg: SyncPostgrestClient) -> dict:
    mapping = {
        "welcome_subject": "email_tpl_welcome_subject",
        "welcome_body": "email_tpl_welcome_body",
        "renewal_subject": "email_tpl_renewal_subject",
        "renewal_body": "email_tpl_renewal_body",
        "past_due_subject": "email_tpl_past_due_subject",
        "past_due_body": "email_tpl_past_due_body",
    }
    result = {}
    for key, db_key in mapping.items():
        saved = get_setting(pg, db_key)
        result[key] = saved if saved else EMAIL_TEMPLATE_DEFAULTS[key]
    return result


def load_smtp_settings(pg: SyncPostgrestClient) -> dict:
    host = get_setting(pg, "smtp_host") or "smtp.gmail.com"
    port_raw = get_setting(pg, "smtp_port") or "465"
    security = (get_setting(pg, "smtp_security") or "SSL").upper()
    username = get_setting(pg, "smtp_username") or get_setting(pg, "smtp_email")
    password = get_setting(pg, "smtp_password")
    from_addr = get_setting(pg, "smtp_from") or username
    try:
        port = int(port_raw)
    except Exception:
        port = 465
    return {
        "host": host,
        "port": port,
        "security": security,
        "username": username,
        "password": password,
        "from_addr": from_addr,
    }


def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    to_addr: str,
    subject: str,
    body: str,
    security: str = "SSL",
    from_addr: str = "",
):
    normalized_to = parseaddr(str(to_addr or "").strip())[1].strip()
    if not normalized_to or "@" not in normalized_to:
        raise ValueError("Recipient email is missing or invalid.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr or smtp_user
    msg["To"] = normalized_to
    msg.set_content(body + "\n\nBest,\nLiberty Smokes Management")

    security_mode = (security or "SSL").upper()
    if security_mode == "SSL":
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        if security_mode == "STARTTLS":
            server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def get_pending_reminders(members: list, templates: dict) -> list[dict]:
    today = datetime.date.today()
    pending = []
    for member in members:
        if member.get("status") == "Canceled":
            continue
        try:
            due = datetime.datetime.strptime(str(member.get("next_billing_date") or ""), "%Y-%m-%d").date()
        except Exception:
            continue
        diff = (due - today).days
        last_reminder = str(member.get("last_reminder") or "").strip()
        if 0 <= diff <= 7 and last_reminder != "7_pre":
            pending.append(
                {
                    "id": member.get("id"),
                    "email": member.get("email"),
                    "target": "7_pre",
                    "subject": format_email_template(templates["renewal_subject"], member),
                    "body": format_email_template(templates["renewal_body"], member),
                }
            )
        elif diff < 0 and last_reminder != "7_post":
            pending.append(
                {
                    "id": member.get("id"),
                    "email": member.get("email"),
                    "target": "7_post",
                    "subject": format_email_template(templates["past_due_subject"], member),
                    "body": format_email_template(templates["past_due_body"], member),
                }
            )
    return pending


def run_pending_member_reminders(pg: SyncPostgrestClient, smtp: dict, templates: dict, members: list[dict], dry_run: bool) -> dict:
    pending = get_pending_reminders(members, templates)
    sent_count = 0
    skipped_no_email = 0
    failures = []

    for row in pending:
        email = str(row.get("email") or "").strip()
        if not email:
            skipped_no_email += 1
            continue
        try:
            if not dry_run:
                send_email(
                    smtp["host"],
                    int(smtp["port"]),
                    smtp["username"],
                    smtp["password"],
                    email,
                    row.get("subject", ""),
                    row.get("body", ""),
                    security=smtp.get("security", "SSL"),
                    from_addr=smtp.get("from_addr", ""),
                )
                pg.from_("members").update({"last_reminder": row.get("target", "")}).eq(
                    "id", row.get("id")
                ).execute()
            sent_count += 1
        except Exception as exc:
            failures.append({"id": row.get("id"), "email": email, "error": str(exc)})

    return {
        "pending": len(pending),
        "sent": sent_count,
        "skipped_no_email": skipped_no_email,
        "failed": len(failures),
        "failures": failures,
    }


def should_run_now(pg: SyncPostgrestClient, ignore_interval: bool) -> tuple[bool, str]:
    if ignore_interval:
        return True, "interval bypassed"

    interval_minutes = max(5, _int_setting(get_setting(pg, EMAIL_REMINDERS_AUTO_INTERVAL_MIN_KEY), 60))
    last_run_raw = str(get_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RUN_KEY) or "").strip()
    if not last_run_raw:
        return True, "no previous run"
    try:
        last_run = datetime.datetime.fromisoformat(last_run_raw)
    except Exception:
        return True, "invalid previous run timestamp"

    now = datetime.datetime.now()
    elapsed = (now - last_run).total_seconds()
    if elapsed >= interval_minutes * 60:
        return True, "interval reached"
    wait_min = int(((interval_minutes * 60) - elapsed + 59) // 60)
    return False, f"throttled ({wait_min} min remaining)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Liberty Smokes automated reminder email cycle once.")
    parser.add_argument("--force", action="store_true", help="Run even if automation is disabled in app settings.")
    parser.add_argument("--ignore-interval", action="store_true", help="Run even if minimum interval has not elapsed.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate reminders without sending emails or updating reminders.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    try:
        supabase_url, supabase_key = load_supabase_credentials(repo_root)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    pg = get_postgrest_client(supabase_url, supabase_key)

    enabled = _bool_setting(get_setting(pg, EMAIL_REMINDERS_AUTO_ENABLED_KEY), False)
    if not enabled and not args.force:
        print("SKIP: email reminder automation is disabled in settings.")
        return 0

    can_run, reason = should_run_now(pg, ignore_interval=args.ignore_interval)
    if not can_run:
        print(f"SKIP: {reason}")
        return 0

    smtp = load_smtp_settings(pg)
    if not smtp.get("host") or not smtp.get("port") or not smtp.get("from_addr") or not smtp.get("password"):
        message = "smtp not fully configured"
        save_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RESULT_KEY, message)
        print(f"SKIP: {message}")
        return 1

    templates = load_email_templates(pg)
    members = fetch_members(pg)
    stats = run_pending_member_reminders(pg, smtp, templates, members, dry_run=args.dry_run)

    now_txt = datetime.datetime.now().isoformat(timespec="seconds")
    if not args.dry_run:
        save_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RUN_KEY, now_txt)

    summary = (
        f"sent={stats.get('sent', 0)}; pending={stats.get('pending', 0)}; "
        f"skipped_no_email={stats.get('skipped_no_email', 0)}; failed={stats.get('failed', 0)}"
    )
    if args.dry_run:
        summary = "dry_run; " + summary
    save_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RESULT_KEY, summary)

    print(f"OK: {summary}")
    return 0 if int(stats.get("failed", 0)) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
