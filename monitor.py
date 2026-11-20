import os
import json
import re
import asyncio
from datetime import datetime, timezone

import requests
from TikTokLive import TikTokLiveClient
from pywebpush import webpush, WebPushException

# ============================================================
# Environment
# ============================================================

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Optional legacy destination: keeps the old single-chat alerts
# for PWA-owned accounts. Multi-user subscriptions are independent.
LEGACY_TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
VAPID_PRIVATE_KEY = os.environ["VAPID_PRIVATE_KEY"].replace("\n", "\n").strip()
VAPID_SUBJECT = "mailto:edcsshop@gmail.com"

MAX_TELEGRAM_ACCOUNTS = 5
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

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
        timeout=25,
    )
    response.raise_for_status()
    return response.json()


def supabase_post(table, data, on_conflict=None):
    headers = dict(HEADERS)
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    params = {"on_conflict": on_conflict} if on_conflict else None
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        params=params,
        json=data,
        timeout=25,
    )
    response.raise_for_status()


def supabase_insert_ignore(table, data, on_conflict=None):
    headers = dict(HEADERS)
    headers["Prefer"] = "resolution=ignore-duplicates,return=minimal"
    params = {"on_conflict": on_conflict} if on_conflict else None
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        params=params,
        json=data,
        timeout=25,
    )
    response.raise_for_status()


def supabase_patch(table, params, data):
    headers = dict(HEADERS)
    headers["Prefer"] = "return=minimal"
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        params=params,
        json=data,
        timeout=25,
    )
    response.raise_for_status()


# ============================================================
# Telegram API and independent per-chat subscriptions
# ============================================================

def telegram_call(method, payload=None, timeout=25):
    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result}")
    return result.get("result")


def telegram_send(chat_id, message):
    telegram_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        },
    )


def clean_username(value):
    value = value.strip()
    value = re.sub(
        r"^https?://(www\.)?tiktok\.com/@",
        "",
        value,
        flags=re.I,
    )
    value = value.split("/")[0].split("?")[0].lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{2,24}", value):
        return None
    return value.lower()


def get_update_offset():
    rows = supabase_get(
        "telegram_bot_state",
        {"select": "last_update_id", "id": "eq.1", "limit": "1"},
    )
    return int(rows[0]["last_update_id"]) + 1 if rows else None


def save_update_offset(update_id):
    supabase_post(
        "telegram_bot_state",
        {"id": 1, "last_update_id": int(update_id), "updated_at": now()},
        on_conflict="id",
    )


def register_telegram_user(chat_id, user):
    supabase_post(
        "telegram_users",
        {
            "chat_id": int(chat_id),
            "telegram_user_id": int(user["id"]),
            "username": user.get("username"),
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "updated_at": now(),
        },
        on_conflict="chat_id",
    )


def get_user_subscriptions(chat_id):
    return supabase_get(
        "telegram_subscriptions",
        {
            "select": "username,created_at",
            "chat_id": f"eq.{chat_id}",
            "order": "username.asc",
        },
    )


def add_subscription(chat_id, username):
    existing = get_user_subscriptions(chat_id)
    if any(row["username"].lower() == username.lower() for row in existing):
        return "duplicate"
    if len(existing) >= MAX_TELEGRAM_ACCOUNTS:
        return "limit"

    # Keep one shared status row per unique TikTok username.
    supabase_insert_ignore(
        "telegram_watchlist",
        {"username": username, "is_live": None, "checked_at": None},
        on_conflict="username",
    )
    supabase_post(
        "telegram_subscriptions",
        {"chat_id": int(chat_id), "username": username, "created_at": now()},
        on_conflict="chat_id,username",
    )
    return "added"


def remove_subscription(chat_id, username):
    response = requests.delete(
        f"{SUPABASE_URL}/rest/v1/telegram_subscriptions",
        headers={**HEADERS, "Prefer": "return=representation"},
        params={"chat_id": f"eq.{chat_id}", "username": f"eq.{username}"},
        timeout=25,
    )
    response.raise_for_status()
    removed = bool(response.json())

    # Remove the shared watch row only when nobody subscribes to it.
    remaining = supabase_get(
        "telegram_subscriptions",
        {"select": "chat_id", "username": f"eq.{username}", "limit": "1"},
    )
    if not remaining:
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/telegram_watchlist",
            headers=HEADERS,
            params={"username": f"eq.{username}"},
            timeout=25,
        ).raise_for_status()
    return removed


