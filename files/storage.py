"""Supabase Storage helpers used by the application and ordering workflows."""

import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

import requests
import streamlit as st


FILES_BUCKET = "liberty-files"
FILES_CATEGORIES = ["Licenses", "Invoices", "Other"]
FILES_SETUP_SQL = """insert into storage.buckets (id, name, public)
values ('liberty-files', 'liberty-files', false)
on conflict (id) do nothing;"""


def _storage_headers() -> dict:
    key = str(st.secrets.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not key:
        st.error(
            "Missing SUPABASE_SERVICE_ROLE_KEY in .streamlit/secrets.toml. "
            "This app requires a server-only Supabase service-role key."
        )
        st.stop()
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _storage_url(path: str) -> str:
    return f"{str(st.secrets.get('SUPABASE_URL')).strip().rstrip('/')}/storage/v1/{path}"


def _safe_storage_name(name: str) -> str:
    base = Path(name).name
    safe = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", base).strip(" .")
    return safe or "file"


def storage_list_files(category: str) -> list[dict]:
    resp = requests.post(
        _storage_url(f"object/list/{FILES_BUCKET}"),
        headers=_storage_headers(),
        json={"prefix": category, "limit": 1000, "sortBy": {"column": "created_at", "order": "desc"}},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
    return [item for item in resp.json() if item.get("id")]


def storage_upload_file(category: str, name: str, data: bytes, mime_type: str = "") -> None:
    path = f"{category}/{_safe_storage_name(name)}"
    resp = requests.post(
        _storage_url(f"object/{FILES_BUCKET}/{quote(path)}"),
        headers={
            **_storage_headers(),
            "Content-Type": mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
            "x-upsert": "true",
        },
        data=data,
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")


def storage_download_file(category: str, name: str) -> bytes:
    resp = requests.get(
        _storage_url(f"object/{FILES_BUCKET}/{quote(category + '/' + name)}"),
        headers=_storage_headers(),
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
    return resp.content


def storage_delete_file(category: str, name: str) -> None:
    resp = requests.delete(
        _storage_url(f"object/{FILES_BUCKET}"),
        headers=_storage_headers(),
        json={"prefixes": [f"{category}/{name}"]},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")


def storage_list_folders(prefix: str) -> list[str]:
    resp = requests.post(
        _storage_url(f"object/list/{FILES_BUCKET}"),
        headers=_storage_headers(),
        json={"prefix": prefix, "limit": 1000, "sortBy": {"column": "name", "order": "asc"}},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
    return [item["name"] for item in resp.json() if not item.get("id")]
