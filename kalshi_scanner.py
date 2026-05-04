"""
Kalshi Morning Market Scanner v3
- Fetches series catalog (public) for category/frequency/tags metadata
- Fetches events (auto-excludes combo/parlay markets)
- Hard-filters by category + frequency
- Sends ALL surviving event titles to Claude for curation
- Claude picks 10-15 interesting markets for a morning digest email
"""

import os, json, time, base64, smtplib, datetime, pathlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

# ── Config ────────────────────────────────────────────────────────────────────
KALSHI_BASE        = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_KEY_ID      = os.environ["KALSHI_API_KEY"]
KALSHI_PRIVATE_KEY = os.environ["KALSHI_PRIVATE_KEY"]
ANTHROPIC_KEY      = os.environ["ANTHROPIC_API_KEY"].strip()
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_PASS         = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO           = os.environ["EMAIL_TO"]

# Hard-drop these categories entirely
DROP_CATEGORIES = {"Mentions", "Exotics", "Social"}

# Hard-drop these frequencies
DROP_FREQUENCIES_ALL = {"daily"}  # drops daily across ALL categories

# Drop weekly ONLY for Entertainment (Billboard, Netflix rankings, etc.)
DROP_WEEKLY_FOR = {"Entertainment"}

_pk = None
def _get_private_key():
    global _pk
    if _pk is None:
        key = KALSHI_PRIVATE_KEY
        if "\\n" in key:
            key = key.replace("\\n", "\n")
        _pk = serialization.load_pem_private_key(key.encode(), password=None)
    return _pk