def process_telegram_command(update):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat", {})
    user = message.get("from", {})
    text = (message.get("text") or "").strip()

    # Only private chats are supported; this prevents accidental group broadcasts.
    if chat.get("type") != "private" or not user.get("id"):
        return
    chat_id = int(chat["id"])
    parts = text.split()
    if not parts:
        return
    command = parts[0].split("@")[0].lower()

    if command == "/start":
        register_telegram_user(chat_id, user)
        telegram_send(
            chat_id,
            "Welcome to TikTok LIVE Checker!\n\n"
            "Each Telegram user has a separate watchlist.\n"
            "/add username - follow a TikTok account\n"
            "/remove username - unfollow an account\n"
            "/list - show your watchlist\n"
            "/live or /check - show followed accounts currently LIVE\n"
            "/help - show commands\n\n"
            f"Maximum {MAX_TELEGRAM_ACCOUNTS} accounts per Telegram user.",
        )
        return

    # Register the user on first command as well; /start is not mandatory.
    register_telegram_user(chat_id, user)

    if command == "/help":
        telegram_send(
            chat_id,
            "/add username - add an account\n"
            "/remove username - remove an account\n"
            "/list - show your own watchlist\n"
            "/live or /check - show followed accounts currently LIVE\n"
            f"Limit: {MAX_TELEGRAM_ACCOUNTS} accounts per user.",
        )
    elif command == "/add":
        if len(parts) < 2:
            telegram_send(chat_id, "Usage: /add username")
            return
        username = clean_username(parts[1])
        if not username:
            telegram_send(chat_id, "Invalid TikTok username or profile URL.")
            return
        result = add_subscription(chat_id, username)
        messages = {
            "added": f"✅ Added @{username}. I'll notify only this Telegram account when their LIVE starts or ends.",
            "duplicate": f"@{username} is already in your watchlist.",
            "limit": f"You have reached the {MAX_TELEGRAM_ACCOUNTS}-account limit. Use /remove first.",
        }
        telegram_send(chat_id, messages[result])
    elif command == "/remove":
        if len(parts) < 2:
            telegram_send(chat_id, "Usage: /remove username")
            return
        username = clean_username(parts[1])
        if not username:
            telegram_send(chat_id, "Invalid TikTok username or profile URL.")
            return
        removed = remove_subscription(chat_id, username)
        telegram_send(
            chat_id,
            f"🗑 Removed @{username}." if removed else f"@{username} is not in your watchlist.",
        )
    elif command == "/list":
        rows = get_user_subscriptions(chat_id)
        if not rows:
            telegram_send(chat_id, "Your watchlist is empty. Add one with /add username.")
            return
        status_rows = supabase_get(
            "telegram_watchlist",
            {
                "select": "username,is_live,checked_at",
                "username": "in.(" + ",".join(row["username"] for row in rows) + ")",
            },
        )
        status_map = {r["username"].lower(): r["is_live"] for r in status_rows}
        lines = ["📋 Your TikTok watchlist:"]
        for row in rows:
            status = status_map.get(row["username"].lower())
            label = "⏳ Waiting for check" if status is None else ("🔴 LIVE" if status else "⚫ Offline")
            lines.append(f"• @{row['username']} — {label}")
        telegram_send(chat_id, "\n".join(lines))
    elif command in ("/live", "/check"):
        rows = get_user_subscriptions(chat_id)
        if not rows:
            telegram_send(chat_id, "Your watchlist is empty. Add one with /add username.")
            return
        names = [row["username"] for row in rows]
        status_rows = supabase_get(
            "telegram_watchlist",
            {
                "select": "username,is_live",
                "username": "in.(" + ",".join(names) + ")",
            },
        )
        live_names = [r["username"] for r in status_rows if r["is_live"] is True]
        if live_names:
            body = "🔴 Currently LIVE:\n" + "\n".join(
                f"• @{name} — https://www.tiktok.com/@{name}/live" for name in live_names
            )
        else:
            body = "None of the accounts in your watchlist are currently marked LIVE. The next scheduled scan will refresh their status."
        telegram_send(chat_id, body)
    else:
        # Ignore unrelated messages; do not treat normal chat as a command.
        return


