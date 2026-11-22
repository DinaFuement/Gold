"""Gold daily report - COMEX (US) + MCX (India), emailed twice a day (10 AM, 10 PM IST).

Runs on GitHub Actions. Needs 3 secrets: SMTP_USER, SMTP_PASS, MAIL_TO.

US   : COMEX gold futures (USD per ounce), from Yahoo Finance.
India: MCX gold last trade (Rs per 10 g), read from the 5paisa MCX page.
       If that page cannot be read, the script uses an ESTIMATE
       (COMEX x USD/INR + MCX premium) and says so clearly in the email.
"""
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

OZ_TO_GRAM = 31.1035          # grams in one troy ounce
K22 = 22 / 24                 # 22K is 22/24 of 24K
MCX_URL = "https://www.5paisa.com/commodity-trading/mcx-gold-price"
# Used only when the MCX page cannot be read: how much higher MCX trades than
# COMEX x USDINR (import duty etc). Change it in the workflow with MCX_PREMIUM_PCT.
DEFAULT_PREMIUM_PCT = 16.3


def pct(new, old):
    return (new - old) / old * 100


# =====================================================================
# STEP 1 - US: COMEX gold
# =====================================================================
def get_comex():
    h = yf.Ticker("GC=F").history(period="15d").dropna(subset=["Close"])
    last, prev = h.iloc[-1], h.iloc[-2]
    return {
        "last": float(last["Close"]),
        "prev": float(prev["Close"]),
        "high": float(last["High"]),
        "low": float(last["Low"]),
        "date": h.index[-1].strftime("%d %b %Y"),
        "closes": h["Close"],
    }


def get_usdinr():
    h = yf.Ticker("USDINR=X").history(period="10d")["Close"].dropna()
    return float(h.iloc[-1]), float(h.iloc[-2])


# =====================================================================
# STEP 2 - India: MCX gold last trade
# =====================================================================
def _num(text):
    return float(text.replace(",", ""))


