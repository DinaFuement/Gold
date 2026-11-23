"""Gold daily report - COMEX (US) + MCX (India), emailed twice a day (10 AM, 10 PM IST).

Runs on GitHub Actions. Needs 3 secrets: SMTP_USER, SMTP_PASS, MAIL_TO.

US   : COMEX gold futures (USD per ounce), from Yahoo Finance.
India: MCX gold last trade (Rs per 10 g), read from the 5paisa MCX page.
       If that page cannot be read, the script uses an ESTIMATE
       (COMEX x USD/INR + MCX premium) and says so clearly in the email.
"""
import html as htmllib
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta
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

RETAIL_URL = "https://www.goodreturns.in/gold-rates/"
# Used only when the Goodreturns page cannot be read: retail rate is about this
# much above MCX (before GST and making charges). Change with RETAIL_PREMIUM_PCT.
DEFAULT_RETAIL_PREMIUM_PCT = 2.7
EXAMPLE_MAKING_PCT = 12   # making charge used in the shop-total example
GST_PCT = 3


def ist_now():
    """India time. A run delayed past midnight still counts as the previous evening."""
    n = datetime.now(ZoneInfo("Asia/Kolkata"))
    return n - timedelta(hours=1) if n.hour == 0 else n


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
        "day": h.index[-1].to_pydatetime(),
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


