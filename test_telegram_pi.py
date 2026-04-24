import json
import urllib.request
import urllib.error

# 🔥 HARDCODED DGX URL
DGX_URL = "http://10.186.71.82:8008"


def is_telegram_command(text: str) -> bool:
    return "telegram" in text.lower()


def clean_telegram_command(text: str) -> str:
    lower = text.lower()
    idx = lower.find("telegram")

    if idx == -1:
        return text.strip()

    return text[idx + len("telegram"):].strip(" ,.")


def send_to_dgx(command: str):
    try:
        payload = json.dumps({"command": command}).encode("utf-8")

        req = urllib.request.Request(
            f"{DGX_URL}/telegram",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            response_data = resp.read().decode()
            print("✅ DGX Response:", response_data)

    except urllib.error.URLError as e:
        print("❌ Failed to reach DGX:", e)
    except Exception as e:
        print("❌ Error:", e)


def main():
    print("=== Pi Telegram Test ===")

    # 🔹 Simulated transcript (no ESP32 needed)
    transcript_text = "Telegram send hello from fake Pi transcript test"

    print("Transcript:", transcript_text)

    if is_telegram_command(transcript_text):
        command = clean_telegram_command(transcript_text)
        print("Parsed command:", command)

        send_to_dgx(command)
    else:
        print("No Telegram command detected")


if __name__ == "__main__":
    main()

