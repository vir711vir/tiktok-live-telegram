
import os
import json
import asyncio
from datetime import datetime, timezone

import requests
from TikTokLive import TikTokLiveClient
from pywebpush import webpush, WebPushException


# =====================================
# 1. Environment variables
# =====================================

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

VAPID_PRIVATE_KEY = os.environ["VAPID_PRIVATE_KEY"].replace(
    "\\n", "\n"
).strip()

VAPID_SUBJECT = "mailto:edcsshop@gmail.com"


# =====================================
# 2. Supabase configuration
# =====================================

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def supabase_get(table, params):
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS,
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def supabase_write(table, data, upsert=False):
    headers = dict(HEADERS)

    headers["Prefer"] = (
        "resolution=merge-duplicates,return=minimal"
        if upsert
        else "return=minimal"
    )

    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        params={"on_conflict": "account_id"} if upsert else None,
        json=data,
        timeout=20,
    )
    response.raise_for_status()


# =====================================
# 3. Telegram notification
# =====================================

def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )

    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("Telegram message failed")


# =====================================
# 4. iPad / PWA Push Notification
# =====================================

def send_push(user_id, username, event_type):

    subscriptions = supabase_get(
        "push_subscriptions",
        {
            "select": "id,subscription",
            "user_id": f"eq.{user_id}",
        },
    )

    if not subscriptions:
        print(f"No push subscription found for user {user_id}.")
        return

    if event_type == "LIVE_STARTED":
        title = "🔴 TikTok LIVE Started"
        body = f"@{username} just started a LIVE!"
    else:
        title = "⚫ TikTok LIVE Ended"
        body = f"@{username} has ended their LIVE."

    payload = {
        "title": title,
        "body": body,
        "url": f"https://www.tiktok.com/@{username}/live",
        "tag": f"tiktok-{username}",
    }

    for item in subscriptions:
        try:
            webpush(
                subscription_info=item["subscription"],
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={
                    "sub": VAPID_SUBJECT,
                },
            )

            print(f"Push notification sent for @{username}.")

        except WebPushException as error:
            print(
                f"Push failed for @{username}: {error}"
            )

        except Exception as error:
            print(
                f"Unexpected push error for @{username}: "
                f"{type(error).__name__}: {error}"
            )


# =====================================
# 5. TikTok LIVE checker
# =====================================

async def check_live(username):

    client = TikTokLiveClient(
        unique_id=f"@{username}"
    )

    return bool(await client.is_live())


# =====================================
# 6. Main monitoring process
# =====================================

def main():

    accounts = supabase_get(
        "accounts",
        {
            "select": "id,username,is_active,user_id",
            "is_active": "eq.true",
        },
    )

    print(
        f"Found {len(accounts)} active accounts in Supabase."
    )

    for account in accounts:

        account_id = account["id"]
        username = account["username"]
        user_id = account["user_id"]

        try:
            live = asyncio.run(check_live(username))

        except Exception as error:
            print(
                f"Could not check @{username}: "
                f"{type(error).__name__}: {error}"
            )
            continue

        try:

            # Get previous LIVE status
            old_rows = supabase_get(
                "live_status",
                {
                    "select": "is_live,started_at",
                    "account_id": f"eq.{account_id}",
                    "limit": "1",
                },
            )

            old = old_rows[0] if old_rows else None

            was_live = old["is_live"] if old else False

            checked_at = now()

            # Preserve original LIVE start time
            if live:
                started_at = (
                    old.get("started_at")
                    if was_live and old
                    else checked_at
                )
            else:
                started_at = None

            # Update current LIVE status
            supabase_write(
                "live_status",
                {
                    "account_id": account_id,
                    "is_live": live,
                    "started_at": started_at,
                    "checked_at": checked_at,
                },
                upsert=True,
            )

            print(
                f"@{username}: "
                f"{'LIVE' if live else 'OFFLINE'}"
            )

            # LIVE started
            if live and not was_live:

                supabase_write(
                    "live_events",
                    {
                        "account_id": account_id,
                        "event_type": "LIVE_STARTED",
                        "occurred_at": checked_at,
                    },
                )

                send_telegram(
                    f"🔴 @{username} just started a LIVE!\n"
                    f"https://www.tiktok.com/@{username}/live"
                )

                send_push(
                    user_id,
                    username,
                    "LIVE_STARTED",
                )

                print(
                    f"Notifications sent: @{username} LIVE started."
                )

            # LIVE ended
            elif not live and was_live:

                supabase_write(
                    "live_events",
                    {
                        "account_id": account_id,
                        "event_type": "LIVE_ENDED",
                        "occurred_at": checked_at,
                    },
                )

                send_telegram(
                    f"⚫ @{username} has ended their LIVE."
                )

                send_push(
                    user_id,
                    username,
                    "LIVE_ENDED",
                )

                print(
                    f"Notifications sent: @{username} LIVE ended."
                )

        except Exception as error:

            print(
                f"Error processing @{username}: "
                f"{type(error).__name__}: {error}"
            )


# =====================================
# 7. Start
# =====================================

if __name__ == "__main__":
    main()