def parse_mcx_page(html):
    """Reads last price, previous close, open, high, low from the 5paisa page.
    Returns a dict, or None if the page layout is not what we expect."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    start = text.find("Gold Price Today")
    if start < 0:
        return None
    text = text[start:]

    def after(label):
        m = re.search(label + r"(?:\s|<[^>]*>|\||\*|:)*₹?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                      text, flags=re.I)
        return _num(m.group(1)) if m else None

    m = re.search(r"₹\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)
    if not m:
        return None
    data = {
        "last": _num(m.group(1)),
        "prev": after("Previous Close"),
        "open": after("Open Price"),
        "high": after("Today.s High"),
        "low": after("Today.s Low"),
    }
    return data if data["last"] and data["prev"] else None


def get_mcx(comex_last, comex_prev):
    """Returns MCX numbers plus a label saying where they came from."""
    usdinr_now, usdinr_prev = get_usdinr()
    implied_now = comex_last * usdinr_now / OZ_TO_GRAM * 10
    implied_prev = comex_prev * usdinr_prev / OZ_TO_GRAM * 10

    try:
        r = requests.get(
            MCX_URL, timeout=25,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 Chrome/124 Safari/537.36"})
        r.raise_for_status()
        d = parse_mcx_page(r.text)
        # safety check: MCX should be in the same area as COMEX x USDINR
        if d and 0.8 * implied_now < d["last"] < 1.6 * implied_now \
                and 0.8 * d["last"] < d["prev"] < 1.2 * d["last"]:
            d["source"] = "MCX gold, nearest contract (via 5paisa)"
            d["estimated"] = False
            return d
        print("MCX page read, but numbers failed the safety check:", d)
    except Exception as e:
        print("MCX page not available:", type(e).__name__, e)

    premium = 1 + float(os.getenv("MCX_PREMIUM_PCT") or DEFAULT_PREMIUM_PCT) / 100
    return {
        "last": implied_now * premium, "prev": implied_prev * premium,
        "open": None, "high": None, "low": None,
        "source": "ESTIMATE (COMEX x USD/INR + MCX premium) - live MCX page unavailable",
        "estimated": True,
    }


# =====================================================================
# STEP 3 - simple view for tomorrow (momentum of COMEX)
# =====================================================================
def outlook(closes):
    today = pct(closes.iloc[-1], closes.iloc[-2])
    week = pct(closes.iloc[-1], closes.iloc[-6])
    above_avg = closes.iloc[-1] > closes.tail(5).mean()
    score = int(today > 0) + int(week > 0) + int(above_avg)
    if score == 3:
        view, buy, sell = "UP", "BUY SOON - price is rising", "GOOD TIME TO SELL"
    elif score == 0:
        view, buy, sell = "DOWN", "WAIT - price may get cheaper", "HOLD - price is low now"
    else:
        view, buy, sell = "SIDEWAYS", "NEUTRAL - no clear direction", "NEUTRAL"
    why = (f"COMEX moved {today:+.2f}% on the last trading day and {week:+.2f}% over "
           f"5 sessions, and sits {'above' if above_avg else 'below'} its 5-day average.")
    return view, buy, sell, why


# =====================================================================
# STEP 4 - write the email
# =====================================================================
def arrow(change):
    return f"{'▲' if change >= 0 else '▼'} {change:+.2f}%"


def indian(n):
    """123456 -> 1,23,456 (Indian digit grouping)."""
    s = str(abs(int(round(n))))
    if len(s) > 3:
        s = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]
    return s


def rs(v):
    return f"Rs {indian(v)}"


def srs(v):
    return f"{'+' if v >= 0 else '-'}{rs(v)}"


LINE = "=" * 46


def build_email():
    cx = get_comex()
    mx = get_mcx(cx["last"], cx["prev"])
    view, buy, sell, why = outlook(cx["closes"])

    cx_chg = cx["last"] - cx["prev"]
    mx_chg = mx["last"] - mx["prev"]
    cx_pct = pct(cx["last"], cx["prev"])
    mx_pct = pct(mx["last"], mx["prev"])

    def gram_row(name, k):
        p1 = mx["last"] / 10 * k
        c1 = mx_chg / 10 * k
        return f"   {name:<4} {rs(p1):>10} ({srs(c1)})     {rs(p1 * 8):>11} ({srs(c1 * 8)})\n"

    cx_range = f"${cx['low']:,.2f} - ${cx['high']:,.2f}"
    mx_range = (f"{rs(mx['low'])} - {rs(mx['high'])}" if mx["low"] and mx["high"] else "not available")
    when = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")

    body = (
        f"GOLD DAILY REPORT - {when}\n"
        f"{LINE}\n\n"
        f"1) US - COMEX gold (USD per ounce)\n"
        f"   Last trade : ${cx['last']:,.2f}   (as of {cx['date']})\n"
        f"   Previous   : ${cx['prev']:,.2f}\n"
        f"   Change     : {arrow(cx_pct)}  ({'+' if cx_chg >= 0 else '-'}${abs(cx_chg):,.2f})\n"
        f"   Day range  : {cx_range}\n\n"
        f"2) INDIA - MCX gold 24K (Rs per 10 gram)\n"
        f"   Source     : {mx['source']}\n"
        f"   Last trade : {rs(mx['last'])}\n"
        f"   Previous   : {rs(mx['prev'])}\n"
        f"   Change     : {arrow(mx_pct)}  ({srs(mx_chg)})\n"
        f"   Day range  : {mx_range}\n\n"
        f"3) INDIA price per gram (from the MCX figure above)\n"
        f"        {'1 gram (change)':<22}  {'8 gram (change)'}\n"
        + gram_row("24K", 1.0)
        + gram_row("22K", K22)
        + "   Shop price will be higher: add 3% GST and the jeweller's making charges.\n\n"
        f"4) VIEW FOR TOMORROW: {view}\n"
        f"   If you want to BUY : {buy}\n"
        f"   If you want to SELL: {sell}\n"
        f"   Why: {why}\n\n"
        "Momentum-based indicator only, not financial advice.\n"
    )
    if mx["estimated"]:
        body += ("\nNOTE: live MCX data could not be read this time, so the India numbers\n"
                 "are an estimate. Treat them as a guide to direction, not the exact MCX price.\n")

    subject = (f"Gold {datetime.now(ZoneInfo('Asia/Kolkata')):%d %b %I:%M %p} | "
               f"COMEX ${cx['last']:,.0f} {arrow(cx_pct)} | "
               f"MCX {rs(mx['last'])} {arrow(mx_pct)} | Tomorrow: {view}")
    return subject, body


# =====================================================================
# STEP 5 - send through Gmail
# =====================================================================
def send_email(subject, body):
    user = os.environ.get("SMTP_USER", "").strip()
    password = "".join(os.environ.get("SMTP_PASS", "").split())
    to = os.environ.get("MAIL_TO", "").strip()

    missing = [n for n, v in (("SMTP_USER", user), ("SMTP_PASS", password), ("MAIL_TO", to)) if not v]
    if missing:
        raise RuntimeError("These GitHub secrets are missing or empty: " + ", ".join(missing))
    if len(password) != 16:
        raise RuntimeError(f"SMTP_PASS has {len(password)} characters. A Gmail App Password has exactly 16.")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    except (smtplib.SMTPServerDisconnected, OSError) as e:
        print("Port 465 failed, trying 587:", e)
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls(context=context)
            s.login(user, password)
            s.send_message(msg)
    print("Email sent")


if __name__ == "__main__":
    try:
        subject, body = build_email()
    except Exception as e:
        print(f"::error::PRICE step failed - {type(e).__name__}: {e}")
        sys.exit(1)

    print(subject)
    print(body)

    try:
        send_email(subject, body)
    except Exception as e:
        print(f"::error::EMAIL step failed - {type(e).__name__}: {e}")
        sys.exit(1)
