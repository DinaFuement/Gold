"""Gold alert - emails COMEX (US) and India gold prices, twice a day.

Runs on GitHub Actions. Needs 3 secrets: SMTP_USER, SMTP_PASS, MAIL_TO.
India price is CALCULATED from COMEX x USD/INR (no duty, GST or shop margin),
because MCX has no free price feed.
"""
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import yfinance as yf

OZ_TO_GRAM = 31.1035   # grams in one troy ounce
K22 = 22 / 24          # 22K is 22/24 of 24K


# ---------- Step 1: get prices ----------
def last_two_closes(ticker):
    """Returns (latest price, previous close)."""
    closes = yf.Ticker(ticker).history(period="10d")["Close"].dropna()
    return float(closes.iloc[-1]), float(closes.iloc[-2])


def pct(new, old):
    return (new - old) / old * 100


def inr_per_10g(usd_per_oz, usd_to_inr):
    return usd_per_oz * usd_to_inr / OZ_TO_GRAM * 10


# ---------- Step 2: simple outlook for tomorrow ----------
def outlook():
    closes = yf.Ticker("GC=F").history(period="15d")["Close"].dropna()
    today = pct(closes.iloc[-1], closes.iloc[-2])
    week = pct(closes.iloc[-1], closes.iloc[-6])
    above_avg = closes.iloc[-1] > closes.tail(5).mean()
    score = int(today > 0) + int(week > 0) + int(above_avg)
    if score == 3:
        view, status = "UP", "BUY: BUY SOON (price rising)  |  SELL: GOOD TIME TO SELL"
    elif score == 0:
        view, status = "DOWN", "BUY: WAIT (may get cheaper)  |  SELL: HOLD (price is low now)"
    else:
        view, status = "SIDEWAYS", "BUY: NEUTRAL  |  SELL: NEUTRAL"
    why = (f"Today {today:+.2f}%, last 5 sessions {week:+.2f}%, price is "
           f"{'above' if above_avg else 'below'} its 5-day average.")
    return view, status, why


# ---------- Step 3: build the email text ----------
def arrow(change):
    return f"{'▲' if change >= 0 else '▼'} {change:+.2f}%"


def rs(value):
    return f"Rs {abs(value):,.0f}"


def signed_rs(value):
    return f"{'+' if value >= 0 else '-'}{rs(value)}"


def karat_lines(name, factor, now10, prev10):
    """Price and rupee change for 1 gram and 8 gram."""
    p1 = now10 / 10 * factor
    c1 = (now10 - prev10) / 10 * factor
    return (f"{name}  1 gram: {rs(p1)} ({signed_rs(c1)})"
            f"   |   8 gram: {rs(p1 * 8)} ({signed_rs(c1 * 8)})\n")


def build_email():
    gold_now, gold_prev = last_two_closes("GC=F")
    fx_now, fx_prev = last_two_closes("USDINR=X")
    india_now = inr_per_10g(gold_now, fx_now)
    india_prev = inr_per_10g(gold_prev, fx_prev)
    comex_chg = pct(gold_now, gold_prev)
    india_chg = pct(india_now, india_prev)
    view, status, why = outlook()

    when = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    body = (
        f"Gold update - {when}\n\n"
        f"COMEX (US): ${gold_now:,.2f}/oz   {arrow(comex_chg)}\n"
        f"India (calculated): {arrow(india_chg)} vs yesterday's close\n\n"
        + karat_lines("24K", 1.0, india_now, india_prev)
        + karat_lines("22K", K22, india_now, india_prev)
        + f"\nStatus: {status}\n"
        f"Tomorrow's outlook: {view}\n"
        f"Why: {why}\n\n"
        "Momentum-based indicator only, not financial advice.\n"
        "India price = COMEX x USD/INR. It excludes duty, GST and jeweller margin,\n"
        "so it will not match the MCX screen or your local shop exactly.\n"
    )
    subject = f"Gold tomorrow: {view} | COMEX {arrow(comex_chg)} | India {arrow(india_chg)}"
    return subject, body


# ---------- Step 4: send the email through Gmail ----------
def send_email(subject, body):
    user = os.environ["SMTP_USER"].strip()
    password = "".join(os.environ["SMTP_PASS"].split())  # removes stray spaces/newlines
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ["MAIL_TO"].strip()
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
    subject, body = build_email()
    print(body)
    send_email(subject, body)

    
