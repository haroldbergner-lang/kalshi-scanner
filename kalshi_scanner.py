"""
Kalshi Morning Scanner — uses /events endpoint which auto-excludes combo/parlay markets.
Sends each event (with all sub-markets) to Claude for edge scoring using the Alex framework.
"""
import os, json, time, base64, smtplib, random
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ANTHROPIC_BASE = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

KALSHI_KEY_ID = os.environ["KALSHI_API_KEY"]
KALSHI_PRIVATE_KEY_PEM = os.environ["KALSHI_PRIVATE_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"].strip()
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

_private_key = serialization.load_pem_private_key(
    KALSHI_PRIVATE_KEY_PEM.encode(), password=None
)


def make_auth_headers(method, path):
    timestamp = str(int(time.time() * 1000))
    path_to_sign = path.split("?")[0]
    msg = (timestamp + method + path_to_sign).encode()
    signature = _private_key.sign(
        msg,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }


def fetch_open_events():
    """Events endpoint auto-excludes multivariate combos. with_nested_markets gives us prices."""
    events = []
    cursor = None
    print("Fetching Kalshi events (combos auto-excluded)...")
    for page in range(15):
        path = "/trade-api/v2/events"
        headers = make_auth_headers("GET", path)
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


def prepare_for_claude(events):
    """Each event = one entry with title + all sub-markets formatted for analysis."""
    formatted = []
    for ev in events:
        title = (ev.get("title") or "").strip()
        if not title:
            continue
        markets = ev.get("markets", [])
        if not markets:
            continue
        market_lines = []
        total_vol = 0.0
        for m in markets:
            sub = (m.get("yes_sub_title") or m.get("title") or "").strip()
            yes_bid = m.get("yes_bid_dollars") or "0"
            vol = float(m.get("volume_fp") or 0)
            total_vol += vol
            market_lines.append(f"  - {sub}: yes ${yes_bid} (vol {int(vol)})")
        formatted.append({
            "event_ticker": ev.get("event_ticker", ""),
            "title": title,
            "category": (ev.get("category") or "").strip(),
            "subtitle": (ev.get("sub_title") or "").strip(),
            "markets_text": "\n".join(market_lines),
            "n_markets": len(markets),
            "total_volume": total_vol,
        })
    random.shuffle(formatted)
    sample = formatted[:200]
    print(f"Total events with markets: {len(formatted)}, sampling {len(sample)}")
    print("Sample of events being analyzed:")
    for ev in sample[:15]:
        print(f"  [{ev['category']}] ({ev['n_markets']}m, vol {int(ev['total_volume'])}) {ev['title'][:80]}")
    return sample


SYSTEM_PROMPT = """You are an expert prediction-market analyst evaluating Kalshi events for edge.

You're hunting for mispriced markets using the Alex/Landtrader framework (Risk Takers Ep 149). There are FOUR edge types worth picking:

1. HIDDEN CONSTRAINT — market resolves on a technical/legal condition the crowd ignores. (Mercy rule kicks in. "Death of Ayatollah" doesn't count as "out of power" per the rules. Exact resolution wording.)

2. BEHAVIORAL OBSERVATION — public info the crowd hasn't priced. (Athlete tweets at 10am, so the 6-10hr bracket is impossible. Player has flu but it's only on local beat reporter Twitter.)

3. NARRATIVE vs REALITY — crowd prices the story, not the mechanics. (Dunk contest contestant priced at 15¢ because YouTube searches surfaced his more athletic brother.)

4. UNDER-THE-RADAR REGULATORY/LEGISLATIVE — niche policy and economic markets where domain knowledge gives edge. (FDA approvals, agency rulings, specific bills, monetary policy nuances.)

REJECT these aggressively:
- Generic "Team X wins game" sports markets (no edge unless a specific hidden constraint applies)
- Markets where the price clearly already reflects the obvious answer
- Award/season-long markets with no near-term resolution
- Anything where you can't articulate WHY the crowd is wrong

Be ruthless. Most events will not have edge — that's fine. Score 7+ only if you can articulate genuine mispricing.

Return ONLY a JSON array. Each pick:
{
  "event_ticker": "...",
  "score": 7-10,
  "edge_type": "hidden_constraint" | "behavioral" | "narrative_vs_reality" | "regulatory",
  "thesis": "2-3 sentences explaining what the crowd is missing",
  "best_market": "which sub-market and direction (yes/no)"
}

If nothing meets the bar, return []. Quality over quantity. Aim for 3-7 picks max."""