def parse_goodreturns(html):
    """Reads today's 1-gram retail rates (24K, 22K) from the Goodreturns page."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    sep = r"(?:\s|<[^>]*>|\|)*"
    row = re.compile(r"(?:^|>|\|)\s*1\s*" + sep + r"\u20b9\s*([0-9][0-9,]*)"
                     + sep + r"\u20b9\s*([0-9][0-9,]*)")
    for m0 in re.finditer(r"Today Gold Price Per Gram", text):
        m = row.search(text[m0.start(): m0.start() + 1500])
        if m:
            return {"k24": _num(m.group(1)), "k22": _num(m.group(2))}
    return None


def get_retail(mcx_last):
    """Retail (jeweller) rate per gram, before GST and making charges."""
    mcx_gram = mcx_last / 10
    try:
        r = requests.get(
            RETAIL_URL, timeout=25,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 Chrome/124 Safari/537.36"})
        r.raise_for_status()
        d = parse_goodreturns(r.text)
        # safety check: retail should be close to MCX, and 22K about 92% of 24K
        if d and 0.9 * mcx_gram < d["k24"] < 1.2 * mcx_gram \
                and 0.88 < d["k22"] / d["k24"] < 0.95:
            d["source"] = "Goodreturns (India average, updated daily)"
            d["estimated"] = False
            return d
        print("Goodreturns page read, but numbers failed the safety check:", d)
    except Exception as e:
        print("Goodreturns page not available:", type(e).__name__, e)

    prem = 1 + float(os.getenv("RETAIL_PREMIUM_PCT") or DEFAULT_RETAIL_PREMIUM_PCT) / 100
    k24 = mcx_gram * prem
    return {"k24": k24, "k22": k24 * K22,
            "source": "ESTIMATE (MCX + retail premium) - Goodreturns page unavailable",
            "estimated": True}


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
    vol = float(closes.pct_change().abs().tail(10).mean() * 100)   # typical daily move, %
    return view, buy, sell, why, vol


# =====================================================================
# STEP 4 - write the email
# =====================================================================
def arrow(change):
    return f"{'▲' if change >= 0 else '▼'} {change:+.2f}%"


def market_note(cx):
    """Returns (title, text) if the market is closed (weekend/holiday), else None."""
    now = ist_now()
    last_txt = cx["day"].strftime("%A, %d %b %Y")
    if now.weekday() >= 5:  # Saturday or Sunday
        return ("WEEKEND - MARKETS CLOSED",
                f"Prices below are the LAST TRADE of {last_txt}. New prices start on Monday.")
    gap = (now.date() - cx["day"].date()).days
    limit = 3 if now.weekday() == 0 else 1   # Monday may legitimately show Friday
    if gap > limit:
        return ("MARKET HOLIDAY - NO NEW TRADING",
                f"Prices below are the LAST TRADE of {last_txt}.")
    return None


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


def retail_section(rt):
    mult = (1 + EXAMPLE_MAKING_PCT / 100) * (1 + GST_PCT / 100)
    out = (
        "4) INDIA RETAIL rate (jeweller rate, before GST and making charges)\n"
        f"   Source: {rt['source']}\n"
        f"        {'1 gram':<14}{'8 gram'}\n"
    )
    for name, g1 in (("24K", rt["k24"]), ("22K", rt["k22"])):
        out += f"   {name:<4} {rs(g1):>10}   {rs(g1 * 8):>11}\n"
    out += (
        f"   Example shop total for 8 gram ({EXAMPLE_MAKING_PCT}% making + {GST_PCT}% GST):\n"
        f"   24K {rs(rt['k24'] * 8 * mult)}   |   22K {rs(rt['k22'] * 8 * mult)}\n"
        "   (making charges differ by shop - ask your jeweller)\n\n"
    )
    return out


# ---------- HTML email design (works on phone and web) ----------
GREEN, RED, AMBER = "#16a34a", "#dc2626", "#b45309"


def _e(text):
    return htmllib.escape(str(text))


def _rup(v):
    return "\u20b9" + indian(v)


def _srup(v):
    return ("+" if v >= 0 else "-") + _rup(v)


def _tone(change):
    return GREEN if change >= 0 else RED


def _chip(text, color):
    bg = "#dcfce7" if color == GREEN else "#fee2e2"
    return (f'<span style="display:inline-block;background:{bg};color:{color};font-weight:bold;'
            f'font-size:14px;padding:4px 12px;border-radius:14px">{text}</span>')


def _row(label, value):
    return ('<tr>'
            f'<td style="padding:7px 0;color:#6b7280;font-size:14px;border-top:1px solid #f1f5f9">{label}</td>'
            f'<td align="right" style="padding:7px 0;color:#111827;font-size:14px;font-weight:bold;'
            f'border-top:1px solid #f1f5f9">{value}</td></tr>')


def _card(title, inner, accent="#d4a017"):
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:#ffffff;border-radius:14px;margin:0 0 14px 0;border-top:4px solid {accent}">'
            '<tr><td style="padding:16px 18px">'
            f'<div style="font-size:12px;letter-spacing:1px;color:#6b7280;font-weight:bold;'
            f'margin-bottom:10px">{title}</div>{inner}</td></tr></table>')


def _note(text):
    return ('<div style="background:#fffbeb;border:1px solid #f59e0b;color:#92400e;font-size:13px;'
            f'padding:10px 12px;border-radius:10px;margin-top:10px">{_e(text)}</div>')


def _gram_table(rows):
    """rows: [(label, price_per_gram, change_per_gram_or_None)]"""
    head = ('<tr>'
            '<td style="font-size:12px;color:#6b7280;font-weight:bold;padding-bottom:6px">PURITY</td>'
            '<td align="right" style="font-size:12px;color:#6b7280;font-weight:bold;padding-bottom:6px">1 GRAM</td>'
            '<td align="right" style="font-size:12px;color:#6b7280;font-weight:bold;padding-bottom:6px">8 GRAM</td></tr>')
    body = ""
    for label, p1, c1 in rows:
        def cell(price, chg):
            sub = (f'<div style="font-size:13px;font-weight:bold;color:{_tone(chg)}">{_srup(chg)}</div>'
                   if chg is not None else "")
            return (f'<td align="right" style="padding:10px 0;border-top:1px solid #f1f5f9">'
                    f'<div style="font-size:17px;font-weight:bold;color:#111827">{_rup(price)}</div>{sub}</td>')
        body += (f'<tr><td style="padding:10px 0;border-top:1px solid #f1f5f9;font-size:17px;'
                 f'font-weight:bold;color:#b45309">{label}</td>'
                 + cell(p1, c1)
                 + cell(p1 * 8, None if c1 is None else c1 * 8) + '</tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{body}</table>'


def _move_card(r):
    """How much the price moved (today) and how much it may move (next day), in rupees."""
    sgn, vcol = r["sgn"], {"UP": GREEN, "DOWN": RED}.get(r["view"], AMBER)
    sym = {"-": "&minus;", "+": "+", "+/-": "&plusmn;"}[sgn]
    today_cap = "Last trade" if r["closed"] else "Today"
    head = ('<tr><td style="font-size:12px;color:#6b7280;font-weight:bold;padding-bottom:6px" width="28%"></td>'
            '<td align="right" style="font-size:12px;color:#6b7280;font-weight:bold;padding-bottom:6px">1 GRAM</td>'
            '<td align="right" style="font-size:12px;color:#6b7280;font-weight:bold;padding-bottom:6px">8 GRAM</td></tr>')

    def line(name, cap, a, b, color):
        return ('<tr><td style="padding:9px 0;border-top:1px solid #f1f5f9">'
                f'<div style="font-size:16px;font-weight:bold;color:#b45309">{name}</div>'
                f'<div style="font-size:11px;color:#6b7280">{cap}</div></td>'
                f'<td align="right" style="padding:9px 0;border-top:1px solid #f1f5f9;font-size:14px;'
                f'font-weight:bold;color:{color}">{a}</td>'
                f'<td align="right" style="padding:9px 0;border-top:1px solid #f1f5f9;font-size:14px;'
                f'font-weight:bold;color:{color}">{b}</td></tr>')

    def sw(pair):
        return f'{sym}{_rup(pair[0])} to {_rup(pair[1])}'

    swing_cap = f'{_e(r["short"])} swing (typical)'
    rows = (line("24K", today_cap, _srup(r["c24"]), _srup(r["c24"] * 8), _tone(r["c24"]))
            + line("22K", today_cap, _srup(r["c22"]), _srup(r["c22"] * 8), _tone(r["c22"]))
            + line("24K", swing_cap, sw(r["sw24"]), sw(r["sw24_8"]), vcol)
            + line("22K", swing_cap, sw(r["sw22"]), sw(r["sw22_8"]), vcol))
    inner = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{rows}</table>'
             '<div style="font-size:12px;color:#9ca3af;margin-top:8px">'
             f'Swing = typical daily move of the last 10 COMEX sessions (about &plusmn;{r["vol"]:.1f}%). '
             'It shows the likely SIZE of a move, not a promise of direction.</div>')
    return _card("RUPEE CHANGE &middot; moved and may move", inner, vcol)


def _range_card(r):
    """Morning: today's range so far. Evening / weekend: likely range for the next day."""
    rg = r["rng"]
    vcol = {"UP": GREEN, "DOWN": RED}.get(r["view"], AMBER)
    head = ('<tr><td width="22%"></td>'
            '<td align="right" style="font-size:12px;color:#6b7280;font-weight:bold;padding-bottom:6px">1 GRAM</td>'
            '<td align="right" style="font-size:12px;color:#6b7280;font-weight:bold;padding-bottom:6px">8 GRAM</td></tr>')

    def line(name, a, b):
        cell = ('<td align="right" style="padding:9px 0;border-top:1px solid #f1f5f9;font-size:13px;'
                'font-weight:bold;color:#111827">{}</td>')
        return (f'<tr><td style="padding:9px 0;border-top:1px solid #f1f5f9;font-size:16px;'
                f'font-weight:bold;color:#b45309">{name}</td>'
                + cell.format(f"{_rup(a[0])} &ndash; {_rup(a[1])}")
                + cell.format(f"{_rup(b[0])} &ndash; {_rup(b[1])}") + '</tr>')

    rows = line("24K", rg["g24"], rg["g24_8"]) + line("22K", rg["g22"], rg["g22_8"])
    inner = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{rows}</table>'
             f'<div style="font-size:12px;color:#9ca3af;margin-top:8px">{_e(rg["note"])}</div>')
    return _card(_e(rg["title"]), inner, vcol)


