from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import tomllib

from postgrest import SyncPostgrestClient


DEFAULT_TABLES = [
    "seats",
    "members",
    "member_monthly_refills",
    "member_monthly_drinks",
    "daily_sales_ledger",
    "daily_sales_cash_deductions",
    "daily_sales_ledger_audit",
    "settings",
]


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


def _jsonable(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return value


def export_table(pg: SyncPostgrestClient, table: str, output_file: Path) -> int:
    rows = pg.from_(table).select("*").execute().data or []
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        with output_file.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["_empty"])
        return 0

    columns: list[str] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                columns.append(key)

    with output_file.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if not isinstance(row, dict):
                continue
            writer.writerow({k: _jsonable(row.get(k)) for k in columns})

    return len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Supabase tables to timestamped CSV backups (read-only)."
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        default=DEFAULT_TABLES,
        help="Space-separated list of table names to export.",
    )
    parser.add_argument(
        "--out-dir",
        default="backups",
        help="Base output directory. Default: backups",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on first table export error instead of continuing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = repo_root / args.out_dir / ts

    url, key = load_supabase_credentials(repo_root)
    pg = get_postgrest_client(url, key)

    print(f"Backup directory: {backup_dir}")
    print("Export is read-only. No data will be modified.")

    failures = 0
    for table in args.tables:
        table_name = str(table).strip()
        if not table_name:
            continue
        out_file = backup_dir / f"{table_name}.csv"
        try:
            count = export_table(pg, table_name, out_file)
            print(f"OK: {table_name} -> {out_file} ({count} rows)")
        except Exception as exc:
            failures += 1
            print(f"FAIL: {table_name} ({exc})")
            if args.strict:
                return 1

    if failures:
        print(f"Completed with {failures} table failure(s).")
        return 1

    print("Backup completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
