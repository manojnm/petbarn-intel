"""Bridge Streamlit Community Cloud's secrets manager (`st.secrets`, backed
by `.streamlit/secrets.toml`) into `os.environ`, so `Settings`
(`config.py`) -- deliberately a plain `pydantic-settings` model with no
Streamlit dependency, since the CLI/agent/scraper use it too -- picks
Azure/OpenAI credentials up transparently on Cloud.

Call `sync_secrets_to_env()` as the very first line of every Streamlit
page, before anything that might call `get_settings()`. No-op wherever
`st.secrets` isn't configured (e.g. local dev, which already uses `.env`).
"""

from __future__ import annotations

import os


def sync_secrets_to_env() -> None:
    try:
        import streamlit as st

        secrets = dict(st.secrets)
    except Exception:  # noqa: BLE001 - no secrets.toml locally, or no secrets configured on Cloud
        return
    for key, value in secrets.items():
        if isinstance(value, dict):
            continue  # nested tables (e.g. [connections]) aren't settings we read
        os.environ.setdefault(key.upper(), str(value))
