# Telegram Group Manager — Fixed & Audited Project

An admin-only Telegram bot for managing approximately 30 Telegram groups. It includes group registration, join-request invite links, private admin notifications with Accept / Reject / Payment Pending actions, dedicated per-group settings menus with pending join-request inspection, permanent membership join/leave logging, and automatic deletion of in-chat entry/exit service notification messages from group chats.

---

## Features

- **Group Management**:
  - Register groups with `/addgroup` (run inside group by an authorized admin with the bot as administrator).
  - Dedicated group settings menu (`/start` -> **Manage groups** -> choose group) displaying group details, pending request counts, invite link generation, view pending requests, and unregistering.
  - Generate invite links that require admin join-request approval.
- **Join-Request Management**:
  - Private notifications to all configured admins whenever a user requests to join.
  - Interactive buttons on notification: **Accept**, **Reject**, and **Payment Pending**.
  - **View Pending Requests**: dedicated button in each group's settings menu showing all active join requests for that specific group (with user name, user ID, status, and request timestamp).
  - Working Accept, Reject, and Payment Pending actions directly inside the pending requests list, with pagination (5 per page) and refresh support.
  - Safe handling of expired, withdrawn, or duplicate requests.
  - **Admin Mention**: Payment-pending messages and applicant notifications explicitly direct applicants to admin `@jainsahiba18` (configurable via `ADMIN_CONTACT` in `.env`).
  - Deep-link support (`/start pay_<token>`) allowing applicants to view payment instructions even if they haven't started the bot before.
  - **Import Historical Requests**: `/addrequest <chat_id> <user_id> [Name]` allows importing requests that arrived before the bot joined, so the bot can accept or reject them.
- **Entry & Exit Logs & Messages**:
  - **Permanent Bot Log Storage**: The bot permanently stores all join, leave, and approval events in `events.json`. No bot log records are ever deleted from storage.
  - **In-Chat Join & Leave Messages Appear Normally**: When a member joins or leaves, the message appears in the group chat and stays visible so members and admins see it.
  - **On-Demand Message Deletion**: When you want to delete these in-chat entry/exit bubbles from the group chat, you can delete them at any time:
    1. **From private notification**: Click **"🗑 Delete join message from chat"** directly on the notification.
    2. **From group settings**: `/start` -> **Manage groups** -> Select group -> **"🗑 Clean in-chat messages"**.
    3. **From membership logs**: `/start` -> **Membership logs** -> Select group -> **"🗑 Delete entry/exit messages from chat"**.
    4. **In the group**: Type `/delmessages` to delete all pending entry/exit service messages, or reply `/del` to any specific message.
  - **Last 10 unique joiners**: shows the 10 most recent unique users who joined (rejoining does not duplicate people).
  - **Recent entry/exit logs**: view the recorded history of all membership events per group.
  - **Backfill Logs**: `/addlog <chat_id> <user_id> <join|leave> [Name]` allows backfilling historical membership entries and exits into bot storage.
- **Commands & Help Section**:
  - **Interactive Help Section**: Available via `/help` or by clicking **📖 Help & Commands** on the main `/start` menu.
  - Complete list of all supported commands:
    - `/start`: Open admin dashboard (or `/start pay_<token>` for applicants).
    - `/help`: Display interactive help guide with all commands, uses, and tips.
    - `/addgroup`: Register a Telegram group with the bot (run inside group).
    - `/groups`: Quick command to list registered groups and open settings.
    - `/links`: Quick command to generate join-request invite links.
    - `/logs`: Quick command to view permanent membership logs.
    - `/addrequest <chat_id> <user_id> [Name]`: Import historical pending join requests.
    - `/addlog <chat_id> <user_id> <join|leave> [Name]`: Backfill historical membership events.
    - `/del`: Reply `/del` to any message in a group to delete it immediately.
    - `/delmessages`: Delete all pending entry/exit service messages in the group.
- **Security & Reliability**:
  - Admin authorization strictly enforced by Telegram numeric user ID.
  - Atomic JSON writes via temporary files to avoid data corruption or partial writes.
  - Safe error handling for missing files, invalid JSON, and Telegram API errors.
  - Safe `.env` loading anchored to the project folder.

---

## Technical Note: Historical Join Requests & In-Chat Messages

### In-Chat Service Messages
The grey service messages ("Sonu was accepted into the group", "Rising star joined the group via invite link", "S left the group", "Vamshi was accepted into the group"):
- **When a user joins**: The message appears in the group as normal and is NOT deleted automatically.
- **When you want to delete them**: Simply press the **"Delete message"** button on the notification, click **"Clean in-chat messages"** in group settings, or run `/delmessages` in the group.
- **Old events from the past**: You can reply `/del` to any message in the group to delete it immediately.

### Why Telegram Bot API Does Not Automatically Fetch Past Requests
Under standard **Telegram Bot API** (`api.telegram.org`):
1. Telegram delivers `chat_join_request` updates and `chat_member` updates **as live push events only**.
2. Telegram does **not** provide a REST endpoint to query historical requests that arrived before the bot was in the group.
3. However, Telegram **does** allow a bot to approve or decline any join request using `approveChatJoinRequest` and `declineChatJoinRequest` as long as you provide the `user_id`.
4. Use `/addrequest <user_id> [Name]` to import any past pending requests so you can approve or decline them via the bot.

---

## Requirements

- Python 3.10 or newer.
- Dependencies: `python-telegram-bot>=21.6,<22.0`, `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`.
- A bot token from [@BotFather](https://t.me/BotFather).
- The numeric Telegram user ID(s) of administrators (obtainable via [@userinfobot](https://t.me/userinfobot)).
- Administrator rights in each managed group:
  - **Invite Users via Link**
  - **Manage Chat / Manage Join Requests**
  - **Delete Messages** (required so the bot can auto-delete join/leave service messages from the group chat)

---

## Windows Setup (CMD)

1. Open Windows Command Prompt (`cmd.exe`).
2. Navigate to your project folder:
   ```cmd
   cd /d "C:\Users\Lenovo\Downloads\channel manage bot"
   ```
3. Create and activate a Python virtual environment:
   ```cmd
   python -m venv .venv
   .venv\Scripts\activate
   ```
4. Install dependencies:
   ```cmd
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```
5. Configure environment variables in `.env`:
   - Copy `.env.example` to `.env` if not already created:
     ```cmd
     copy .env.example .env
     ```
   - Open `.env` in Notepad and set your variables:
     ```dotenv
     BOT_TOKEN=YOUR_BOT_TOKEN
     ADMIN_IDS=123456789,987654321
     DATA_FILE=groups.json
     REQUESTS_FILE=requests.json
     EVENTS_FILE=events.json
     ADMIN_CONTACT=@jainsahiba18
     ```
6. Run the automated tests to verify everything is working:
   ```cmd
   python -m pytest test_bot.py -v
   ```
7. Start the bot:
   ```cmd
   python bot.py
   ```
