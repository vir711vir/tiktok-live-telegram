# TikTok LIVE → Telegram (5 accounts)

## What it does
Checks up to five public TikTok accounts on a GitHub Actions schedule and sends Telegram alerts when they go LIVE or end their LIVE.

## Limitations
- GitHub schedules a check every 5 minutes, but runs can be delayed.
- Streams that start and end between checks can be missed.
- TikTokLive is unofficial and may break if TikTok changes its systems.
- Temporary errors are treated as unknown, never as OFFLINE.
- No TikTok password or session cookie is required.
- The first successful check establishes the baseline. If an account is already LIVE then, you receive a LIVE alert.

## Setup
1. Create a **public** GitHub repository and upload all project files, preserving `.github/workflows/tiktok-live-check.yml`.
2. In Telegram, open `@BotFather`, send `/newbot`, and follow its instructions. Keep the token private.
3. Open your new bot in Telegram and press **Start**.
4. In Safari, open `https://api.telegram.org/botYOUR_TOKEN/getUpdates` (replace `YOUR_TOKEN` with your token). Find `message` → `chat` → `id`. If no result appears, send `/start` to your bot and refresh.
5. In GitHub, open the repository → Settings → Secrets and variables → Actions → New repository secret.
   - Add `TELEGRAM_BOT_TOKEN` with the BotFather token.
   - Add `TELEGRAM_CHAT_ID` with the numeric chat ID.
6. Edit `accounts.txt` and add up to five usernames, one per line. Use usernames only, not profile URLs.
7. Open Actions. If prompted, enable workflows.
8. Choose **TikTok LIVE checker** → **Run workflow** to test it. Scheduled checks follow every five minutes.

## Security
Never put the Telegram token in any repository file. Store it only in GitHub Actions Secrets. The public repository exposes its source code and the usernames you monitor.

## Change accounts
Edit `accounts.txt` on GitHub and commit the change. The next run uses the updated list.