def _auth_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path}".encode()
    sig = _get_private_key().sign(
        msg,
        asym_padding.PSS(mgf=asym_padding.MGF1(hashes.SHA256()),
                         salt_length=asym_padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


# ── Step 1: Build series lookup table (public endpoint, no auth) ──────────────

def fetch_series_lookup():
    """Returns {series_ticker: {category, frequency, tags}} for filtering."""
    print("Fetching series catalog (public)...")
    r = requests.get(f"{KALSHI_BASE}/series", timeout=60)
    r.raise_for_status()
    series_list = r.json().get("series", [])
    lookup = {}
    for s in series_list:
        lookup[s["ticker"]] = {
            "category":  s.get("category", ""),
            "frequency": s.get("frequency", ""),
            "tags":      s.get("tags") or [],
        }
    print(f"  Loaded {len(lookup)} series templates")
    return lookup


# ── Step 2: Fetch all open events (auto-excludes combos) ──────────────────────

def fetch_open_events():
    """Events endpoint auto-excludes multivariate (combo/parlay) markets."""
    events = []
    cursor = None
    print("Fetching open events...")
    for page in range(20):
        path = "/trade-api/v2/events"
        headers = _auth_headers("GET", path)
        params = {"limit": 200, "status": "open", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{KALSHI_BASE}/events", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        batch = body.get("events", [])
        events.extend(batch)
        cursor = body.get("cursor")
        print(f"  Page {page+1}: {len(batch)} events (total {len(events)})")
        if not cursor or not batch:
            break
    print(f"Total open events: {len(events)}")
    return events


# ── Step 3: Hard filter using series metadata ─────────────────────────────────

def hard_filter(events, series_lookup):
    kept = []
    dropped_cat = 0
    dropped_freq = 0

    for ev in events:
        title = (ev.get("title") or "").strip()
        if not title:
            continue

        series_ticker = ev.get("series_ticker", "")
        meta = series_lookup.get(series_ticker, {})
        category = meta.get("category", "") or ev.get("category", "")
        frequency = meta.get("frequency", "")
        tags = meta.get("tags", [])

        if category in DROP_CATEGORIES:
            dropped_cat += 1
            continue

        if frequency in DROP_FREQUENCIES_ALL:
            dropped_freq += 1
            continue

        if frequency == "weekly" and category in DROP_WEEKLY_FOR:
            dropped_freq += 1
            continue

        markets = ev.get("markets", [])
        market_summaries = []
        for m in markets:
            if m.get("status") not in ("active", "open", None):
                continue
            sub = m.get("yes_sub_title") or m.get("title") or ""
            market_summaries.append({
                "ticker": m.get("ticker", ""),
                "sub": sub.strip(),
                "yes_bid": m.get("yes_bid_dollars", ""),
                "volume": m.get("volume_fp", "0"),
                "close_time": m.get("close_time", ""),
            })

        if not market_summaries:
            continue

        kept.append({
            "event_ticker": ev.get("event_ticker", ""),
            "title": title,
            "subtitle": (ev.get("sub_title") or "").strip(),
            "category": category,
            "frequency": frequency,
            "tags": tags,
            "markets": market_summaries,
            "n_markets": len(market_summaries),
        })

    print(f"Hard filter: {len(events)} events -> {len(kept)} kept "
          f"(dropped {dropped_cat} by category, {dropped_freq} by frequency)")

    from collections import Counter
    cats = Counter(e["category"] for e in kept)
    print(f"Category breakdown: {dict(cats.most_common(15))}")

    return kept


# ── Step 4: Claude picks 10-15 interesting markets ────────────────────────────

SYSTEM_PROMPT = """You are a market surfacing tool for a prediction market trader.

Your job is to pick the 10-15 most interesting Kalshi events from the list you receive.
"Interesting" means: a person with the right industry knowledge, connections, or domain
expertise could have an informational edge on this market.

Examples of interesting markets:
- "Will credit card rates be capped in 2026?" -> someone at a bank would know
- "Will the FDA approve X for medical use?" -> someone in pharma would know
- "Assistant Secretary of Treasury confirmation" -> someone on the Hill would know
- "Will Perplexity acquire Chrome?" -> someone in tech M&A would know
- "ISM PMI report" -> a macro economist would know
- "Will Bill Belichick coach a UNC game?" -> weird structural sports question
- "RTX PRO 6000 monthly price" -> someone in GPU supply chain would know

Examples of NOT interesting:
- "NBA Northwest Division Winner" -> just a standard sports outcome
- "Billboard Top 200 #1" -> pure pop culture guessing
- "Oscars Best Picture" -> no domain edge possible
- "Will it rain in Houston tomorrow?" -> pure weather

PRIORITIZE newer markets (recently created) since they are less efficiently priced.

For each pick, write ONE sentence explaining what kind of person would have edge
on this market.

Return ONLY a JSON array with 10-15 items:
{
  "event_ticker": "...",
  "one_liner": "One sentence on who would have edge and why this market is interesting"
}"""

def ask_claude(events):
    lines = []
    for ev in events:
        tag_str = ", ".join(ev["tags"][:3]) if ev["tags"] else ""
        lines.append(
            f"[{ev['event_ticker']}] ({ev['category']}"
            f"{' / ' + tag_str if tag_str else ''}) "
            f"{ev['title']}"
            f"{' -- ' + ev['subtitle'] if ev['subtitle'] else ''}"
            f" ({ev['n_markets']} markets)"
        )

    user_msg = (
        f"Today is {datetime.datetime.utcnow().strftime('%A, %B %d, %Y')}.\n\n"
        f"Here are {len(lines)} open Kalshi events after pre-filtering. "
        f"Pick the 10-15 most interesting ones.\n\n"
        + "\n".join(lines)
        + "\n\nReturn JSON array only. No markdown."
    )

    print(f"Sending {len(lines)} event titles to Claude...")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}],
        },
        timeout=120,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        print(f"No JSON array in response. First 500 chars:\n{text[:500]}")
        return []

    try:
        picks = json.loads(text[start:end+1])
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        return []

    print(f"Claude returned {len(picks)} picks")
    return picks


# ── Step 5: Build email ───────────────────────────────────────────────────────

