from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import re
import threading
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle, ChatMemberStatus
from pyrogram.errors import RPCError
from pyrogram.types import ChatJoinRequest, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from tracker import OsintBot
from flask import Flask

# Keep literal placeholders such as ${NUMBER} in endpoint templates. Without
# this, python-dotenv expands them while reading .env and sends an empty value.
load_dotenv(interpolate=False)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


web_app = Flask(__name__)

@web_app.get("/")
def home():
    return "Telegram bot is running"


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing {name}. Add it to your .env file.")
    return value


osint_bot = OsintBot(
    mongo_uri=required("MONGO_URI"),
    database_name=os.getenv("MONGO_DATABASE", "osint_bot"),
)

def chat_id_from_env(name: str) -> int | str:
    value = required(name)
    return int(value) if value.lstrip("-").isdigit() else value


REQUIRED_CHANNELS = (
    (chat_id_from_env("CHANNEL_1_ID"), required("CHANNEL_1_URL"), "Channel 1"),
    (chat_id_from_env("CHANNEL_2_ID"), required("CHANNEL_2_URL"), "Channel 2"),
    (chat_id_from_env("CHANNEL_3_ID"), required("CHANNEL_3_URL"), "Channel 3"),
)
PENDING_JOIN_REQUESTS: dict[int | str, set[int]] = {}


def admin_user_ids() -> frozenset[int]:
    """Read one or more comma-separated Telegram administrator IDs."""
    raw_ids = os.getenv("ADMIN_USER_ID", "")
    ids: set[int] = set()
    for raw_id in raw_ids.split(","):
        raw_id = raw_id.strip()
        if not raw_id or raw_id == "0":
            continue
        if raw_id.isdigit():
            ids.add(int(raw_id))
        else:
            LOGGER.warning("Ignoring invalid ADMIN_USER_ID value")
    return frozenset(ids)


ADMIN_USER_IDS = admin_user_ids()
BOT_USERNAME = os.getenv("BOT_USERNAME", "OsintYutaBot").lstrip("@")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

app = Client(
    "osint_bot",
    api_id=int(required("API_ID")),
    api_hash=required("API_HASH"),
    bot_token=required("BOT_TOKEN"),
)

WELCOME_TEXT = (
        "<b>🕵️‍♂️ OSINT Bot</b>\n\n"
        "I can help you find information using osint commands.\n\n"

        "<b>💳 Credit:</b> {credits}\n\n"

        "── ── ── ── ── ── ── ── ── ── ── ── ── ── ──\n"
        "<b>🛠 Available Commands:</b>\n\n"
        "<code>/num number</code> - get number information\n"
        "<code>/refer</code> - get your referral link and rewards\n"

        "\n<b>⚠️ Important:</b>\n"
        "• This bot is for educational purposes only\n"
        "• Do not use for illegal activities\n"
        "• Users are responsible for their actions\n"
        "• Unauthorized use is strictly prohibited\n"
        "• Your results messages will be auto-deleted after 300 seconds"
)

JOIN_TEXT = (
    "<b>OSINT BOT</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>Must Join all channels then You can use this bot</b>"
)


def join_keyboard(missing_channels: tuple[tuple[int | str, str, str], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(name, url=url, style=ButtonStyle.PRIMARY)]
            for _, url, name in missing_channels
        ]
        + [[
            InlineKeyboardButton(
                "✅ Joined",
                callback_data="verify_subscription",
                style=ButtonStyle.SUCCESS,
            )
        ]]
    )


def start_menu_keyboard() -> InlineKeyboardMarkup:
    """Buttons shown beneath the welcome message."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Update Channel",
                    url=REQUIRED_CHANNELS[0][1],
                    style=ButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    "👤 Owner", user_id=OWNER_ID, style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "🤝 Refer & Earn",
                    callback_data="refer_earn",
                    style=ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def referral_keyboard() -> InlineKeyboardMarkup:
    """Buttons for the referral view."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 Copy Referral Link",
                    callback_data="copy_referral_link",
                    style=ButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back", callback_data="refer_back", style=ButtonStyle.DANGER
                )
            ],
        ]
    )


