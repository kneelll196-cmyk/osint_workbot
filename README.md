# Telegram Bot

Telegram bot built with **Kurigram 2.2.17+**, Python, and MongoDB. It verifies required channel membership, registers users, and supports the configured `/num` command.

## Commands

- `/start` — show bot instructions and register the user in MongoDB.
- `/num +919876543210` — look up a phone number using the configured `NUM_TO_INFO` endpoint.

## Setup

1. Install Python 3.10+ and MongoDB.
2. Create and activate a virtual environment.
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and add your Telegram API ID, API hash, bot token, and MongoDB URI. Add `NUM_TO_INFO` only if you want to enable `/num`.
   - Obtain `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).
   - Create the bot token with [@BotFather](https://t.me/BotFather).
   - Set `CHANNEL_1_ID` to the private channel's numeric `-100...` ID, and add the bot as an **administrator** in both required channels. Keep the supplied invite URLs in `CHANNEL_1_URL` and `CHANNEL_2_URL`; Telegram cannot check membership using a private invite URL alone.
5. Start the bot:

   ```bash
   python main.py
   ```

The database stores Telegram user profiles in `users`. It saves minimal pending-request records in `pending_join_requests` only for invite links that require admin approval, so those users remain eligible after a bot restart. Direct-link joins are checked live and are not stored.

## Required group/channel check

On `/start`, users must join both configured groups or channels. The bot displays two join buttons and a **Joined** button; pressing it verifies their memberships before enabling commands. Users with a pending join request are allowed to use the bot, but their group/channel request is not automatically approved. The bot must be an administrator in every configured group/channel to receive and verify join requests.

## Privacy

Use the bot only with proper authorization, follow applicable laws, and secure MongoDB access.