def score_with_claude(events):
    blocks = []
    for ev in events:
        blocks.append(
            f"=== {ev['event_ticker']} ===\n"
            f"Title: {ev['title']}\n"
            f"Category: {ev['category']}\n"
            f"Subtitle: {ev['subtitle']}\n"
            f"Total volume: {int(ev['total_volume'])}\n"
            f"Markets:\n{ev['markets_text']}"
        )
    user_msg = (
        f"Today is {datetime.utcnow().strftime('%Y-%m-%d')}.\n\n"
        "Evaluate these Kalshi events for edge using the four-edge framework. "
        "Be selective — most won't qualify.\n\n"
        "EVENTS:\n\n" + "\n\n".join(blocks) +
        "\n\nReturn your picks as a JSON array only. No prose before or after."
    )
    print("Sending to Claude...")
    r = requests.post(
        ANTHROPIC_BASE,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}],
        },
        timeout=180,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        print(f"No JSON array in response. First 500 chars: {text[:500]}")
        return []
    try:
        picks = json.loads(text[start:end+1])
    except json.JSONDecodeError as e:
        print(f"JSON parse failed: {e}")
        return []
    by_ticker = {ev["event_ticker"]: ev for ev in events}
    enriched = []
    for p in picks:
        t = p.get("event_ticker", "")
        if t in by_ticker:
            p["_event"] = by_ticker[t]
            enriched.append(p)
    print(f"Claude returned {len(enriched)} picks")
    return enriched


def format_email_html(picks):
    if not picks:
        return "<html><body><p>No picks today — nothing met the edge bar.</p></body></html>"
    rows = []
    for p in picks:
        ev = p.get("_event", {})
        score = p.get("score", 0)
        color = "#22c55e" if score >= 8 else "#eab308"
        edge_label = p.get("edge_type", "").replace("_", " ").upper()
        ticker = ev.get("event_ticker", "").lower()
        rows.append(f'''
<div style="border:1px solid #e2e8f0; border-radius:10px; padding:18px; margin-bottom:14px; background:white;">
  <div style="display:flex; justify-content:space-between; gap:12px;">
    <div style="flex:1;">
      <div style="font-size:11px; color:#64748b; letter-spacing:0.05em; font-weight:600;">{ev.get('category','')} · {edge_label}</div>
      <div style="font-size:16px; font-weight:600; margin-top:4px; color:#0f172a;">{ev.get('title','')}</div>
    </div>
    <div style="background:{color}; color:white; font-weight:700; padding:6px 12px; border-radius:8px; font-size:14px; height:fit-content;">{score}/10</div>
  </div>
  <div style="margin-top:12px; color:#334155; font-size:14px; line-height:1.55;">{p.get('thesis','')}</div>
  <div style="margin-top:12px; padding:10px 12px; background:#f1f5f9; border-radius:6px; font-size:13px; color:#475569;">
    <strong style="color:#0f172a;">Trade idea:</strong> {p.get('best_market','')}
  </div>
  <div style="margin-top:10px; font-size:12px;">
    <a href="https://kalshi.com/markets/{ticker}" style="color:#3b82f6; text-decoration:none;">Open on Kalshi →</a>
  </div>
</div>''')
    return f'''<html><body style="font-family:-apple-system,Segoe UI,sans-serif; background:#f8fafc; max-width:640px; margin:0 auto; padding:24px;">
<h1 style="font-size:22px; margin:0 0 4px 0; color:#0f172a;">Kalshi Morning Scan</h1>
<p style="color:#64748b; margin:0 0 20px 0; font-size:14px;">{datetime.utcnow().strftime('%A, %B %d, %Y')} · {len(picks)} picks</p>
{''.join(rows)}
</body></html>'''


def send_email(picks):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Kalshi Scan · {len(picks)} picks · {datetime.utcnow().strftime('%b %d')}"
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(format_email_html(picks), "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.send_message(msg)
    print(f"Sent email with {len(picks)} picks to {EMAIL_TO}")


def main():
    events = fetch_open_events()
    sample = prepare_for_claude(events)
    if not sample:
        print("No events to analyze")
        return
    picks = score_with_claude(sample)
    if not picks:
        print("No picks — skipping email")
        return
    send_email(picks)


if __name__ == "__main__":
    main()
