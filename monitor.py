
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

VAPID_PRIVATE_KEY = os.environ["VAPID_PRIVATE_KEY"]
VAPID_SUBJECT = os.environ.get(
    "VAPID_SUBJECT",
    "mailto:admin@example.com"
)


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
        if upsert else "return=minimal"
    )

    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        params={"on_conflict": "account_id"} if upsert else None,
        json=data,
        timeout=20,
    )
    response.raise_for_status()


def supabase_delete(table, params):
    response = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS,
        params=params,
        timeout=20,
    )
    response.raise_for_status()


# =====================================
# 3. Telegram notification
# =====================================

def telegram(message):
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
# 4. Web Push notification
# =====================================

def send_web_push(user_id, title, body, url, tag):

    subscriptions = supabase_get(
        "push_subscriptions",
        {
            "select": "id,subscription",
            "user_id": f"eq.{user_id}",
        },
    )

    if not subscriptions:
        print(f"No Push subscription for user {user_id}.")
        return

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url,
        "tag": tag,
    })

    for item in subscriptions:

        subscription_id = item["id"]
        subscription_info = item["subscription"]

        if isinstance(subscription_info, str):
            subscription_info = json.loads(subscription_info)

        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={
                    "sub": VAPID_SUBJECT,
                },
            )

            print(f"Web Push sent to user {user_id}.")

        except WebPushException as error:

            status_code = (
                error.response.status_code
                if error.response is not None
                else None
            )

            print(
                f"Web Push failed: "
                f"{type(error).__name__}: {error}"
            )

            # Remove expired subscriptions
            if status_code in (404, 410):
                try:
                    supabase_delete(
                        "push_subscriptions",
                        {"id": f"eq.{subscription_id}"},
                    )
                    print(
                        f"Removed expired subscription "
                        f"{subscription_id}."
                    )
                except Exception as delete_error:
                    print(
                        f"Could not remove expired subscription: "
                        f"{delete_error}"
                    )

        except Exception as error:
            print(
                f"Unexpected Push error: "
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

            live_url = (
                f"https://www.tiktok.com/@{username}/live"
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

                message = (
                    f"🔴 @{username} just started a LIVE!\n"
                    f"{live_url}"
                )

                try:
                    telegram(message)
                    print("Telegram LIVE started notification sent.")
                except Exception as error:
                    print(f"Telegram error: {error}")

                send_web_push(
                    user_id=user_id,
                    title=f"🔴 @{username} is LIVE!",
                    body=f"@{username} just started a LIVE.",
                    url=live_url,
                    tag=f"{username}-live",
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

                message = (
                    f"⚫ @{username} has ended their LIVE."
                )

                try:
                    telegram(message)
                    print("Telegram LIVE ended notification sent.")
                except Exception as error:
                    print(f"Telegram error: {error}")

                send_web_push(
                    user_id=user_id,
                    title=f"⚫ @{username} ended their LIVE",
                    body=f"@{username} is no longer LIVE.",
                    url=live_url,
                    tag=f"{username}-live",
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
