import os
import base64
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

load_dotenv()

_token_b64 = os.getenv("TOKEN_PICKLE_B64")
if _token_b64 and not os.path.exists("token.pickle"):
    with open("token.pickle", "wb") as _f:
        _f.write(base64.b64decode(_token_b64))

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
NEWSLETTER_QUERY = (
    "(from:news@alphasignal.ai OR from:dan@tldrnewsletter.com) newer_than:1d"
)
AGENTIC_KEYWORDS = [
    "claude", "codex", "cursor", "warp", "mcp", "model release", "agent",
    "coding", "llm", "gpt", "gemini", "copilot", "agentic", "ai tool",
    "benchmark", "performance", "inference", "multi-agent", "single-agent",
]


def get_gmail_service():
    if not os.path.exists("token.pickle"):
        raise RuntimeError(
            "token.pickle not found. Set TOKEN_PICKLE_B64 env var on Render "
            "(run locally first to generate token.pickle, then base64-encode it)."
        )
    with open("token.pickle", "rb") as f:
        creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open("token.pickle", "wb") as f:
                pickle.dump(creds, f)
        else:
            raise RuntimeError("Gmail token is invalid and cannot be refreshed. Re-run auth locally and update TOKEN_PICKLE_B64.")
    return build("gmail", "v1", credentials=creds)


def get_email_body(service, msg_id):
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    parts = msg.get("payload", {}).get("parts", [])
    body = ""
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part["body"].get("data", "")
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            break
    if not body:
        data = msg.get("payload", {}).get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return body


def is_relevant(text):
    text_lower = text.lower()
    return any(kw in text_lower for kw in AGENTIC_KEYWORDS)


JUNK_PHRASES = [
    "unsubscribe", "manage your subscriptions", "links: ------",
    "privacy policy", "work with us", "follow on x", "signup",
    "forward →", "presented by", "utm_source", "grab your seat",
    "learn more →", "get the guide", "read more", "‌",
]


def is_junk(text):
    t = text.lower()
    return any(j in t for j in JUNK_PHRASES)


def extract_topics(body, subject):
    topics = []
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    current_headline = subject
    first_sentence = ""

    for line in lines:
        if is_junk(line):
            continue
        is_header = (
            len(line) < 100
            and not line.endswith((",", ";", ":"))
            and line[0].isupper()
            and len(line.split()) >= 4
        )
        if is_header:
            if first_sentence and is_relevant(current_headline + " " + first_sentence):
                topics.append((current_headline, first_sentence))
            current_headline = line
            first_sentence = ""
        elif not first_sentence and len(line) > 30 and not is_junk(line):
            # Take only the first meaningful sentence
            sentence = line.split(".")[0].strip()
            if len(sentence) > 20:
                first_sentence = sentence[:200]

    if first_sentence and is_relevant(current_headline + " " + first_sentence):
        topics.append((current_headline, first_sentence))

    return topics


def fetch_digest():
    service = get_gmail_service()
    results = service.users().threads().list(userId="me", q=NEWSLETTER_QUERY, maxResults=5).execute()
    threads = results.get("threads", [])

    digest_items = []

    for thread in threads:
        thread_data = service.users().threads().get(userId="me", id=thread["id"]).execute()
        messages = thread_data.get("messages", [])
        for msg in messages:
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            subject = headers.get("Subject", "No Subject")
            body = get_email_body(service, msg["id"])
            topics = extract_topics(body, subject)
            digest_items.extend(topics)

    return digest_items


def safe(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_digest(items):
    if not items:
        return "No agentic coding news found in the last 24 hours."
    lines = ["<b>Agentic Coding Digest</b>\n"]
    for i, (headline, summary) in enumerate(items[:8], 1):
        lines.append(f"<b>{i}. {safe(headline)}</b>")
        lines.append(f"{safe(summary)}\n")
    return "\n".join(lines)


@app.route("/")
def index():
    return "Gmail Digest Bot is running. Use /webhook for Telegram updates.", 200


@app.route("/debug")
def debug():
    """Visit this URL in browser to see exactly what the bot would send."""
    try:
        token_exists = os.path.exists("token.pickle")
        token_b64_set = bool(os.getenv("TOKEN_PICKLE_B64"))
        items = fetch_digest()
        result = format_digest(items)
        return (
            f"<pre>token.pickle exists: {token_exists}\n"
            f"TOKEN_PICKLE_B64 set: {token_b64_set}\n"
            f"Items found: {len(items)}\n\n"
            f"--- OUTPUT ({len(result)} chars) ---\n\n{result}</pre>"
        ), 200
    except Exception as e:
        return f"<pre>ERROR: {e}</pre>", 500


@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()

    message = data.get("message") or data.get("edited_message")
    if not message:
        return jsonify({"status": "ok"}), 200

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    sender = message["from"].get("first_name", "User")

    print(f"Message from {sender} ({chat_id}): {text}")

    if text.lower() in ("/digest", "/news"):
        send_message(chat_id, "Fetching latest agentic coding news...")
        try:
            items = fetch_digest()
            reply = format_digest(items)
            send_message(chat_id, reply, parse_mode="HTML")
        except Exception as e:
            send_message(chat_id, f"Error fetching digest: {e}")
        return jsonify({"status": "ok"}), 200
    else:
        reply = "Send /digest to get the latest agentic coding news from your newsletters."

    send_message(chat_id, reply)
    return jsonify({"status": "ok"}), 200


def send_message(chat_id: int, text: str, parse_mode: str = None):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    response = requests.post(url, json=payload)
    if not response.ok:
        print(f"Failed to send message: {response.status_code} {response.text}")


if __name__ == "__main__":
    get_gmail_service()  # authenticate once at startup, opens browser if needed
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
