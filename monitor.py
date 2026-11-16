import asyncio, json, os
from pathlib import Path
import requests
from TikTokLive import TikTokLiveClient

ROOT = Path(__file__).parent
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

def accounts():
    names = []
    for line in (ROOT / "accounts.txt").read_text().splitlines():
        name = line.strip().lstrip("@")
        if name and not name.startswith("#") and name not in names:
            names.append(name)
    if not names or len(names) > 5:
        raise ValueError("Add between 1 and 5 usernames to accounts.txt")
    return names

def telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Telegram rejected the message")

async def check(name):
    client = TikTokLiveClient(unique_id=f"@{name}")
    return bool(await asyncio.wait_for(client.is_live(), timeout=35))

async def main():
    state_file = ROOT / "state.json"
    try:
        old = json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}
    new = dict(old)

    for name in accounts():
        try:
            live = await check(name)
        except Exception as e:
            print(f"Could not check @{name}: {type(e).__name__}: {e}")
            continue  # Unknown is not the same as offline.

        was = old.get(name)
        print(f"@{name}: {'LIVE' if live else 'OFFLINE'}")
        if was is None:
            new[name] = live
            if live:
                telegram(f"🔴 @{name} is LIVE now\nhttps://www.tiktok.com/@{name}/live")
        elif live and was is False:
            telegram(f"🔴 @{name} just started a LIVE!\nhttps://www.tiktok.com/@{name}/live")
            new[name] = True
        elif not live and was is True:
            telegram(f"⚫ @{name} has ended their LIVE.")
            new[name] = False
        else:
            new[name] = live

    state_file.write_text(json.dumps(new, indent=2) + "\n")

if __name__ == "__main__":
    asyncio.run(main())