def to_html(r, note):
    """Builds the phone + web friendly HTML email from the report data."""
    view = r["view"]
    col = {"UP": GREEN, "DOWN": RED}.get(view, AMBER)
    bg = {"UP": "#f0fdf4", "DOWN": "#fef2f2"}.get(view, "#fffbeb")
    sym = {"UP": "\u25b2", "DOWN": "\u25bc"}.get(view, "\u25ac")
    cx, mx, rt = r["cx"], r["mx"], r["rt"]

    banner = ""
    if note:
        banner = ('<div style="background:#fff3cd;border:2px solid #e0a800;color:#7a4b00;padding:14px;'
                  'border-radius:12px;margin:0 0 14px 0;text-align:center">'
                  f'<div style="font-size:18px;font-weight:bold">{_e(note[0])}</div>'
                  f'<div style="font-size:15px;font-weight:bold;margin-top:4px">{_e(note[1])}</div></div>')

    t = mx["pct"]                       # today's MCX move decides the header colour
    tword = "UP" if t > 0.05 else "DOWN" if t < -0.05 else "FLAT"
    tsym = {"UP": "\u25b2", "DOWN": "\u25bc", "FLAT": "\u25ac"}[tword]
    hbg = {"UP": "#15803d", "DOWN": "#b91c1c", "FLAT": "#b45309"}[tword]
    hsub = {"UP": "#dcfce7", "DOWN": "#fee2e2", "FLAT": "#fef3c7"}[tword]
    top_label = ("LAST TRADE PRICE (MCX INDIA)" if r["closed"]
                 else "TODAY&#39;S FINAL CLOSE (MCX INDIA)" if r["final"]
                 else "TODAY&#39;S PRICE SO FAR (MCX INDIA)")
    vcol0 = {"UP": GREEN, "DOWN": RED}.get(view, AMBER)
    vsym0 = {"UP": "\u25b2", "DOWN": "\u25bc"}.get(view, "\u25ac")
    header = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
              f'style="background:{hbg};border-radius:14px;margin:0 0 14px 0"><tr>'
              '<td style="padding:18px">'
              f'<div style="font-size:14px;font-weight:bold;color:{hsub}">&#129689; Gold Daily Report</div>'
              f'<div style="font-size:12px;letter-spacing:1px;font-weight:bold;color:{hsub};margin-top:10px">'
              f'{top_label}</div>'
              f'<div style="font-size:34px;font-weight:bold;color:#ffffff;margin-top:2px">'
              f'{tsym} {tword} {abs(t):.2f}%</div>'
              f'<div style="font-size:14px;color:{hsub};margin-top:2px">'
              f'COMEX (US) {_e(arrow(cx["pct"]))}</div>'
              '<div style="margin-top:12px"><span style="display:inline-block;background:#ffffff;'
              f'color:{vcol0};font-weight:bold;font-size:14px;padding:6px 14px;border-radius:16px">'
              f'VIEW FOR {_e(r["label"])}: {vsym0} {_e(view)}</span></div>'
              f'<div style="font-size:12px;color:{hsub};margin-top:10px">{_e(r["when"])}</div>'
              '</td></tr></table>')
    preview = (f'{tsym} {tword} {abs(t):.2f}% today | 24K \u20b9{indian(r["g24"])}/g | '
               f'8g \u20b9{indian(r["g24"] * 8)} | {r["short"]}: {view}')
    preheader = ('<div style="display:none;max-height:0;overflow:hidden;font-size:1px;color:#f3f4f6;'
                 f'line-height:1px">{_e(preview)}</div>')

    hero = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{bg};border:2px solid {col};border-radius:14px;margin:0 0 14px 0">'
            '<tr><td align="center" style="padding:18px">'
            f'<div style="font-size:12px;letter-spacing:1px;color:#6b7280;font-weight:bold">VIEW FOR {_e(r["label"])}</div>'
            f'<div style="font-size:38px;font-weight:bold;color:{col};margin:6px 0">{sym} {_e(view)}</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px">'
            '<tr><td style="padding:8px 0;border-top:1px solid #e5e7eb;font-size:13px;color:#6b7280" width="38%">'
            'If you want to BUY</td>'
            f'<td style="padding:8px 0;border-top:1px solid #e5e7eb;font-size:15px;font-weight:bold;color:#111827">'
            f'{_e(r["buy"])}</td></tr>'
            '<tr><td style="padding:8px 0;border-top:1px solid #e5e7eb;font-size:13px;color:#6b7280">'
            'If you want to SELL</td>'
            f'<td style="padding:8px 0;border-top:1px solid #e5e7eb;font-size:15px;font-weight:bold;color:#111827">'
            f'{_e(r["sell"])}</td></tr></table>'
            f'<div style="font-size:13px;color:#4b5563;margin-top:8px;text-align:left">{_e(r["why"])}</div>'
            '</td></tr></table>')

    cx_inner = (
        f'<div style="font-size:32px;font-weight:bold;color:#111827">${cx["last"]:,.2f}'
        '<span style="font-size:14px;color:#6b7280;font-weight:normal"> per ounce</span></div>'
        f'<div style="margin:8px 0 10px 0">{_chip(arrow(cx["pct"]), _tone(cx["chg"]))}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + _row("Previous close", f'${cx["prev"]:,.2f}')
        + _row("Change", f'{"+" if cx["chg"] >= 0 else "-"}${abs(cx["chg"]):,.2f}')
        + _row("Day range", f'${cx["low"]:,.2f} - ${cx["high"]:,.2f}')
        + _row("Last trade date", _e(cx["date"]))
        + '</table>')
    cx_card = _card("&#127482;&#127480; US &middot; COMEX GOLD", cx_inner, "#2563eb")

    rng = (f'{_rup(mx["low"])} - {_rup(mx["high"])}' if mx["low"] and mx["high"] else "not available")
    mx_inner = (
        f'<div style="font-size:32px;font-weight:bold;color:#111827">{_rup(mx["last"])}'
        '<span style="font-size:14px;color:#6b7280;font-weight:normal"> per 10 gram (24K)</span></div>'
        f'<div style="margin:8px 0 10px 0">{_chip(arrow(mx["pct"]), _tone(mx["chg"]))}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + _row("Previous close", _rup(mx["prev"]))
        + _row("Change", _srup(mx["chg"]))
        + _row("Day range", rng)
        + '</table>'
        f'<div style="font-size:12px;color:#9ca3af;margin-top:8px">Source: {_e(mx["source"])}</div>')
    if mx["estimated"]:
        mx_inner += _note("Live MCX data could not be read, so this is an estimate. "
                          "Use it for direction, not the exact price.")
    mx_card = _card("&#127470;&#127475; INDIA &middot; MCX GOLD", mx_inner, "#f97316")

    mkt_card = _card("PRICE PER GRAM &middot; from MCX last trade",
                     _gram_table([("24K", r["g24"], r["c24"]), ("22K", r["g22"], r["c22"])]),
                     "#d4a017")

    mult_txt = f'{EXAMPLE_MAKING_PCT}% making + {GST_PCT}% GST'
    retail_inner = (
        _gram_table([("24K", rt["k24"], None), ("22K", rt["k22"], None)])
        + '<div style="background:#f8fafc;border-radius:10px;padding:12px;margin-top:12px">'
        f'<div style="font-size:12px;color:#6b7280;font-weight:bold">SHOP TOTAL FOR 8 GRAM ({_e(mult_txt)})</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px"><tr>'
        f'<td style="font-size:15px;color:#111827"><b style="color:#b45309">24K</b> {_rup(r["t24"])}</td>'
        f'<td align="right" style="font-size:15px;color:#111827"><b style="color:#b45309">22K</b> {_rup(r["t22"])}</td>'
        '</tr></table>'
        '<div style="font-size:12px;color:#9ca3af;margin-top:4px">Making charges differ by shop - ask your jeweller.</div>'
        '</div>'
        f'<div style="font-size:12px;color:#9ca3af;margin-top:8px">Rate before GST and making charges. '
        f'Source: {_e(rt["source"])}</div>')
    if rt["estimated"]:
        retail_inner += _note("The retail rate could not be read this time, so it is estimated from MCX. "
                              "Check your jeweller for the exact rate.")
    retail_card = _card("INDIA RETAIL &middot; jeweller rate", retail_inner, "#16a34a")

    footer = ('<div style="font-size:12px;color:#6b7280;text-align:center;padding:6px 8px 18px 8px;line-height:1.6">'
              'Momentum-based indicator only, not financial advice.<br>'
              'Check yourself: <a href="https://www.cmegroup.com" style="color:#2563eb">CME (COMEX)</a> &middot; '
              '<a href="https://www.mcxindia.com" style="color:#2563eb">MCX</a> &middot; '
              '<a href="https://www.goodreturns.in/gold-rates/" style="color:#2563eb">Goodreturns</a></div>')

    return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            '<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            '<td align="center" style="padding:12px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px"><tr><td>'
            + preheader + header + banner + hero + _move_card(r) + _range_card(r) + cx_card + mx_card + mkt_card + retail_card + footer
            + '</td></tr></table></td></tr></table></body></html>')


