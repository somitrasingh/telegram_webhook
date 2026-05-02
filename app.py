import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


@app.route("/webhook", methods=["POST"])
def receive_message():
    """Telegram sends all incoming updates here."""
    data = request.get_json()

    message = data.get("message") or data.get("edited_message")
    if not message:
        return jsonify({"status": "ok"}), 200

    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    sender = message["from"].get("first_name", "User")

    print(f"Message from {sender} ({chat_id}): {text}")

    # --- Your reply logic here ---
    reply = f"You said: {text}"
    send_message(chat_id, reply)

    return jsonify({"status": "ok"}), 200


def send_message(chat_id: int, text: str):
    """Send a text message via Telegram Bot API."""
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    response = requests.post(url, json=payload)
    if not response.ok:
        print(f"Failed to send message: {response.status_code} {response.text}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