async def missing_required_channels(user_id: int) -> tuple[tuple[int | str, str, str], ...]:
    """Return channels where the user is neither joined nor awaiting approval."""
    inactive_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
    missing: list[tuple[int | str, str, str]] = []
    for channel in REQUIRED_CHANNELS:
        chat_id, _, _ = channel
        try:
            member = await app.get_chat_member(chat_id, user_id)
            if member.status not in inactive_statuses:
                PENDING_JOIN_REQUESTS.get(chat_id, set()).discard(user_id)
                await asyncio.to_thread(osint_bot.clear_pending_join_request, chat_id, user_id)
            else:
                if not await has_pending_join_request(chat_id, user_id):
                    missing.append(channel)
        except RPCError:
            # A non-member raises an RPC error, so check the pending-request list too.
            if not await has_pending_join_request(chat_id, user_id):
                missing.append(channel)
    return tuple(missing)


async def has_pending_join_request(chat_id: int | str, user_id: int) -> bool:
    """Check a pending request received by this bot, including before a restart."""
    return (
        user_id in PENDING_JOIN_REQUESTS.get(chat_id, set())
        or await asyncio.to_thread(osint_bot.has_pending_join_request, chat_id, user_id)
    )


@app.on_chat_join_request()
async def process_join_request(_: Client, request: ChatJoinRequest) -> None:
    """Record pending access only for invite links that require approval."""
    invite_link = request.invite_link
    if not invite_link or not invite_link.creates_join_request:
        return

    for chat_id, _, _ in REQUIRED_CHANNELS:
        if request.chat.id == chat_id:
            PENDING_JOIN_REQUESTS.setdefault(chat_id, set()).add(request.from_user.id)
            await asyncio.to_thread(osint_bot.record_pending_join_request, chat_id, request.from_user.id)
            LOGGER.info("Granted bot access for pending request from user %s in chat %s", request.from_user.id, chat_id)
            return


async def send_start_message(message: Message, user_id: int | None = None) -> None:
    if user_id is None and not message.from_user:
        return
    credits = await asyncio.to_thread(
        osint_bot.get_credits, user_id if user_id is not None else message.from_user.id
    )
    await message.reply_text(
        WELCOME_TEXT.format(credits=credits), reply_markup=start_menu_keyboard()
    )


async def referral_view_text(user_id: int) -> str:
    """Build the referral screen shown from the inline menu."""
    referrals, earned_credits = await asyncio.to_thread(osint_bot.referral_stats, user_id)
    progress = referrals % 2
    percent = progress * 50
    progress_bar = "■" * (progress * 5) + "▱" * (10 - progress * 5)
    remaining = 2 - progress
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    return (
        "<b>🤝 REFER &amp; EARN</b>\n\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{referral_link}</code>\n\n"
        "📊 <b>Your Stats:</b>\n"
        f"• Total Successful Referrals: <b>{referrals}</b>\n"
        f"• Credits Earned from Referrals: <b>{earned_credits}</b>\n\n"
        "🎯 <b>Progress to Next Credit:</b>\n"
        f"{progress_bar} {percent}%\n"
        f"┗ ➤ {remaining} more referral(s) needed\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How it works:</b>\n"
        "• Share your referral link with friends.\n"
        "• When they start the bot using your link and join all required channels, they become your referral.\n"
        "• For every 2 successful referrals, you earn <b>1 FREE Credit</b>!\n"
        "• Referrals are counted only after channel membership is verified.\n\n"
        "🚀 <b>Start sharing now and earn free credits!</b>"
    )


async def remove_loading_message(message: Message) -> None:
    """Delete a temporary loading reply without hiding the final result."""
    try:
        await message.delete()
    except RPCError:
        LOGGER.debug("Could not delete loading message %s", message.id)