def poll_telegram_updates():
    offset = get_update_offset()
    payload = {"timeout": 0, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    updates = telegram_call("getUpdates", payload, timeout=25) or []
    for update in updates:
        update_id = update.get("update_id")
        try:
            process_telegram_command(update)
        except Exception as error:
            print(f"Telegram update {update_id} failed: {type(error).__name__}: {error}")
            # Do not advance offset past a failed update; it can be retried next run.
            break
        if update_id is not None:
            save_update_offset(update_id)


# ============================================================
# Existing PWA push notifications
# ============================================================

def send_push(user_id, username, event_type):
    subscriptions = supabase_get(
        "push_subscriptions",
        {"select": "id,subscription", "user_id": f"eq.{user_id}"},
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
                vapid_claims={"sub": VAPID_SUBJECT},
            )
            print(f"PWA push sent for @{username}.")
        except WebPushException as error:
            print(f"Push failed for @{username}: {error}")
        except Exception as error:
            print(f"Unexpected push error for @{username}: {type(error).__name__}: {error}")


def send_legacy_telegram(message):
    if not LEGACY_TELEGRAM_CHAT_ID:
        return
    try:
        telegram_send(LEGACY_TELEGRAM_CHAT_ID, message)
    except Exception as error:
        print(f"Legacy Telegram alert failed: {type(error).__name__}: {error}")


# ============================================================
# TikTok checking
# ============================================================

async def check_live(username):
    client = TikTokLiveClient(unique_id=f"@{username}")
    try:
        return bool(await asyncio.wait_for(client.is_live(), timeout=25))
    finally:
        try:
            await client.web.close()
        except Exception:
            pass


def notify_telegram_subscribers(username, event_type):
    subscribers = supabase_get(
        "telegram_subscriptions",
        {"select": "chat_id", "username": f"eq.{username}"},
    )
    if event_type == "LIVE_STARTED":
        message = (
            f"🔴 LIVE STARTED\n\n@{username} is LIVE!\n"
            f"https://www.tiktok.com/@{username}/live"
        )
    else:
        message = f"⚫ LIVE ENDED\n\n@{username} has ended their LIVE."
    for subscriber in subscribers:
        try:
            telegram_send(subscriber["chat_id"], message)
        except Exception as error:
            print(
                f"Telegram notification failed for @{username}, "
                f"chat {subscriber['chat_id']}: {type(error).__name__}: {error}"
            )


def scan_telegram_watchlist():
    rows = supabase_get(
        "telegram_watchlist",
        {"select": "username,is_live,checked_at", "order": "username.asc"},
    )
    print(f"Found {len(rows)} unique Telegram-only watchlist accounts.")
    for row in rows:
        username = row["username"]
        try:
            live = asyncio.run(check_live(username))
        except Exception as error:
            print(f"Could not check Telegram @{username}: {type(error).__name__}: {error}")
            continue

        was_live = row["is_live"]
        checked_at = now()
        if was_live is None:
            # Establish initial status without sending a false start alert.
            supabase_patch(
                "telegram_watchlist",
                {"username": f"eq.{username}"},
                {"is_live": live, "checked_at": checked_at},
            )
            print(f"Telegram @{username}: initial state = {'LIVE' if live else 'OFFLINE'}")
            continue

        if live != was_live:
            event_type = "LIVE_STARTED" if live else "LIVE_ENDED"
            notify_telegram_subscribers(username, event_type)

        supabase_patch(
            "telegram_watchlist",
            {"username": f"eq.{username}"},
            {"is_live": live, "checked_at": checked_at},
        )
        print(f"Telegram @{username}: {'LIVE' if live else 'OFFLINE'}")


# ============================================================
# Existing PWA-owned accounts: preserve status, events and push
# ============================================================

def scan_pwa_accounts():
    accounts = supabase_get(
        "accounts",
        {"select": "id,username,is_active,user_id", "is_active": "eq.true"},
    )
    print(f"Found {len(accounts)} active PWA accounts in Supabase.")

    for account in accounts:
        account_id = account["id"]
        username = account["username"]
        user_id = account["user_id"]

        try:
            live = asyncio.run(check_live(username))
        except Exception as error:
            print(f"Could not check PWA @{username}: {type(error).__name__}: {error}")
            continue

        try:
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
            started_at = (old.get("started_at") if was_live and old else checked_at) if live else None

            supabase_post(
                "live_status",
                {
                    "account_id": account_id,
                    "is_live": live,
                    "started_at": started_at,
                    "checked_at": checked_at,
                },
                on_conflict="account_id",
            )

            print(f"PWA @{username}: {'LIVE' if live else 'OFFLINE'}")

            if live and not was_live:
                supabase_post(
                    "live_events",
                    {
                        "account_id": account_id,
                        "event_type": "LIVE_STARTED",
                        "occurred_at": checked_at,
                    },
                )
                send_legacy_telegram(
                    f"🔴 @{username} just started a LIVE!\n"
                    f"https://www.tiktok.com/@{username}/live"
                )
                send_push(user_id, username, "LIVE_STARTED")
            elif not live and was_live:
                supabase_post(
                    "live_events",
                    {
                        "account_id": account_id,
                        "event_type": "LIVE_ENDED",
                        "occurred_at": checked_at,
                    },
                )
                send_legacy_telegram(f"⚫ @{username} has ended their LIVE.")
                send_push(user_id, username, "LIVE_ENDED")

        except Exception as error:
            print(f"Error processing PWA @{username}: {type(error).__name__}: {error}")


def main():
    print("Polling Telegram bot commands...")
    poll_telegram_updates()

    print("Checking independent Telegram watchlists...")
    scan_telegram_watchlist()

    print("Checking existing PWA accounts...")
    scan_pwa_accounts()


if __name__ == "__main__":
    main()
