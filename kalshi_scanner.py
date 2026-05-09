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

# For these categories, ONLY keep one_off frequency (drops regular games, state races)
# This keeps weird structural markets like "NFL Receiving Yards Record" or
# "Will any independent win a House or Senate race?"
ONLY_ONE_OFF_FOR = {"Sports", "Elections"}

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


        # Sports & Elections: only keep one_off (weird structural markets)
        if category in {"Sports", "Elections"} and frequency != "one_off":
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

SYSTEM_PROMPT = """You are a market surfacing tool for a Kalshi prediction market trader.

Pick the 10-15 WEIRDEST, most UNUSUAL, and most RESEARCHABLE markets from the list.
The trader is 24, works in finance, and wants markets where doing your homework gives
you an edge over people who are just vibing off headlines.

WHAT MAKES A MARKET INTERESTING:
- It makes you go "wait, that is a real market?" (aliens, pandemics, obscure stuff)
- The crowd is pricing narrative instead of fundamentals (a market gets bid up
  because it is in the news, but the news does not actually change the probability)
- Reading the actual bill text, FDA timeline, or resolution criteria gives you a view
  most traders do not have
- Weird structural markets where the resolution mechanism creates edge
- Policy/regulatory questions where following the space closely matters

WHAT IS NOT INTERESTING:
- Standard sports outcomes (division winners, championships)
- Generic financial price targets
- Boring state-level election races
- Markets with obvious answers already priced in

PRIORITIZE markets tagged [NEW] or [RECENT].

For each pick, write ONE sentence about what the market is and why it is
interesting/weird/researchable. Do NOT write who would have insider access.

Return ONLY a JSON array with 10-15 items:
{
  "event_ticker": "...",
  "one_liner": "What this market is and why it is interesting"
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


# ── Mentions "Mention Markets" Email ─────────────────────────────────────────────


def run_mentions():
    """Query each Mentions series directly to find all upcoming events."""
    import requests as req
    print("Fetching series catalog...")
    r = req.get(f"{KALSHI_BASE}/series", timeout=60)
    r.raise_for_status()
    all_series = r.json().get("series", [])
    mention_series = [s["ticker"] for s in all_series if s.get("category") == "Mentions"]
    print(f"Found {len(mention_series)} Mentions series")
    now = datetime.datetime.utcnow()
    cutoff = now + datetime.timedelta(days=7)
    mentions = []
    print("Querying events for each Mentions series...")
    for i, st in enumerate(mention_series):
        try:
            path = "/trade-api/v2/events"
            headers = _auth_headers("GET", path)
            params = {"series_ticker": st, "status": "open", "with_nested_markets": "true", "limit": 10}
            r = req.get(f"{KALSHI_BASE}/events", headers=headers, params=params, timeout=15)
            if r.status_code != 200:
                continue
            events = r.json().get("events", [])
            for ev in events:
                title = (ev.get("title") or "").strip()
                if not title:
                    continue
                # Use strike_date (when event actually happens) not close_time
                sd = ev.get("strike_date", "")
                if not sd:
                    # Fallback to earliest market close_time
                    for m in ev.get("markets", []):
                        sd = m.get("close_time", "")
                        if sd:
                            break
                if not sd:
                    continue
                try:
                    event_dt = datetime.datetime.fromisoformat(sd.replace("Z", "+00:00"))
                    event_naive = event_dt.replace(tzinfo=None)
                    days_away = (event_naive - now).days
                    if i < 20:
                        print(f"    {st}: {title[:50]} | date={sd[:10]} | days_away={days_away}")
                    if now <= event_naive <= cutoff:
                        mentions.append({"title": title, "subtitle": (ev.get("sub_title") or "").strip(), "event_ticker": ev.get("event_ticker", ""), "close_dt": event_naive})
                except Exception as e:
                    if i < 20:
                        print(f"    {st}: parse error: {e}")
                    continue
        except Exception:
            continue
        if (i + 1) % 50 == 0:
            print(f"  Checked {i+1}/{len(mention_series)} series, found {len(mentions)} events so far")
    print(f"Found {len(mentions)} Mentions events closing in next 7 days")
    if not mentions:
        print("No upcoming mentions events found.")
        return
    mentions.sort(key=lambda x: x["close_dt"])
    from collections import OrderedDict
    by_day = OrderedDict()
    for ev in mentions:
        day_key = ev["close_dt"].strftime("%A, %B %d")
        if day_key not in by_day:
            by_day[day_key] = []
        by_day[day_key].append(ev)
    cards = ""
    for day, day_events in by_day.items():
        cards += '<div style="margin-top:20px;margin-bottom:8px;"><div style="font-size:14px;font-weight:700;color:#0f172a;border-bottom:2px solid #e2e8f0;padding-bottom:6px;">' + day + '</div></div>'
        for ev in day_events:
            time_str = ev["close_dt"].strftime("%I:%M %p UTC")
            link = ev["event_ticker"].lower()
            sub_html = '<div style="font-size:13px;color:#475569;margin-top:2px;">' + ev["subtitle"] + '</div>' if ev["subtitle"] else ""
            cards += '<div style="border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin-bottom:10px;background:white;"><div style="font-size:11px;color:#64748b;font-weight:600;">' + time_str + '</div><div style="font-size:15px;font-weight:600;color:#0f172a;margin-top:2px;">' + ev["title"] + '</div>' + sub_html + '<div style="margin-top:8px;"><a href="https://kalshi.com/markets/' + link + '" style="font-size:13px;color:#3b82f6;text-decoration:none;">Open on Kalshi &rarr;</a></div></div>'
    today_str = now.strftime("%A, %B %d, %Y")
    html = '<html><body style="font-family:-apple-system,Segoe UI,sans-serif;background:#f8fafc;max-width:640px;margin:0 auto;padding:24px;"><h1 style="font-size:22px;margin:0 0 4px;color:#0f172a;">Mention Markets This Week</h1><p style="color:#64748b;margin:0 0 20px;font-size:14px;">' + today_str + ' &middot; ' + str(len(mentions)) + ' upcoming events</p>' + cards + '<p style="font-size:11px;color:#cbd5e1;text-align:center;margin-top:20px;">Not financial advice &middot; Do your own research</p></body></html>'
    from email.mime.text import MIMEText as MT2
    from email.mime.multipart import MIMEMultipart as MM2
    msg = MM2("alternative")
    msg["Subject"] = f"Mention Markets - {len(mentions)} events - {now.strftime(chr(37)+chr(98)+chr(32)+chr(37)+chr(100))}"
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MT2(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.send_message(msg)
    print(f"Mentions email sent to {EMAIL_TO}")


import sys
if __name__ == "__main__":
    if "--mentions" in sys.argv:
        run_mentions()
    else:
        main()