LINE = "=" * 46


def build_email():
    cx = get_comex()
    mx = get_mcx(cx["last"], cx["prev"])
    rt = get_retail(mx["last"])
    view, buy, sell, why, vol = outlook(cx["closes"])
    note = market_note(cx)

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
    ist = ist_now()
    final = ist.hour >= 23            # the 11:45 PM email: MCX has closed for the day
    when = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    if note or (ist.weekday() == 4 and ist.hour >= 15):
        label, short = "NEXT TRADING DAY", "Next day"
    elif ist.hour < 15:
        label, short = "TODAY", "Today"
    else:
        label, short = "TOMORROW", "Tomorrow"

    # typical swing in rupees (half to full typical daily move), rounded to nearest Rs 10
    def swing(price):
        hi = max(round(price * vol / 100, -1), 20)
        lo = max(round(price * vol / 100 * 0.5, -1), 10)
        return (min(lo, hi - 10), hi)

    sgn = "-" if view == "DOWN" else "+" if view == "UP" else "+/-"

    # price range: morning = today's real range so far (from MCX); evening/weekend = likely range next day
    def around(price):
        typ = max(round(price * vol / 100, -1), 20)
        return (price - typ, price + typ)

    has_day_range = bool(mx["low"] and mx["high"])
    p24_, p22_ = mx["last"] / 10, mx["last"] / 10 * K22
    if label == "TODAY" and has_day_range:
        lo, hi = mx["low"] / 10, mx["high"] / 10
        rng = {"g24": (lo, hi), "g24_8": (lo * 8, hi * 8),
               "g22": (lo * K22, hi * K22), "g22_8": (lo * K22 * 8, hi * K22 * 8),
               "title": "PRICE RANGE - TODAY SO FAR",
               "note": "Real low and high traded on MCX so far today (24K). The day is not over yet."}
    else:
        when_txt = {"TODAY": "today", "TOMORROW": "tomorrow"}.get(label, "the next trading day")
        rng = {"g24": around(p24_), "g24_8": around(p24_ * 8),
               "g22": around(p22_), "g22_8": around(p22_ * 8),
               "title": f"LIKELY PRICE RANGE - {label}",
               "note": (f"{'Final MCX close' if final else 'Last price'} plus or minus a typical daily move (about {vol:.1f}%), for {when_txt}. "
                        "An estimate of the size of the move, not a forecast.")}
    p24, p22 = mx["last"] / 10, mx["last"] / 10 * K22

    def swing_text(price):
        lo, hi = swing(price)
        return f"{sgn}{rs(lo)} to {rs(hi)}"

    body = (
        f"GOLD DAILY REPORT - {when}\n"
        f"PRICE TODAY (MCX): {'UP' if mx_pct > 0.05 else 'DOWN' if mx_pct < -0.05 else 'FLAT'} {arrow(mx_pct)}\n"
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
        + "\n"
        + retail_section(rt)
        + f"5) VIEW FOR {label}: {view}\n"
        f"   If you want to BUY : {buy}\n"
        f"   If you want to SELL: {sell}\n"
        f"   Why: {why}\n\n"
        f"6) RUPEE CHANGE AND PRICE RANGE\n"
        f"   Today (24K): {srs(mx_chg / 10)} per gram | {srs(mx_chg / 10 * 8)} per 8 gram\n"
        f"   Today (22K): {srs(mx_chg / 10 * K22)} per gram | {srs(mx_chg / 10 * K22 * 8)} per 8 gram\n"
        f"   Typical swing for {label}: about +-{vol:.1f}% (last 10 sessions)\n"
        f"   24K: {swing_text(p24)} per gram | {swing_text(p24 * 8)} per 8 gram\n"
        f"   22K: {swing_text(p22)} per gram | {swing_text(p22 * 8)} per 8 gram\n"
        "   (size of a likely move, not a promise of direction)\n"
        f"   {rng['title'].title()}:\n"
        f"   24K: {rs(rng['g24'][0])} - {rs(rng['g24'][1])} per gram | "
        f"{rs(rng['g24_8'][0])} - {rs(rng['g24_8'][1])} per 8 gram\n"
        f"   22K: {rs(rng['g22'][0])} - {rs(rng['g22'][1])} per gram | "
        f"{rs(rng['g22_8'][0])} - {rs(rng['g22_8'][1])} per 8 gram\n"
        f"   ({rng['note']})\n\n"
        "Momentum-based indicator only, not financial advice.\n"
    )
    if mx["estimated"]:
        body += ("\nNOTE: live MCX data could not be read this time, so the India numbers\n"
                 "are an estimate. Treat them as a guide to direction, not the exact MCX price.\n")

    if rt["estimated"]:
        body += ("\nNOTE: the retail rate could not be read from Goodreturns this time, so it\n"
                 "is estimated from MCX. Check your jeweller for the exact rate.\n")

    if note:
        body = f"*** {note[0]} ***\n{note[1]}\n\n" + body

    dot = "\U0001F7E2" if mx_pct > 0.05 else "\U0001F534" if mx_pct < -0.05 else "\U0001F7E1"
    word = "UP" if mx_pct > 0.05 else "DOWN" if mx_pct < -0.05 else "FLAT"
    tag = ((note[0].split(" - ")[0] + " | ") if note            # WEEKEND / MARKET HOLIDAY
           else "FINAL CLOSE | " if final else "")
    subject = (f"{dot} {tag}Gold {word} {arrow(mx_pct)} | MCX {rs(mx['last'])} | "
               f"COMEX ${cx['last']:,.0f} {arrow(cx_pct)} | {short}: {view}")
    mult = (1 + EXAMPLE_MAKING_PCT / 100) * (1 + GST_PCT / 100)
    report = {
        "when": when, "view": view, "buy": buy, "sell": sell, "why": why,
        "cx": {"last": cx["last"], "prev": cx["prev"], "chg": cx_chg, "pct": cx_pct,
               "low": cx["low"], "high": cx["high"], "date": cx["date"]},
        "mx": {"last": mx["last"], "prev": mx["prev"], "chg": mx_chg, "pct": mx_pct,
               "low": mx["low"], "high": mx["high"], "source": mx["source"],
               "estimated": mx["estimated"]},
        "g24": mx["last"] / 10, "c24": mx_chg / 10,
        "g22": mx["last"] / 10 * K22, "c22": mx_chg / 10 * K22,
        "rt": rt, "t24": rt["k24"] * 8 * mult, "t22": rt["k22"] * 8 * mult,
        "label": label, "short": short, "vol": vol, "closed": bool(note), "final": final, "sgn": sgn, "rng": rng,
        "sw24": swing(p24), "sw24_8": swing(p24 * 8), "sw22": swing(p22), "sw22_8": swing(p22 * 8),
    }
    return subject, body, to_html(report, note)


# =====================================================================
# STEP 5 - send through Gmail
# =====================================================================
def send_email(subject, body, html=None):
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
    if html:
        msg.add_alternative(html, subtype="html")

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
        subject, body, html = build_email()
    except Exception as e:
        print(f"::error::PRICE step failed - {type(e).__name__}: {e}")
        sys.exit(1)

    print(subject)
    print(body)

    try:
        send_email(subject, body, html)
    except Exception as e:
        print(f"::error::EMAIL step failed - {type(e).__name__}: {e}")
        sys.exit(1)
