
import asyncio
import json
import os
from pathlib import Path
from datetime import datetime, timezone

import requests
from TikTokLive import TikTokLiveClient

ROOT = Path(__file__).parent

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

def now():
    return datetime.now(timezone.utc).isoformat()

def supabase_get(table, params):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS,
        params=params,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def supabase_post(table, data, upsert=False):
    headers = HEADERS.copy()

    if upsert:
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        params={"on_conflict": "account_id"} if upsert else None,
        json=data,
        timeout=20,
    )
    r.raise_for_status()

def get_accounts():
    rows = supabase_get(
        "accounts",
        {
            "select": "id,username",
            "is_active": "eq.true",
            "order": "created_at.asc",
        },
    )

    if not rows:
        raise ValueError("No active accounts found in Supabase.")

    if len(rows) > 5:
        raise ValueError(
            "More than 5 active accounts found. "
            "Current monitor supports up to 5."
        )

    return rows

def telegram(message):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    r.raise_for_status()

    if not r.json().get("ok"):
        raise RuntimeError("Telegram rejected the message")

async def check(username):
    client = TikTokLiveClient(unique_id=f"@{username}")
    return bool(
        await asyncio.wait_for(client.is_live(), timeout=35)
    )

def save_live_status(account_id, live, started_at):
    supabase_post(
        "live_status",
        {
            "account_id": account_id,
            "is_live": live,
            "started_at": started_at,
            "checked_at": now(),
        },
        upsert=True,
    )

def save_event(account_id, event_type):
    supabase_post(
        "live_events",
        {
            "account_id": account_id,
            "event_type": event_type,
            "occurred_at": now(),
        },
    )

async def main():
    state_file = ROOT / "state.json"

    try:
        old = json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}

    new = dict(old)

    rows = get_accounts()
    print(f"Found {len(rows)} active accounts in Supabase.")

    for account in rows:
        account_id = account["id"]
        username = account["username"].strip().lstrip("@")

        try:
            live = await check(username)
        except Exception as e:
            print(
                f"Could not check @{username}: "
                f"{type(e).__name__}: {e}"
            )
            continue

        previous = supabase_get(
            "live_status",
            {
                "select": "is_live,started_at",
                "account_id": f"eq.{account_id}",
                "limit": "1",
            },
        )

        if previous:
            was_live = previous[0]["is_live"]
            started_at = previous[0]["started_at"]
        else:
            was_live = old.get(username)
            started_at = None

        print(
            f"@{username}: "
            f"{'LIVE' if live else 'OFFLINE'}"
        )

        if live:
            if was_live is not True:
                started_at = now()

                save_event(account_id, "started")

                telegram(
                    f"🔴 @{username} just started a LIVE!\n"
                    f"https://www.tiktok.com/@{username}/live"
                )

            save_live_status(account_id, True, started_at)

        else:
            if was_live is True:
                save_event(account_id, "ended")

                telegram(
                    f"⚫ @{username} has ended their LIVE."
                )

            save_live_status(account_id, False, None)

        new[username] = live

    state_file.write_text(
        json.dumps(new, indent=2) + "\n"
    )

if __name__ == "__main__":
    asyncio.run(main())
