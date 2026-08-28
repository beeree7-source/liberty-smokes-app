from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


REQUIRED_FILES = [
    "app.py",
    "requirements.txt",
    ".streamlit/secrets.toml.example",
    "scheduler/run_email_reminders.py",
    "supabase/create_seats_table.sql",
    "supabase/create_members_table.sql",
    "supabase/create_member_monthly_refills_table.sql",
    "supabase/create_daily_sales_ledger_table.sql",
]

PYTHON_FILES_TO_PARSE = [
    "app.py",
    "scheduler/run_email_reminders.py",
    "batch_parse_price_sheets.py",
    "import_cleaned_outputs_to_ordering.py",
    "audit_cleaned_outputs.py",
    "launcher.py",
]

REQUIRED_REQUIREMENTS = [
    "streamlit",
    "postgrest",
    "requests",
    "pandas",
]


def fail(message: str) -> None:
    print(f"FAIL: {message}")


def ok(message: str) -> None:
    print(f"OK:   {message}")


def check_required_files() -> bool:
    all_good = True
    for rel in REQUIRED_FILES:
        path = ROOT / rel
        if path.exists():
            ok(f"Found {rel}")
        else:
            fail(f"Missing required file: {rel}")
            all_good = False
    return all_good


def check_python_syntax() -> bool:
    all_good = True
    for rel in PYTHON_FILES_TO_PARSE:
        path = ROOT / rel
        if not path.exists():
            fail(f"Python file missing for syntax check: {rel}")
            all_good = False
            continue
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(path))
            ok(f"Syntax valid: {rel}")
        except SyntaxError as exc:
            fail(f"Syntax error in {rel}: line {exc.lineno}, col {exc.offset}: {exc.msg}")
            all_good = False
        except UnicodeDecodeError:
            fail(f"Could not decode {rel} as UTF-8")
            all_good = False
    return all_good


def check_requirements() -> bool:
    req_path = ROOT / "requirements.txt"
    if not req_path.exists():
        fail("requirements.txt not found")
        return False

    raw = req_path.read_text(encoding="utf-8")
    normalized = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pkg = line.split("==")[0].split(">=")[0].split("<=")[0].strip().lower()
        normalized.append(pkg)

    all_good = True
    for package in REQUIRED_REQUIREMENTS:
        if package in normalized:
            ok(f"Requirement present: {package}")
        else:
            fail(f"Required package missing in requirements.txt: {package}")
            all_good = False
    return all_good


def main() -> int:
    print("Running pre-deploy smoke test...")
    print(f"Project root: {ROOT}")

    checks = [
        check_required_files(),
        check_python_syntax(),
        check_requirements(),
    ]

    if all(checks):
        print("\nPASS: Smoke test completed successfully.")
        return 0

    print("\nFAIL: Smoke test found one or more issues.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
