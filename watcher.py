"""
RSS watcher – watches an RSS feed and reports new entries to a Discord webhook.

Each run:
1. Fetch and parse the feed (feedparser)
2. Load the entries already seen from seen.json
3. Filter out the new entries
4. Send a formatted message to the Discord webhook for each new entry
5. Persist the seen entries

All configuration comes from environment variables (see .env.example) so the
webhook URL never ends up in the code or on GitHub.

Usage:
    python watcher.py            # a single run (e.g. for a cron job / Task Scheduler)
    python watcher.py --loop     # loop forever, checking every POLL_INTERVAL seconds
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

# Load .env from the project folder (if present)
load_dotenv()

# --- Configuration from environment variables -------------------------------

# Required: target feed and Discord webhook
FEED_URL = os.getenv("FEED_URL", "").strip()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Optional: wait time between two runs in --loop mode (seconds)
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "900"))  # default: 15 minutes

# Optional: how many entries to report on the very first run.
# Prevents a full feed from triggering a flood of messages on first start.
MAX_FIRST_RUN = int(os.getenv("MAX_FIRST_RUN", "3"))

# File that stores the IDs of entries already seen
STATE_FILE = Path(__file__).with_name("seen.json")


# --- Persistence: seen entries ----------------------------------------------

def load_seen() -> set[str]:
    """Read the set of already reported entry IDs from seen.json."""
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] Could not read seen.json ({exc}) – starting with an empty list.")
        return set()


def save_seen(seen: set[str]) -> None:
    """Save the seen IDs. Capped at the last 500 so the file doesn't grow forever."""
    trimmed = list(seen)[-500:]
    payload = {"updated": datetime.now(timezone.utc).isoformat(), "seen": trimmed}
    STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# --- Feed processing --------------------------------------------------------

def entry_id(entry) -> str:
    """
    Return a stable ID for a feed entry.
    Prefers the GUID/ID provided by the feed, falls back to the link.
    """
    return getattr(entry, "id", None) or getattr(entry, "link", "") or getattr(entry, "title", "")


def clean_html(raw: str) -> str:
    """
    Turn HTML from a feed entry into readable plain text.
    Discord embeds don't render HTML, so we strip tags and restore
    HTML entities (e.g. &amp;).
    """
    # Turn block elements into line breaks so paragraphs are kept
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", raw)
    text = re.sub(r"(?i)</\s*(p|div|li|pre|h[1-6])\s*>", "\n", text)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Resolve entities (&amp; -> &, &#x27; -> ' etc.)
    text = html.unescape(text)
    # Collapse repeated blank lines / whitespace
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    # Neutralize Discord markdown at the start of a line (# -> heading, > -> quote)
    # so feed text isn't rendered as a huge heading.
    text = re.sub(r"(?m)^(\s*)([#>]+)", r"\1\\\2", text)
    return text.strip()


def build_discord_message(entry, feed_title: str) -> dict:
    """Build the JSON payload for a Discord webhook message (embed format)."""
    title = html.unescape(getattr(entry, "title", "Untitled"))
    link = getattr(entry, "link", "")
    summary = clean_html(getattr(entry, "summary", "") or "")

    # Trim the summary – Discord embeds have character limits
    if len(summary) > 300:
        summary = summary[:297] + "..."

    return {
        "username": "RSS Watcher",
        "embeds": [
            {
                "title": title[:256],
                "url": link,
                "description": summary,
                "color": 0x5865F2,  # Discord blurple
                "footer": {"text": f"Source: {feed_title}"},
            }
        ],
    }


def send_to_discord(payload: dict) -> bool:
    """Send a payload to the Discord webhook. Returns True on success."""
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        # Discord responds with 204 (No Content) on success
        if resp.status_code in (200, 204):
            return True
        print(f"[WARN] Discord responded with status {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.RequestException as exc:
        print(f"[ERROR] Sending to Discord failed: {exc}")
        return False


def check_feed_once() -> int:
    """
    Run a single check.
    Returns the number of newly reported entries.
    """
    print(f"[{datetime.now():%H:%M:%S}] Checking feed: {FEED_URL}")
    feed = feedparser.parse(FEED_URL)

    if feed.bozo:  # feedparser reports parse problems via the bozo flag
        print(f"[WARN] Feed may be malformed: {feed.get('bozo_exception')}")

    if not feed.entries:
        print("[INFO] No entries found in the feed.")
        return 0

    feed_title = feed.feed.get("title", "Unknown feed")
    seen = load_seen()
    first_run = len(seen) == 0

    # Determine new entries in feed order (newest first)
    new_entries = [e for e in feed.entries if entry_id(e) not in seen]

    if not new_entries:
        print("[INFO] No new entries.")
        return 0

    # On the very first run, don't send the whole feed
    to_send = new_entries[:MAX_FIRST_RUN] if first_run else new_entries
    if first_run:
        print(f"[INFO] First run – reporting only the newest {len(to_send)} of {len(new_entries)} entries.")

    sent = 0
    # Send in reverse order so the newest appears last (at the bottom of the chat)
    for entry in reversed(to_send):
        payload = build_discord_message(entry, feed_title)
        if send_to_discord(payload):
            sent += 1
            print(f"  -> sent: {getattr(entry, 'title', '')[:80]}")
            time.sleep(1)  # small pause to be gentle on Discord rate limits

    # Remember all new IDs (including the ones skipped on the first run)
    # so they aren't reported again on the next run.
    for entry in new_entries:
        seen.add(entry_id(entry))
    save_seen(seen)

    print(f"[OK] Reported {sent} new entries.")
    return sent


# --- Entry point ------------------------------------------------------------

def validate_config() -> None:
    """Abort with a clear message if required environment variables are missing."""
    missing = []
    if not FEED_URL:
        missing.append("FEED_URL")
    if not WEBHOOK_URL:
        missing.append("DISCORD_WEBHOOK_URL")
    if missing:
        print(f"[ERROR] Missing environment variables: {', '.join(missing)}")
        print("        Create a .env file (see .env.example) or set the variables in your environment.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="RSS watcher with Discord notifications")
    parser.add_argument(
        "--loop",
        action="store_true",
        help=f"Loop forever: check every {POLL_INTERVAL}s (instead of just once)",
    )
    args = parser.parse_args()

    validate_config()

    if args.loop:
        print(f"[START] Loop mode – interval {POLL_INTERVAL}s. Press Ctrl+C to stop.")
        try:
            while True:
                check_feed_once()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n[STOP] Stopped.")
    else:
        check_feed_once()


if __name__ == "__main__":
    main()
