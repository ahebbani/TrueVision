"""
Simple Telegram sender for TrueVision (hardcoded config).
⚠️ Token is hardcoded by request — do NOT commit this file to public repos.
"""

import requests

# ── HARD-CODED CONFIG ────────────────────────────────────────────────
BOT_TOKEN = "8651924169:AAFhja-ZRfqCEV3Q6yy4gZAM6tCIthpNLDg"
CHAT_ID = "-5141486260"

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ── CORE SEND FUNCTION ───────────────────────────────────────────────
def send_telegram_message(text: str) -> dict:
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
    }

    response = requests.post(
        f"{BASE_URL}/sendMessage",
        json=payload,
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")

    return data


# ── MESSAGE FORMATTER ────────────────────────────────────────────────
def build_message(command: str, content: str, speaker_name: str = "TrueVision") -> str:
    content = content.strip()

    if command == "send":
        return content

    if command == "ask":
        return f"{speaker_name} is asking: {content}"

    if command == "tell":
        return f"{speaker_name} says: {content}"

    if command == "announce":
        return f"Announcement from {speaker_name}: {content}"

    # fallback
    return content


# ── VOICE COMMAND HANDLER ────────────────────────────────────────────
def handle_telegram_voice_command(command_text: str) -> str:
    """
    Input examples (from speech → Whisper):
        "send hello team"
        "tell I am outside"
        "announce demo is working"

    Returns a status string (sent back to Pi UI).
    """

    text = command_text.strip()
    if not text:
        return "No Telegram command provided."

    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    content = parts[1] if len(parts) > 1 else ""

    if command not in {"send", "ask", "tell", "announce"}:
        command = "send"
        content = text

    final_message = build_message(command, content)

    result = send_telegram_message(final_message)
    msg_id = result["result"]["message_id"]

    return f"Sent: {final_message} (id={msg_id})"