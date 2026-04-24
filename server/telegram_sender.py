import requests

BOT_TOKEN = "8651924169:AAFhja-ZRfqCEV3Q6yy4gZAM6tCIthpNLDg"
CHAT_ID = "-5141486260"

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_telegram_message(text: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")

    return data


def handle_telegram_voice_command(command_text: str) -> str:
    text = command_text.strip()

    if text.lower().startswith("send "):
        text = text[5:].strip()

    if not text:
        return "No Telegram message provided."

    result = send_telegram_message(text)
    msg_id = result["result"]["message_id"]

    return f"Telegram sent: {text} (id={msg_id})"
