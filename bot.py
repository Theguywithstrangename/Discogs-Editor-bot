import os
import re
import io
import requests
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChannelPostHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# -------------------- CONFIG --------------------
load_dotenv()  # will read env vars locally; Render uses its own env

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN")
USER_AGENT = os.getenv("USER_AGENT", "RadioAristoclesBot/1.0")
BASE_URL = os.getenv("BASE_URL")  # e.g. https://radio-aristocles-bot.onrender.com
PORT = int(os.getenv("PORT", "10000"))  # Render sets PORT automatically

if not TELEGRAM_BOT_TOKEN or not DISCOGS_TOKEN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and DISCOGS_TOKEN in environment.")

HEADERS = {
    "Authorization": f"Discogs token={DISCOGS_TOKEN}",
    "User-Agent": USER_AGENT,
}

RE_RELEASE = re.compile(r"discogs\.com/(?:.*?/)?release/(\d+)", re.I)
RE_MASTER = re.compile(r"discogs\.com/(?:.*?/)?master/(\d+)", re.I)


# -------------------- DISCogs helpers --------------------
def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_release(release_id):
    return get_json(f"https://api.discogs.com/releases/{release_id}")


def get_master(master_id):
    m = get_json(f"https://api.discogs.com/masters/{master_id}")
    rid = m.get("main_release") or m.get("main_release_id")
    if not rid:
        return None
    return get_release(rid)


def extract_roles(extra, keywords):
    if not extra:
        return []
    out = []
    for p in extra:
        role = (p.get("role") or "").lower()
        name = p.get("name") or ""
        if any(k in role for k in keywords):
            out.append(name)
    # de-dup preserving order
    return list(dict.fromkeys(out))


def hashtags(data):
    g = data.get("genres") or []
    s = data.get("styles") or []
    return " ".join(
        "#" + re.sub(r"[^A-Za-z0-9]+", "", x)
        for x in (g + s)
        if x.strip()
    )


def choose_track(data, user_text):
    if "|" not in user_text:
        return None
    hint = user_text.split("|", 1)[1].strip()
    tracks = [
        t for t in (data.get("tracklist") or [])
        if t.get("type_", "track") == "track"
    ]

    # | track:2
    m = re.match(r"track\s*:\s*(\d+)", hint, re.I)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(tracks):
            return tracks[idx].get("title")

    # | Some Title
    h = hint.lower()
    for t in tracks:
        if h in (t.get("title", "").lower()):
            return t.get("title")
    return None


def build_caption(data, user_text):
    artist = data.get("artists_sort") or ", ".join(
        a.get("name", "") for a in data.get("artists", [])
    )
    album = data.get("title") or ""
    year = str(data.get("year") or "").strip()
    track = choose_track(data, user_text)

    producers = extract_roles(data.get("extraartists"), ["producer"])
    engineers = extract_roles(
        data.get("extraartists"),
        ["engineer", "mix", "record", "master", "sound"],
    )

    lines = []
    lines.append(f"🎧 Artist : {artist}")
    if track:
        lines.append(f"🎵 Track Title : {track}")
    lines.append(f"💿 Album : {album}")
    if producers:
        lines.append("🎚 Producer : " + ", ".join(producers))
    if engineers:
        lines.append("🎛 Sound Engineer : " + ", ".join(engineers))
    if year:
        decade = ""
        if year.isdigit() and len(year) == 4:
            decade = f" #{year[2]}0s"
        lines.append(f"📅 Date : {year}{decade}")
    tags = hashtags(data)
    if tags:
        lines.append(tags)
    return "\n".join(lines)


# -------------------- Telegram handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a Discogs release/master URL. "
        "Optionally add `| track:2` or `| Track Title`."
    )


def extract_id(text):
    m = RE_RELEASE.search(text)
    if m:
        return "release", m.group(1)
    m = RE_MASTER.search(text)
    if m:
        return "master", m.group(1)
    return None, None


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (update.effective_message.text or "").strip()
    typ, did = extract_id(msg)
    if not did:
        await update.effective_message.reply_text(
            "Send a valid Discogs release/master URL."
        )
        return

    try:
        data = get_release(did) if typ == "release" else get_master(did)
        if not data:
            await update.effective_message.reply_text(
                "Couldn't get data from Discogs."
            )
            return
    except Exception as e:
        await update.effective_message.reply_text(f"Discogs error: {e}")
        return

    caption = build_caption(data, msg)

    img_list = data.get("images") or []
    img_url = img_list[0].get("uri") if img_list else None

    if img_url:
        try:
            img = requests.get(img_url, timeout=15).content
            await update.effective_message.reply_photo(
                photo=io.BytesIO(img),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        except Exception:
            pass

    await update.effective_message.reply_text(
        caption, parse_mode=ParseMode.MARKDOWN
    )


# -------------------- lifecycle --------------------
async def post_init(app):
    """Set webhook after app is built (needed for Render)."""
    if not BASE_URL:
        # If BASE_URL is not set yet, don't set webhook (local/dev run).
        return
    await app.bot.set_webhook(url=f"{BASE_URL}/{TELEGRAM_BOT_TOKEN}")


def main():
    # Use HTTPXRequest so we can adjust timeouts if needed
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .post_init(post_init)  # will set webhook when BASE_URL exists
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    application.add_handler(ChannelPostHandler(handle))  # optional

    # Webhook server for Render
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"{BASE_URL}/{TELEGRAM_BOT_TOKEN}" if BASE_URL else None,
    )


if __name__ == "__main__":
    main()
