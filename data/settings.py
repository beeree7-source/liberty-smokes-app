import datetime
import json

import streamlit as st
from postgrest import SyncPostgrestClient


SETTINGS_CACHE_KEY = "_settings_cache_map_v1"
SETTINGS_CACHE_AT_KEY = "_settings_cache_at_v1"
SETTINGS_CACHE_TTL_SECONDS = 30.0


def _invalidate_settings_cache() -> None:
    st.session_state.pop(SETTINGS_CACHE_KEY, None)
    st.session_state.pop(SETTINGS_CACHE_AT_KEY, None)


def _load_settings_cache(pg: SyncPostgrestClient, force: bool = False) -> dict[str, str]:
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cached = st.session_state.get(SETTINGS_CACHE_KEY)
    cached_at = st.session_state.get(SETTINGS_CACHE_AT_KEY)
    if (
        not force
        and isinstance(cached, dict)
        and isinstance(cached_at, (int, float))
        and (now_ts - float(cached_at)) < SETTINGS_CACHE_TTL_SECONDS
    ):
        return cached

    try:
        rows = pg.from_("settings").select("key, value").execute().data or []
    except Exception as exc:
        if "getaddrinfo failed" in str(exc).lower():
            st.error(
                "Failed to connect to Supabase (DNS resolution error). "
                "Verify SUPABASE_URL in .streamlit/secrets.toml and your network DNS settings."
            )
            st.stop()
        raise

    settings_map: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        settings_map[key] = str(row.get("value") or "")

    st.session_state[SETTINGS_CACHE_KEY] = settings_map
    st.session_state[SETTINGS_CACHE_AT_KEY] = now_ts
    return settings_map


def get_setting(pg: SyncPostgrestClient, key: str) -> str:
    settings_map = _load_settings_cache(pg)
    return str(settings_map.get(key) or "")


def save_setting(pg: SyncPostgrestClient, key: str, value: str):
    settings_map = _load_settings_cache(pg)
    if key in settings_map:
        pg.from_("settings").update({"value": value}).eq("key", key).execute()
    else:
        pg.from_("settings").insert({"key": key, "value": value}).execute()
    _invalidate_settings_cache()


def clear_setting(pg: SyncPostgrestClient, key: str):
    pg.from_("settings").delete().eq("key", key).execute()
    _invalidate_settings_cache()


def _load_json_list_setting(pg: SyncPostgrestClient, key: str) -> list[dict]:
    raw = get_setting(pg, key)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _save_json_list_setting(pg: SyncPostgrestClient, key: str, rows: list[dict]):
    save_setting(pg, key, json.dumps(rows))


def _setting_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