async def notify_referrer(referrer_id: int | None, referred_user: Any) -> None:
    """Tell the referrer when their referral completes channel verification."""
    if referrer_id is None:
        return
    name = html.escape(referred_user.first_name or referred_user.username or "A user")
    try:
        await app.send_message(
            referrer_id,
            f"{name} successfully joined the bot with your referral link.",
        )
    except RPCError:
        LOGGER.warning("Could not notify referrer %s", referrer_id)


Aadhaar_RE = re.compile(r"(?<!\d)(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
SENSITIVE_KEYS = {"aadhar", "aadhaar", "aadharno", "aadhaarno", "uid", "uidai"}
REMOVED_KEYS = {"owner", "metadata"}


def sanitize(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(item): sanitize(child, str(item))
            for item, child in value.items()
            if re.sub(r"[^a-z0-9]", "", str(item).lower()) not in REMOVED_KEYS
        }

    if isinstance(value, list):
        return [sanitize(item, key) for item in value]

    return value


def format_result(data: Any) -> str:
    safe_data = sanitize(data)
    return json.dumps(safe_data, indent=2, ensure_ascii=True, default=str)


def result_file(data: Any, filename: str) -> io.BytesIO:
    document = io.BytesIO(format_result(data).encode("utf-8"))
    document.name = filename
    return document


def json_message(data: Any) -> str:
    return "```json\n" + format_result(data) + "\n```"


def normalise_phone_number(value: str) -> str | None:
    """Return the 10-digit Indian mobile number expected by the configured API."""
    digits = re.sub(r"[\s-]", "", value)
    if digits.startswith("+"):
        digits = digits[1:]
    if not digits.isdigit():
        return None
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if len(digits) == 10 else None


def api_error_message(data: Any) -> str | None:
    """Return a safe, user-facing message from a failed JSON API response."""
    if not isinstance(data, dict) or data.get("status") is not False:
        return None
    message = data.get("message")
    return message if isinstance(message, str) and message else "No result found."


def combine_lookup_results(number_data: Any, aadhar_data: Any) -> Any:
    if aadhar_data:
        return {"number_info": number_data, "aadhaar_info": aadhar_data}
    return number_data


def is_no_result_payload(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    status = data.get("status")
    message = data.get("message")
    if status is False and isinstance(message, str):
        normalized = re.sub(r"[^a-z0-9]", "", message.lower())
        if "nonumberdatafound" in normalized or "numberdatafound" in normalized and "no" in normalized:
            return True
    return False


def build_api_url(template: str, placeholder: str, value: str, parameter: str) -> str:
    encoded_value = quote(value, safe="")
    expanded = template.replace("${" + placeholder + "}", encoded_value)
    if "${" + placeholder + "}" in expanded:
        raise ValueError(f"Unresolved API placeholder: {placeholder}")

    parts = urlsplit(expanded)
    if parameter not in parts.query:
        separator = "&" if parts.query else ""
        expanded = urlunsplit(parts._replace(query=parts.query + separator + f"{parameter}={encoded_value}"))
    return expanded


def start_referrer_id(message: Message) -> int | None:
    """Extract a numeric referrer ID from a /start deep-link command."""
    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) != 2:
        return None
    value = parts[1].strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


@app.on_message(filters.command("num") & filters.private)
async def lookup_number(_: Client, message: Message) -> None:
    if not message.from_user:
        return

    missing_channels = await missing_required_channels(message.from_user.id)

    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return

    await asyncio.to_thread(osint_bot.register_user, message.from_user)

    parts = message.text.split(maxsplit=1) if message.text else []
    number = normalise_phone_number(parts[1]) if len(parts) == 2 else None
    if not number:
        await message.reply_text("Usage: /num <10-digit mobile number>")
        return

    first_api = os.getenv("NUM_TO_INFO", "").strip()
    second_api = os.getenv("AADHAR_TO_INFO", "").strip()
    if not first_api:
        await message.reply_text("The number lookup API is not configured.")
        return

    has_credit = await asyncio.to_thread(osint_bot.consume_credit, message.from_user.id)
    if not has_credit:
        await message.reply_text("<b><i>Contact the administrator to add credits to your account before using this command.\n\n Admin Contact: @its_aadish or @GodUHappy</b></i>")
        return

    loading_message = await message.reply_text("Fetching the number information…")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            first_url = build_api_url(first_api, "NUMBER", number, "number")
            first_response = await client.get(first_url)
            first_response.raise_for_status()
            first_data = first_response.json()
            error_message = api_error_message(first_data)
            if error_message:
                await remove_loading_message(loading_message)
                await message.reply_text(error_message)
                return

            second_data = {}
            if second_api:
                aadhaar = find_aadhaar(first_data)
                if aadhaar:
                    try:
                        second_url = build_api_url(second_api, "AADHAR", aadhaar, "aadhar")
                        second_response = await client.get(second_url)
                        second_response.raise_for_status()
                        second_data = second_response.json()
                    except (httpx.HTTPError, ValueError, KeyError) as error:
                        LOGGER.warning("Aadhaar lookup failed; using number-only data. Error: %s", type(error).__name__)
                        second_data = {}
    except httpx.HTTPStatusError as error:
        LOGGER.warning("Number API returned HTTP %s", error.response.status_code)
        await remove_loading_message(loading_message)
        await message.reply_text("The lookup service rejected the request. Please try again later.")
        return
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOGGER.warning("Lookup failed: %s", type(error).__name__)
        await remove_loading_message(loading_message)
        await message.reply_text("Lookup failed. Please try again later.")
        return

    combined_data = combine_lookup_results(first_data, second_data)
    if is_no_result_payload(first_data):
        await remove_loading_message(loading_message)
        await message.reply_text("**No result found**")
        return
    if not combined_data:
        await remove_loading_message(loading_message)
        await message.reply_text("**No result found**")
        return

    result_text = json_message(combined_data)
    await remove_loading_message(loading_message)
    if len(result_text) > 4096:
        await message.reply_document(result_file(combined_data, "lookup-result.txt"))
        return

    await message.reply_text(result_text)


@app.on_message(filters.command("refer") & filters.private)
async def refer_command(_: Client, message: Message) -> None:
    """Show a user's referral link and completed-referral progress."""
    if not message.from_user:
        return
    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return

    await asyncio.to_thread(osint_bot.register_user, message.from_user)
    referrals, earned_credits = await asyncio.to_thread(
        osint_bot.referral_stats, message.from_user.id
    )
    progress = referrals % 2
    percent = progress * 50
    progress_bar = "▰" * (progress * 5) + "▱" * (10 - progress * 5)
    remaining = 2 - progress
    referral_link = f"https://t.me/{BOT_USERNAME}?start={message.from_user.id}"
    await message.reply_text(
        "<b>🤝 REFER &amp; EARN</b>\n\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{referral_link}</code>\n\n"
        "📊 <b>Your Stats:</b>\n"
        f"• Total Successful Referrals: <b>{referrals}</b>\n"
        f"• Credits Earned from Referrals: <b>{earned_credits}</b>\n\n"
        "🎯 <b>Progress to Next Credit:</b>\n"
        f"{progress_bar} {percent}%\n"
        f"┗ ➤ {remaining} more referral(s) needed\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How it works:</b>\n"
        "• Share your referral link with friends.\n"
        "• When they start the bot using your link and join all required channels, they become your referral.\n"
        "• For every 2 successful referrals, you earn <b>1 FREE Credit</b>!\n"
        "• Referrals are counted only after channel membership is verified.\n\n"
        "🚀 <b>Start sharing now and earn free credits!</b>"
        ,
        reply_markup=referral_keyboard(),
    )


@app.on_message(filters.command("givecredit") & filters.private)
async def give_credit_command(_: Client, message: Message) -> None:
    """Allow the configured administrator to add credits to registered users."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    parts = message.text.split(maxsplit=2) if message.text else []
    if len(parts) != 3:
        await message.reply_text("Usage: /givecredit <amount> <all|userid>")
        return

    try:
        amount = int(parts[1])
    except ValueError:
        amount = 0
    if amount <= 0:
        await message.reply_text("Credit amount must be a positive whole number.")
        return

    recipient = parts[2].strip().lower()
    if recipient == "all":
        count = await asyncio.to_thread(osint_bot.add_credits_to_all, amount)
        await message.reply_text(f"Added {amount} credit(s) to {count} user(s).")
        return

    if not recipient.isdigit():
        await message.reply_text("Usage: /givecredit <amount> <all|userid>")
        return

    updated = await asyncio.to_thread(osint_bot.add_credits, int(recipient), amount)
    if not updated:
        await message.reply_text("That user has not started the bot yet.")
        return
    await message.reply_text(f"Added {amount} credit(s) to user {recipient}.")


def find_aadhaar(value: Any) -> str | None:
    """Find an Aadhaar field internally; it is never included in bot output."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"aadhar", "aadhaar", "aadharno", "aadhaarno", "uid"}:
                digits = re.sub(r"\D", "", str(child))
                if len(digits) == 12:
                    return digits
            found = find_aadhaar(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_aadhaar(child)
            if found:
                return found
    return None


@app.on_message(filters.command("start") & filters.private)
async def start_command(_: Client, message: Message) -> None:
    if not message.from_user:
        return
    referrer_id = start_referrer_id(message)
    await asyncio.to_thread(osint_bot.register_user, message.from_user, referrer_id)
    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        await message.reply_text(JOIN_TEXT, reply_markup=join_keyboard(missing_channels))
        return
    referrer_id = await asyncio.to_thread(
        osint_bot.complete_referral, message.from_user.id
    )
    await notify_referrer(referrer_id, message.from_user)
    await send_start_message(message)


@app.on_callback_query(filters.regex("^verify_subscription$"))
async def verify_subscription(_: Client, callback_query: CallbackQuery) -> None:
    user = callback_query.from_user
    missing_channels = await missing_required_channels(user.id)
    if missing_channels:
        await callback_query.answer(
            "Please join every required channel first.",
            show_alert=True,
        )
        return

    await asyncio.to_thread(osint_bot.register_user, user)
    referrer_id = await asyncio.to_thread(osint_bot.complete_referral, user.id)
    await notify_referrer(referrer_id, user)
    await callback_query.answer("Membership verified!")
    await callback_query.message.delete()
    await send_start_message(callback_query.message, user.id)


@app.on_callback_query(filters.regex("^refer_earn$"))
async def show_referral_view(_: Client, callback_query: CallbackQuery) -> None:
    user = callback_query.from_user
    missing_channels = await missing_required_channels(user.id)
    if missing_channels:
        await callback_query.answer("Please join every required channel first.", show_alert=True)
        return

    await asyncio.to_thread(osint_bot.register_user, user)
    await callback_query.message.edit_text(
        await referral_view_text(user.id), reply_markup=referral_keyboard()
    )
    await callback_query.answer()


@app.on_callback_query(filters.regex("^copy_referral_link$"))
async def copy_referral_link(_: Client, callback_query: CallbackQuery) -> None:
    referral_link = f"https://t.me/{BOT_USERNAME}?start={callback_query.from_user.id}"
    await callback_query.answer(f"Copy this referral link:\n{referral_link}", show_alert=True)


@app.on_callback_query(filters.regex("^refer_back$"))
async def show_start_view(_: Client, callback_query: CallbackQuery) -> None:
    user = callback_query.from_user
    credits = await asyncio.to_thread(osint_bot.get_credits, user.id)
    await callback_query.message.edit_text(
        WELCOME_TEXT.format(credits=credits), reply_markup=start_menu_keyboard()
    )
    await callback_query.answer()



if __name__ == "__main__":
    LOGGER.info("Starting bot")
    threading.Thread(
        target=lambda: web_app.run(
            host="0.0.0.0",
            port=int(os.getenv("PORT", "5000")),
            use_reloader=False,
        ),
        daemon=True,
    ).start()
    app.run()