def build_email(picks, events_by_ticker):
    today = datetime.datetime.utcnow().strftime("%A, %B %d, %Y")
    cards = ""
    for p in picks:
        ticker = p.get("event_ticker", "")
        ev = events_by_ticker.get(ticker, {})
        one_liner = p.get("one_liner", "")
        category = ev.get("category", "")
        title = ev.get("title", ticker)
        subtitle = ev.get("subtitle", "")

        markets = ev.get("markets", [])
        market_info = ""
        if markets:
            m = markets[0]
            if m.get("close_time"):
                try:
                    dt = datetime.datetime.fromisoformat(
                        m["close_time"].replace("Z", "+00:00"))
                    market_info = f"Closes {dt.strftime('%b %d, %Y')}"
                except Exception:
                    pass

        link_ticker = ticker.lower()

        cards += f"""
<div style="border:1px solid #e2e8f0;border-radius:10px;padding:16px;margin-bottom:12px;background:white;">
  <div style="font-size:11px;color:#64748b;letter-spacing:0.04em;font-weight:600;margin-bottom:4px;">{category}</div>
  <div style="font-size:15px;font-weight:600;color:#0f172a;line-height:1.4;">{title}</div>
  {"<div style='font-size:13px;color:#475569;margin-top:2px;'>" + subtitle + "</div>" if subtitle else ""}
  <div style="font-size:13px;color:#334155;margin-top:8px;line-height:1.5;font-style:italic;">{one_liner}</div>
  <div style="margin-top:10px;font-size:12px;color:#64748b;">{market_info}</div>
  <div style="margin-top:8px;">
    <a href="https://kalshi.com/markets/{link_ticker}" style="font-size:13px;color:#3b82f6;text-decoration:none;">Open on Kalshi &rarr;</a>
  </div>
</div>"""

    return f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;background:#f8fafc;max-width:640px;margin:0 auto;padding:24px;">
<h1 style="font-size:22px;margin:0 0 4px;color:#0f172a;">Kalshi Morning Scan</h1>
<p style="color:#64748b;margin:0 0 20px;font-size:14px;">{today} &middot; {len(picks)} markets worth a look</p>
{cards}
<p style="font-size:11px;color:#cbd5e1;text-align:center;margin-top:20px;">Not financial advice &middot; Do your own research</p>
</body></html>"""


# ── Step 6: Send email ────────────────────────────────────────────────────────

def send_email(html, pick_count):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Kalshi Scan · {pick_count} markets · {datetime.datetime.utcnow().strftime('%b %d')}"
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.send_message(msg)
    print(f"Email sent to {EMAIL_TO}")


# ── Main ──────────────────────────────────────────────────────────────────────

SENT_FILE = pathlib.Path(__file__).parent / "sent_tickers.json"

def load_sent_tickers():
    """Load tickers sent in the last 7 days."""
    if not SENT_FILE.exists():
        return set()
    try:
        data = json.loads(SENT_FILE.read_text())
    except Exception:
        return set()
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    # Keep only entries from last 7 days
    recent = {t: d for t, d in data.items() if d > cutoff}
    return set(recent.keys())

def save_sent_tickers(new_tickers):
    """Merge new tickers with existing, prune older than 7 days."""
    try:
        data = json.loads(SENT_FILE.read_text()) if SENT_FILE.exists() else {}
    except Exception:
        data = {}
    now = datetime.datetime.utcnow().isoformat()
    for t in new_tickers:
        data[t] = now
    # Prune old entries
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    data = {t: d for t, d in data.items() if d > cutoff}
    SENT_FILE.write_text(json.dumps(data, indent=2))


def main():
    series_lookup = fetch_series_lookup()
    events = fetch_open_events()
    filtered = hard_filter(events, series_lookup)
    if not filtered:
        print("No events survived filtering.")
        return
    # Remove markets already emailed in last 7 days
    already_sent = load_sent_tickers()
    before = len(filtered)
    filtered = [e for e in filtered if e["event_ticker"] not in already_sent]
    print(f"Dedup: {before} -> {len(filtered)} events ({before - len(filtered)} already sent)")

    if not filtered:
        print("All events already sent recently — skipping.")
        return

    picks = ask_claude(filtered)
    if not picks:
        print("Claude returned no picks.")
        return
    events_by_ticker = {e["event_ticker"]: e for e in filtered}
    html = build_email(picks, events_by_ticker)
    send_email(html, len(picks))

    # Track what we sent so we don't repeat tomorrow
    sent_tickers = [p.get("event_ticker", "") for p in picks if p.get("event_ticker")]
    save_sent_tickers(sent_tickers)
    print(f"Saved {len(sent_tickers)} tickers to sent_tickers.json")


if __name__ == "__main__":
    main()
