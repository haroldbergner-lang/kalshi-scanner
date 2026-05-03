"""
Kalshi Morning Market Scanner
"""

import os
import json
import time
import base64
import smtplib
import requests
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

import anthropic

KALSHI_API_KEY      = os.environ["KALSHI_API_KEY"]
KALSHI_PRIVATE_KEY  = os.environ["KALSHI_PRIVATE_KEY"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER          = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO            = os.environ["EMAIL_TO"]

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

def get_private_key():
    key = KALSHI_PRIVATE_KEY
    if "\\n" in key:
        key = key.replace("\\n", "\n")
    return serialization.load_pem_private_key(key.encode(), password=None)

def make_auth_headers(method: str, path: str) -> dict:
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    message = f"{timestamp}{method}{path}".encode("utf-8")
    private_key = get_private_key()
    signature = private_key.sign(
        message,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
    }

def fetch_all_markets() -> list[dict]:
    markets: list[dict] = []
    cursor: str | None = None

    print("Fetching Kalshi markets...")
    for page in range(20):
        sign_path = "/trade-api/v2/markets"
        headers = make_auth_headers("GET", sign_path)
        params: dict = {"limit": 100, "status": "open"}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(f"{KALSHI_BASE}/markets", headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()

        batch = body.get("markets", [])
        markets.extend(batch)
        cursor = body.get("cursor")
        print(f"  Page {page + 1}: got {len(batch)} markets (total {len(markets)})")
        if not cursor or not batch:
            break

    print(f"Total open markets fetched: {len(markets)}")
    return markets

def prepare_for_claude(markets: list[dict]) -> list[dict]:
    cleaned = []
    for m in markets:
        volume = m.get("volume") or 0
        yes_bid = m.get("yes_bid") or 0
        title = (m.get("title") or "").strip()
        subtitle = (m.get("subtitle") or "").strip()
        category = (m.get("category") or "").lower()

        if not title:
            continue

        # Filter out sports and entertainment categories before sending to Claude
        skip_keywords = ["nba", "nfl", "nhl", "mlb", "nascar", "golf", "mma", "ufc",
                         "boxing", "soccer", "tennis", "parlay", "oscar", "emmy",
                         "grammy", "celebrity", "reality tv"]
        title_lower = title.lower()
        if any(kw in title_lower for kw in skip_keywords):
            continue
        if any(kw in category for kw in ["sports", "entertainment", "pop culture"]):
            continue

        cleaned.append({
            "ticker":    m.get("ticker", ""),
            "title":     title,
            "subtitle":  subtitle,
            "category":  m.get("category", ""),
            "volume":    volume,
            "yes_bid":   yes_bid,
            "close_time": m.get("close_time", ""),
        })

    cleaned.sort(key=lambda x: x["volume"])
    return cleaned[:300]

def score_markets(candidates: list[dict]) -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.datetime.now().strftime("%A, %B %d, %Y")

    system_prompt = """You are a prediction market analyst. Your job is to identify the 6 most interesting
non-sports, non-entertainment Kalshi markets for a trader focused on regulatory, legislative,
economic, and corporate events.

You MUST always return exactly 6 picks. Never return an empty list."""

    user_prompt = f"""Today is {today}.

Here are {len(candidates)} active Kalshi markets (sports and entertainment already filtered out).
Pick the 6 most interesting for a trader who wants edge in regulatory, legislative, FDA, 
economic data, or corporate action markets.

Markets:
{json.dumps(candidates, indent=2, default=str)}

You MUST return a JSON array with EXACTLY 6 items. No markdown, no backticks, just raw JSON.

Each object must have:
{{
  "ticker": "ticker string",
  "title": "market title",
  "score": 7,
  "reasoning": "2-3 sentences on why this is interesting",
  "edge_type": "Legislative | Regulatory | Corporate | Economic | Other",
  "yes_bid": 45,
  "volume": 1234,
  "news_hook": "4-6 word hook",
  "close_time": "ISO date or empty string"
}}"""

    print("Sending to Claude for analysis...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start == -1 or end == 0:
        print("Warning: Claude didn't return a valid JSON array")
        print(f"Raw response: {raw[:500]}")
        return []
    picks = json.loads(raw[start:end])
    print(f"Claude returned {len(picks)} picks")
    return picks

def build_email(picks: list[dict]) -> str:
    today_str = datetime.datetime.now().strftime("%A, %B %d, %Y")

    def score_color(s):
        if s >= 8: return "#15803d"
        if s >= 6: return "#b45309"
        return "#6b7280"

    def badge(s):
        if s >= 8: return "Strong signal"
        if s >= 6: return "Worth a look"
        return "Moderate"

    def badge_bg(s):
        if s >= 8: return "#dcfce7"
        if s >= 6: return "#fef3c7"
        return "#f3f4f6"

    def fmt_close(close_time):
        if not close_time: return "—"
        try:
            dt = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            return dt.strftime("%b %d, %Y")
        except Exception:
            return close_time[:10] if len(close_time) >= 10 else close_time

    cards = ""
    for p in picks:
        score   = int(p.get("score", 5))
        yes_bid = int(p.get("yes_bid", 50))
        volume  = int(p.get("volume", 0))
        ticker  = p.get("ticker", "")
        close   = fmt_close(p.get("close_time", ""))
        cards += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;gap:12px;">
            <p style="font-size:15px;font-weight:600;color:#111827;margin:0;flex:1;line-height:1.4;">{p.get("title","")}</p>
            <div style="text-align:right;flex-shrink:0;">
              <span style="font-size:22px;font-weight:700;color:{score_color(score)};">{score}/10</span><br>
              <span style="display:inline-block;font-size:11px;font-weight:600;color:{score_color(score)};
                           background:{badge_bg(score)};padding:2px 8px;border-radius:6px;margin-top:2px;">{badge(score)}</span>
            </div>
          </div>
          <p style="font-size:13px;color:#6b7280;line-height:1.6;margin:0 0 14px;">{p.get("reasoning","")}</p>
          <div style="border-top:1px solid #f3f4f6;padding-top:10px;">
            <table style="width:100%;border-collapse:collapse;font-size:12px;">
              <tr>
                <td style="color:#9ca3af;padding:3px 0;">Type</td>
                <td style="color:#374151;font-weight:600;text-align:right;">{p.get("edge_type","—")}</td>
                <td style="width:20px;"></td>
                <td style="color:#9ca3af;padding:3px 0;">Yes price</td>
                <td style="color:#374151;font-weight:600;text-align:right;">{yes_bid}¢</td>
              </tr>
              <tr>
                <td style="color:#9ca3af;padding:3px 0;">Volume</td>
                <td style="color:#374151;font-weight:600;text-align:right;">${volume:,}</td>
                <td></td>
                <td style="color:#9ca3af;padding:3px 0;">Closes</td>
                <td style="color:#374151;font-weight:600;text-align:right;">{close}</td>
              </tr>
              <tr>
                <td style="color:#9ca3af;padding:3px 0;">News hook</td>
                <td colspan="4" style="color:#374151;font-weight:600;text-align:right;">{p.get("news_hook","—")}</td>
              </tr>
            </table>
            <div style="margin-top:12px;">
              <a href="https://kalshi.com/markets/{ticker}"
                 style="display:inline-block;font-size:13px;color:#2563eb;text-decoration:none;
                        border:1px solid #bfdbfe;border-radius:8px;padding:6px 14px;">
                View on Kalshi →
              </a>
            </div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:620px;margin:0 auto;padding:32px 16px;">
    <div style="margin-bottom:28px;">
      <h1 style="font-size:26px;font-weight:700;color:#111827;margin:0 0 4px;">Kalshi Market Scanner</h1>
      <p style="font-size:14px;color:#9ca3af;margin:0;">{today_str} · Niche legislative &amp; regulatory picks</p>
    </div>
    {cards}
    <p style="font-size:11px;color:#d1d5db;text-align:center;margin-top:24px;line-height:1.6;">
      Powered by Claude + Kalshi API · Not financial advice · Do your own research before trading
    </p>
  </div>
</body>
</html>"""

def send_email(html: str, pick_count: int) -> None:
    subject = f"Kalshi Scan · {pick_count} niche picks · {datetime.datetime.now().strftime('%b %d')}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    print(f"Sending email to {EMAIL_TO}...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
    print("Email sent!")

def main() -> None:
    markets    = fetch_all_markets()
    candidates = prepare_for_claude(markets)
    print(f"Candidates after filtering: {len(candidates)}")
    picks      = score_markets(candidates)
    if not picks:
        print("No picks returned — skipping email.")
        return
    html = build_email(picks)
    send_email(html, len(picks))

if __name__ == "__main__":
    main()
