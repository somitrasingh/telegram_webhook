import os
import re
import io
import base64
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from openai import OpenAI
import pickle

load_dotenv()

_token_b64 = os.getenv("TOKEN_PICKLE_B64")
if _token_b64 and not os.path.exists("token.pickle"):
    with open("token.pickle", "wb") as _f:
        _f.write(base64.b64decode(_token_b64))

app = Flask(__name__)

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Stores last digest per chat so number replies map to the correct topics
pending_digest: dict[int, list[tuple[str, str]]] = {}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

DRIVE_FOLDER_NAME = "agiorbit-research"
_drive_folder_id: str | None = None
NEWSLETTER_QUERY = (
    "(from:news@alphasignal.ai OR from:dan@tldrnewsletter.com) newer_than:1d"
)
AGENTIC_KEYWORDS = [
    "claude", "codex", "cursor", "warp", "mcp", "model release", "agent",
    "coding", "llm", "gpt", "gemini", "copilot", "agentic", "ai tool",
    "benchmark", "performance", "inference", "multi-agent", "single-agent",
]


def _load_creds():
    """Load, refresh, or initiate OAuth flow; return valid Credentials."""
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
    with open("token.pickle", "wb") as f:
        pickle.dump(creds, f)
    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=_load_creds())


def get_drive_service():
    return build("drive", "v3", credentials=_load_creds())


def get_research_folder_id(drive_service) -> str:
    global _drive_folder_id
    if _drive_folder_id:
        return _drive_folder_id
    query = (
        f"name='{DRIVE_FOLDER_NAME}' "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        _drive_folder_id = files[0]["id"]
    else:
        folder = drive_service.files().create(
            body={"name": DRIVE_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
            fields="id",
        ).execute()
        _drive_folder_id = folder["id"]
    return _drive_folder_id


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


def is_topic_selection(text: str) -> bool:
    """Return True if the message is purely a comma/space-separated list of integers."""
    return bool(re.fullmatch(r'[\d ,]+', text.strip())) and bool(re.search(r'\d', text))


def parse_topic_nums(text: str) -> list[int]:
    return [int(n) for n in re.findall(r'\d+', text)]


def research_topic(topic_num: int, headline: str, summary: str) -> str:
    """Call OpenAI to research a topic and upload the result to Google Drive; return filename."""
    prompt = (
        f"Research the following AI/coding topic and give a detailed but concise summary.\n\n"
        f"Topic: {headline}\n"
        f"Context: {summary}\n\n"
        f"Cover:\n"
        f"1. What this is about (2-3 sentences)\n"
        f"2. Why it matters for developers / AI practitioners\n"
        f"3. Key technical details or implications\n\n"
        f"Keep the response under 400 words."
    )
    response = openai_client.chat.completions.create(
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
    )
    text = response.choices[0].message.content.strip()

    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = re.sub(r'[^a-zA-Z0-9 _-]', '', headline[:40]).strip().replace(' ', '_')
    filename = f"{date_str}_topic{topic_num}_{slug}.md"

    content = f"# {headline}\n\n**Context:** {summary}\n\n**Research Date:** {date_str}\n\n---\n\n{text}"

    drive_service = get_drive_service()
    folder_id = get_research_folder_id(drive_service)
    media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype="text/markdown")
    drive_service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id",
    ).execute()

    return filename


def research_and_reply(chat_id: int, topic_nums: list[int], items: list[tuple[str, str]]):
    """Run in a background thread: research topics in parallel, then notify done."""
    errors: dict[int, str] = {}
    saved: list[str] = []
    lock = threading.Lock()

    def do_one(num: int):
        headline, summary = items[num - 1]
        try:
            filepath = research_topic(num, headline, summary)
            with lock:
                saved.append(f"Topic {num} → {filepath}")
        except Exception as e:
            with lock:
                errors[num] = str(e)

    threads = [threading.Thread(target=do_one, args=(n,)) for n in topic_nums]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        failed = ", ".join(f"topic {n}" for n in sorted(errors))
        send_message(chat_id, f"Research done. Failed: {failed}.")
    else:
        send_message(chat_id, f"Research complete. {len(saved)} topic(s) saved to Google Drive → {DRIVE_FOLDER_NAME}.")


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
            pending_digest[chat_id] = items
            reply = format_digest(items)
            send_message(chat_id, reply, parse_mode="HTML")
            if items:
                send_message(chat_id, "Reply with topic numbers (e.g. 2,5) to research them.")
        except Exception as e:
            send_message(chat_id, f"Error fetching digest: {e}")
        return jsonify({"status": "ok"}), 200

    if chat_id in pending_digest and is_topic_selection(text):
        items = pending_digest[chat_id]
        nums = parse_topic_nums(text)
        valid = [n for n in nums if 1 <= n <= len(items)]
        invalid = [n for n in nums if n not in valid]
        if not valid:
            send_message(chat_id, f"Please send valid topic numbers between 1 and {len(items)}.")
            return jsonify({"status": "ok"}), 200
        if invalid:
            send_message(chat_id, f"Ignoring out-of-range numbers: {', '.join(map(str, invalid))}.")
        send_message(chat_id, f"Researching topic(s) {', '.join(map(str, valid))}...")
        t = threading.Thread(target=research_and_reply, args=(chat_id, valid, items), daemon=True)
        t.start()
        return jsonify({"status": "ok"}), 200

    send_message(chat_id, "Send /digest to get the latest agentic coding news from your newsletters.")
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
