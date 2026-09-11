from flask import Flask, render_template, jsonify, request, session, redirect, url_for
import yfinance as yf
import pandas as pd
import numpy as np
import sqlite3
import threading
import time
import hashlib
import hmac
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import feedparser

app = Flask(__name__)

# Authentication/security. Set these in intesa.env.
app.secret_key = os.getenv("INTESA_DASHBOARD_SECRET", "") or os.urandom(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("INTESA_COOKIE_SECURE", "0") == "1"

TICKER = "ISP.MI"   # Intesa Sanpaolo, Borsa Italiana
COMPANY = "Intesa Sanpaolo S.p.A."
DB_PATH = Path("intesa_alerts.db")
CONFIG_PATH = Path("intesa_config.json")

def load_env_file():
    """Load simple KEY=VALUE pairs from intesa.env if present.
    This keeps desktop and Raspberry Pi configuration consistent without python-dotenv.
    Existing process environment variables always win.
    """
    env_path = Path("intesa.env")
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value and key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass

load_env_file()

# Re-read secret after intesa.env has been loaded.
app.secret_key = os.getenv("INTESA_DASHBOARD_SECRET") or app.secret_key

DEFAULT_CONFIG = {
    "monitor_enabled": False,
    "signal_interval_minutes": 15,
    "news_interval_minutes": 15,
    "buy_score_threshold": 80,
    "sell_score_threshold": 30,
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "daily_summary_hour": 8
}

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    env_map = {
        "INTESA_TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "INTESA_TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env, key in env_map.items():
        if os.getenv(env):
            cfg[key] = os.getenv(env)
    return cfg

def save_public_config(data):
    cfg = load_config()
    allowed = [
        "monitor_enabled", "signal_interval_minutes", "news_interval_minutes",
        "buy_score_threshold", "sell_score_threshold",
        "telegram_enabled", "daily_summary_hour"
    ]
    for k in allowed:
        if k in data:
            cfg[k] = data[k]
    # Telegram secrets are intentionally not saved from the browser.
    cfg.pop("telegram_bot_token", None)
    cfg.pop("telegram_chat_id", None)
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return load_config()

def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS sent_alerts(
        alert_key TEXT PRIMARY KEY,
        alert_type TEXT,
        title TEXT,
        created_at TEXT
    )""")
    con.commit()
    return con

def sent_before(key):
    con = db()
    row = con.execute("SELECT 1 FROM sent_alerts WHERE alert_key=?", (key,)).fetchone()
    con.close()
    return bool(row)

def mark_sent(key, kind, title):
    con = db()
    con.execute("INSERT OR IGNORE INTO sent_alerts VALUES(?,?,?,?)",
                (key, kind, title, datetime.now().isoformat()))
    con.commit()
    con.close()

def clean(df):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).title() for c in df.columns]
    keep = [c for c in ["Open","High","Low","Close","Volume"] if c in df.columns]
    df = df[keep].dropna(subset=["High","Low","Close"]).copy()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    return df

def download(interval, period):
    try:
        x = yf.download(
            TICKER, interval=interval, period=period, auto_adjust=False,
            prepost=False, progress=False, threads=False
        )
    except Exception as e:
        raise ValueError(f"Yahoo Finance-fel: {e}")
    x = clean(x)
    if x.empty:
        raise ValueError(f"Ingen {interval}-data hittades för {TICKER}.")
    return x

def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()

def rsi(series, n=14):
    d = series.diff()
    up = d.clip(lower=0)
    down = -d.clip(upper=0)
    rs = up.ewm(alpha=1/n, adjust=False).mean() / down.ewm(alpha=1/n, adjust=False).mean().replace(0, np.nan)
    return 100 - 100/(1+rs)

def atr_series(df, n=14):
    pc = df.Close.shift(1)
    tr = pd.concat([
        df.High-df.Low,
        (df.High-pc).abs(),
        (df.Low-pc).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def pivots(df, left=2, right=2):
    hs, ls = [], []
    h, l, idx = df.High.to_numpy(), df.Low.to_numpy(), df.index
    for i in range(left, len(df)-right):
        if h[i] == np.max(h[i-left:i+right+1]) and h[i] > np.max(h[i-left:i]):
            hs.append({"time": idx[i].isoformat(), "price": float(h[i])})
        if l[i] == np.min(l[i-left:i+right+1]) and l[i] < np.min(l[i-left:i]):
            ls.append({"time": idx[i].isoformat(), "price": float(l[i])})
    return hs, ls

def structure_from_pivots(hs, ls):
    if len(hs) < 2 or len(ls) < 2:
        return "Neutral"
    hh = hs[-1]["price"] > hs[-2]["price"]
    hl = ls[-1]["price"] > ls[-2]["price"]
    lh = hs[-1]["price"] < hs[-2]["price"]
    ll = ls[-1]["price"] < ls[-2]["price"]
    if hh and hl:
        return "Bullish"
    if lh and ll:
        return "Bearish"
    return "Neutral"

def previous_day_range(df15):
    dates = pd.DatetimeIndex(df15.index.normalize()).unique().sort_values()
    if len(dates) < 2:
        raise ValueError("För lite 15m-historik för föregående dags range.")
    last_date = dates[-1]
    # If latest bar is today, use previous trading date; otherwise latest available date.
    today = pd.Timestamp(datetime.now().date())
    completed = [d for d in dates if d.date() < today.date()]
    day = completed[-1] if completed else dates[-2]
    x = df15[df15.index.normalize() == day]
    return {
        "date": str(day.date()),
        "high": float(x.High.max()),
        "low": float(x.Low.min())
    }

def relative_volume(df, n=20):
    if "Volume" not in df.columns or len(df) < n+1:
        return None
    base = float(df.Volume.iloc[-n-1:-1].mean())
    return float(df.Volume.iloc[-1]/base) if base > 0 else None

def tight_base(df):
    if len(df) < 48:
        return False, None
    recent = df.tail(16)
    prior = df.iloc[-48:-16]
    rw = float(recent.High.max()-recent.Low.min())
    pw = float(prior.High.max()-prior.Low.min())
    return bool(pw > 0 and rw/pw <= 0.65), rw

def vcp(df):
    if len(df) < 60:
        return False, 0, []
    x = df.tail(60)
    parts = [x.iloc[:20], x.iloc[20:40], x.iloc[40:60]]
    widths = [float(p.High.max()-p.Low.min()) for p in parts]
    contractions = sum(widths[i] < widths[i-1]*0.88 for i in range(1, len(widths)))
    return contractions >= 2, contractions, widths

def timeframe_summary(df):
    hs, ls = pivots(df)
    close = float(df.Close.iloc[-1])
    e20 = float(ema(df.Close, 20).iloc[-1])
    e50 = float(ema(df.Close, 50).iloc[-1]) if len(df) >= 50 else None
    structure = structure_from_pivots(hs, ls)
    trend = "Neutral"
    if e50 is not None:
        if close > e20 > e50:
            trend = "Bullish"
        elif close < e20 < e50:
            trend = "Bearish"
    return {
        "close": close, "ema20": e20, "ema50": e50,
        "structure": structure, "trend": trend,
        "swing_high": hs[-1]["price"] if hs else None,
        "swing_low": ls[-1]["price"] if ls else None
    }

def analyse():
    df15 = download("15m", "60d")
    df1h = download("60m", "730d")
    dfd = download("1d", "5y")

    rg = previous_day_range(df15)
    hs15, ls15 = pivots(df15)
    last = float(df15.Close.iloc[-1])
    prev = float(df15.Close.iloc[-2])
    rh, rl = rg["high"], rg["low"]
    atr15 = float(atr_series(df15).iloc[-1])
    rv = relative_volume(df15)
    tb, base_width = tight_base(df15)
    is_vcp, contractions, vcp_widths = vcp(df15)

    tf15 = timeframe_summary(df15)
    tf1h = timeframe_summary(df1h)
    tfd = timeframe_summary(dfd)

    rsi15 = float(rsi(df15.Close).iloc[-1])
    rsi1h = float(rsi(df1h.Close).iloc[-1])
    rsid = float(rsi(dfd.Close).iloc[-1])

    macd = ema(df15.Close, 12) - ema(df15.Close, 26)
    signal_line = ema(macd, 9)
    macd_now = float(macd.iloc[-1])
    macd_signal = float(signal_line.iloc[-1])

    breakout = last > rh and prev <= rh
    rejection = prev > rh and last < rh
    breakdown = last < rl and prev >= rl
    reclaim = prev < rl and last > rl
    retest_high = last >= rh and last <= rh + 0.35*atr15
    retest_low = last <= rl and last >= rl - 0.35*atr15

    above = [x for x in hs15 if x["price"] > rh]
    below = [x for x in ls15 if x["price"] < rl]
    nearest_above = min((x["price"] for x in above if x["price"] > last), default=None)
    nearest_below = max((x["price"] for x in below if x["price"] < last), default=None)

    # Transparent Score V2 (0-100). This is still a heuristic model, not a
    # statistically validated probability. Each component has a fixed max.
    reasons = []
    dist_rh = (rh-last)/last*100
    dist_rl = (last-rl)/last*100

    # 30 pts: higher-timeframe trend
    trend_score = 0
    for label, tf, weight in [("Daily", tfd, 15), ("1H", tf1h, 15)]:
        if tf["trend"] == "Bullish":
            trend_score += weight
            reasons.append(f"{label} trend bullish (+{weight})")
        elif tf["trend"] == "Neutral":
            trend_score += weight // 2
        else:
            reasons.append(f"{label} trend bearish (0/{weight})")

    # 15 pts: 15m structure
    structure_score = 15 if tf15["structure"] == "Bullish" else 8 if tf15["structure"] == "Neutral" else 0
    if tf15["structure"] == "Bullish": reasons.append("15m bullish market structure (+15)")
    elif tf15["structure"] == "Bearish": reasons.append("15m bearish market structure (0/15)")

    # 25 pts: setup quality. Use the strongest confirmed setup, avoiding
    # double-counting every pattern into the score.
    setup_score = 0
    if breakout:
        setup_score = 25; reasons.append("15m breakout over Range High (+25)")
    elif retest_high and tf1h["trend"] == "Bullish":
        setup_score = 20; reasons.append("Retest of Range High (+20)")
    elif is_vcp and tb:
        setup_score = 18; reasons.append("VCP + Tight Base (+18)")
    elif is_vcp:
        setup_score = 12; reasons.append("VCP-like contraction (+12)")
    elif tb:
        setup_score = 9; reasons.append("Tight Base (+9)")
    elif 0 <= dist_rh <= 2:
        setup_score = 5; reasons.append("Price within 2% of Range High (+5)")

    if rejection:
        setup_score = max(0, setup_score - 10)
        reasons.append("Rejection under Range High (-10)")
    if breakdown:
        setup_score = max(0, setup_score - 15)
        reasons.append("Breakdown under Range Low (-15)")
    if reclaim:
        setup_score = min(25, setup_score + 5)
        reasons.append("Reclaim of Range Low (+5)")

    # 15 pts: momentum
    momentum_score = 0
    if 55 <= rsi15 <= 70:
        momentum_score += 8; reasons.append("15m RSI in healthy momentum zone (+8)")
    elif 45 <= rsi15 < 55 or 70 < rsi15 <= 75:
        momentum_score += 5
    elif 30 <= rsi15 < 45:
        momentum_score += 3
    elif rsi15 > 75:
        momentum_score += 1; reasons.append("15m RSI overbought (limited momentum score)")
    else:
        momentum_score += 2
    if macd_now > macd_signal:
        momentum_score += 7; reasons.append("15m MACD above signal (+7)")
    else:
        momentum_score += 2
    momentum_score = min(15, momentum_score)

    # 15 pts: volume
    volume_score = 3
    if rv is not None:
        if rv >= 1.5:
            volume_score = 15; reasons.append("Relative volume ≥ 1.5x (+15)")
        elif rv >= 1.2:
            volume_score = 10; reasons.append("Relative volume ≥ 1.2x (+10)")
        elif rv >= 1.0:
            volume_score = 7
    
    score = int(max(0, min(100, trend_score + structure_score + setup_score + momentum_score + volume_score)))
    score_components = {
        "trend": trend_score, "structure": structure_score, "setup": setup_score,
        "momentum": momentum_score, "volume": volume_score
    }

    cfg = load_config()
    buy_thr = int(cfg.get("buy_score_threshold", 80))
    sell_thr = int(cfg.get("sell_score_threshold", 30))

    # These are technical alerts, not automatic orders.
    buy_setup = (
        score >= buy_thr
        and tfd["trend"] == "Bullish"
        and tf1h["trend"] != "Bearish"
        and (breakout or retest_high or (tb and is_vcp))
    )
    sell_risk = (
        score <= sell_thr
        or (breakdown and tf1h["trend"] == "Bearish")
        or (rejection and tfd["trend"] == "Bearish")
    )

    alert = "NEUTRAL"
    if buy_setup:
        alert = "KÖPLÄGE / ADD SETUP"
    elif sell_risk:
        alert = "SÄLJ / RISK SETUP"
    elif score >= 70:
        alert = "BEVAKA"

    # Fundamental snapshot. Market multiples come from Yahoo when available;
    # key bank metrics are pinned to the latest official Intesa H1 2026 release.
    def normalize_ratio(value):
        try:
            if value is None:
                return None
            value = float(value)
            return value / 100.0 if abs(value) > 1 else value
        except (TypeError, ValueError):
            return None

    info = {}
    try:
        raw = yf.Ticker(TICKER).info or {}
        dividend_yield = normalize_ratio(raw.get("dividendYield"))
        roe = normalize_ratio(raw.get("returnOnEquity"))
        if dividend_yield is None:
            dividend_rate = raw.get("dividendRate")
            current_price = raw.get("currentPrice") or raw.get("regularMarketPrice")
            try:
                if dividend_rate is not None and current_price:
                    dividend_yield = float(dividend_rate) / float(current_price)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        info = {
            "market_cap": raw.get("marketCap"),
            "pe": raw.get("trailingPE"),
            "forward_pe": raw.get("forwardPE"),
            "price_to_book": raw.get("priceToBook"),
            "dividend_yield": dividend_yield,
            "roe": roe,
            "week52_high": raw.get("fiftyTwoWeekHigh"),
            "week52_low": raw.get("fiftyTwoWeekLow"),
            "currency": raw.get("currency", "EUR")
        }
    except Exception:
        pass

    official = {
        "as_of": "2026-06-30",
        "roe": 0.20,
        "rote": 0.25,
        "cet1": 0.131,
        "npl_net": 0.008,
        "net_income_h1": 5.554,
        "net_income_guidance": ">€10bn",
        "shareholder_return_2026": 9.4,
        "dividend_yield_guidance": 0.07,
        "source": "Intesa Sanpaolo H1 2026 results"
    }
    # Prefer the official bank figures for bank-specific metrics.
    info["official"] = official

    bars = [{
        "time": i.isoformat(),
        "open": float(r.Open), "high": float(r.High),
        "low": float(r.Low), "close": float(r.Close),
        "volume": int(float(r.Volume)) if "Volume" in df15.columns and pd.notna(r.Volume) else 0
    } for i, r in df15.tail(350).iterrows()]

    return {
        "company": COMPANY, "ticker": TICKER, "last": last,
        "last_time": df15.index[-1].isoformat(),
        "score": score, "score_components": score_components, "alert": alert, "buy_setup": buy_setup, "sell_risk": sell_risk,
        "reasons": reasons,
        "range": rg,
        "distance_range_high_pct": dist_rh,
        "distance_range_low_pct": dist_rl,
        "nearest_above": nearest_above, "nearest_below": nearest_below,
        "relative_volume": rv, "atr15": atr15,
        "rsi15": rsi15, "rsi1h": rsi1h, "rsid": rsid,
        "macd": macd_now, "macd_signal": macd_signal,
        "tight_base": tb, "base_width": base_width,
        "vcp": is_vcp, "vcp_contractions": contractions, "vcp_widths": vcp_widths,
        "patterns": {
            "breakout": breakout, "retest_high": retest_high, "rejection": rejection,
            "breakdown": breakdown, "retest_low": retest_low, "reclaim": reclaim
        },
        "timeframes": {"15m": tf15, "1h": tf1h, "daily": tfd},
        "fundamentals": info,
        "bars": bars,
        "swings": {"highs": hs15[-60:], "lows": ls15[-60:]}
    }

IMPORTANT_WORDS = [
    "results", "earnings", "guidance", "dividend", "buyback", "capital",
    "cet1", "acquisition", "takeover", "merger", "mps", "monte dei paschi",
    "ecb", "regulator", "consob", "tax", "profit", "loss", "ceo", "rating",
    "stress test", "investigation", "fine"
]

def get_news(limit=20):
    items = []
    # Google News RSS, queried from the user's own machine.
    url = "https://news.google.com/rss/search?q=" + quote_plus('"Intesa Sanpaolo"') + "&hl=en&gl=IT&ceid=IT:en"
    try:
        feed = feedparser.parse(url)
        for e in feed.entries[:limit]:
            title = getattr(e, "title", "")
            link = getattr(e, "link", "")
            published = getattr(e, "published", "")
            low = title.lower()
            important = any(w in low for w in IMPORTANT_WORDS)
            items.append({
                "title": title, "link": link, "published": published,
                "important": important
            })
    except Exception:
        pass
    return items

def send_telegram(text):
    cfg = load_config()
    if not cfg.get("telegram_enabled"):
        raise ValueError("Telegram alerts är inte aktiverade.")
    token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    if not token or not chat_id:
        raise ValueError("Telegram är aktiverat men bot token/chat ID saknas.")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true"
    }).encode()
    req = Request(url, data=payload, method="POST")
    with urlopen(req, timeout=20) as response:
        if response.status != 200:
            raise ValueError(f"Telegram HTTP {response.status}")
        return True

def signal_telegram(a):
    if a["buy_setup"]:
        emoji = "🟢"
    elif a["sell_risk"]:
        emoji = "🔴"
    else:
        emoji = "🟡"

    lines = [
        f"{emoji} INTESA {a['alert']}",
        f"Score: {a['score']}/100",
        f"Kurs: €{a['last']:.3f}",
        f"Daily: {a['timeframes']['daily']['trend']}",
        f"1H: {a['timeframes']['1h']['trend']}",
        f"15m: {a['timeframes']['15m']['structure']}",
        f"Range: €{a['range']['low']:.3f}–€{a['range']['high']:.3f}",
        f"VCP: {'YES' if a['vcp'] else 'NO'} | Tight Base: {'YES' if a['tight_base'] else 'NO'}",
        f"RelVol: {a['relative_volume']:.2f}x" if a["relative_volume"] is not None else "RelVol: —",
        f"Setup: {', '.join(k for k,v in a['patterns'].items() if v) or 'none'}"
    ]
    if a["reasons"]:
        lines.append("")
        lines.extend("• " + r for r in a["reasons"][:5])
    lines.append("")
    lines.append("Teknisk alert – ingen order läggs automatiskt.")
    send_telegram("\n".join(lines))

def news_telegram(item):
    # Kept as a compatibility name for the monitor, but sends Telegram only.
    title = item["title"].strip()
    # Google News RSS often appends the publisher after " - ".
    source = ""
    if " - " in title:
        title, source = title.rsplit(" - ", 1)
    summary = title[:220] + ("…" if len(title) > 220 else "")
    text = (
        "📰 INTESA VIKTIG NYHET\n\n"
        f"{summary}\n"
        + (f"Källa: {source}\n" if source else "")
        + (f"{item['published']}\n" if item.get("published") else "")
        + f"Källa/länk: {item['link']}"
    )
    send_telegram(text)

def daily_summary_telegram(a, news):
    important = [x for x in news if x.get("important")][:5]
    lines = [
        "📊 INTESA DAILY REPORT",
        f"Kurs: €{a['last']:.3f} | Score: {a['score']}/100",
        f"Core: {'HOLD CORE' if a['timeframes']['daily']['trend']=='Bullish' else 'REVIEW CORE'}",
        f"Swing: {'ADD SWING' if a['buy_setup'] else 'REDUCE SWING' if a['sell_risk'] else 'HOLD/WAIT'}",
        f"Daily: {a['timeframes']['daily']['trend']} | 1H: {a['timeframes']['1h']['trend']} | 15m: {a['timeframes']['15m']['structure']}",
        f"Range: €{a['range']['low']:.3f}–€{a['range']['high']:.3f}",
        f"VCP: {'YES' if a['vcp'] else 'NO'} | Tight Base: {'YES' if a['tight_base'] else 'NO'}"
    ]
    if important:
        lines.append("")
        lines.append("📰 Viktiga nyheter:")
        for x in important:
            lines.append("• " + x["title"][:180])
            lines.append("  " + x["link"])
    else:
        lines.append("")
        lines.append("📰 Inga viktiga nyheter flaggade.")
    send_telegram("\n".join(lines))


def monitor_once():
    cfg = load_config()
    if not cfg.get("monitor_enabled"):
        return

    try:
        a = analyse()
        if a["buy_setup"] or a["sell_risk"]:
            key_raw = f"signal|{datetime.now().date()}|{a['alert']}|{round(a['last'],2)}"
            key = hashlib.sha256(key_raw.encode()).hexdigest()
            if not sent_before(key):
                signal_telegram(a)
                mark_sent(key, "signal", a["alert"])
    except Exception as e:
        app.logger.exception("Signal monitor failed: %s", e)

    try:
        news = get_news(15)
        for item in news:
            if not item["important"]:
                continue
            key = hashlib.sha256(("news|" + item["title"]).encode()).hexdigest()
            if not sent_before(key):
                news_telegram(item)
                mark_sent(key, "news", item["title"])
        # Daily report once per local date, after configured hour.
        hour = int(cfg.get("daily_summary_hour", 8))
        now = datetime.now()
        daily_key = hashlib.sha256(
            f"daily|{now.date()}".encode()
        ).hexdigest()
        if now.hour >= hour and not sent_before(daily_key):
            daily_summary_telegram(a, news)
            mark_sent(daily_key, "daily", "Daily report")
    except Exception as e:
        app.logger.exception("News/daily monitor failed: %s", e)


_monitor_started = False
def monitor_loop():
    while True:
        cfg = load_config()
        mins = max(5, int(min(cfg.get("signal_interval_minutes", 15),
                              cfg.get("news_interval_minutes", 15))))
        try:
            monitor_once()
        except Exception:
            app.logger.exception("Monitor loop failed")
        time.sleep(mins * 60)

def start_monitor():
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response

@app.before_request
def same_origin_post_check():
    if request.method != "POST" or not request.path.startswith("/api/"):
        return None
    origin = request.headers.get("Origin")
    if origin:
        expected = f"{request.scheme}://{request.host}"
        if origin.rstrip("/") != expected.rstrip("/"):
            return jsonify({"error": "Otillåten origin."}), 403
    return None

def dashboard_password():
    return os.getenv("INTESA_DASHBOARD_PASSWORD", "").strip()

def is_authenticated():
    return bool(session.get("authenticated"))

def api_auth_required():
    if not dashboard_password():
        return jsonify({"error": "Dashboard-lösenord saknas. Sätt INTESA_DASHBOARD_PASSWORD i intesa.env."}), 503
    if not is_authenticated():
        return jsonify({"error": "Autentisering krävs."}), 401
    return None

@app.route("/login", methods=["GET", "POST"])
def login():
    if not dashboard_password():
        return render_template("login.html", setup_required=True), 503
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if supplied and hmac.compare_digest(
            hashlib.sha256(supplied.encode()).digest(),
            hashlib.sha256(dashboard_password().encode()).digest()
        ):
            session["authenticated"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Fel lösenord."), 401
    return render_template("login.html")

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def index():
    if not dashboard_password():
        return redirect(url_for("login"))
    if not is_authenticated():
        return redirect(url_for("login"))
    return render_template("index.html")

@app.get("/api/analyse")
def api_analyse():
    auth = api_auth_required()
    if auth: return auth
    try:
        return jsonify(analyse())
    except Exception as e:
        app.logger.exception("Analysis failed")
        return jsonify({"error": str(e)}), 400

@app.get("/api/news")
def api_news():
    auth = api_auth_required()
    if auth: return auth
    return jsonify(get_news(20))

@app.get("/api/config")
def api_config_get():
    auth = api_auth_required()
    if auth: return auth
    cfg = load_config()
    return jsonify({
        k: v for k, v in cfg.items()
        if k not in ("telegram_bot_token", "telegram_chat_id")
    } | {
        "telegram_configured": bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"))
    })

@app.post("/api/config")
def api_config_save():
    auth = api_auth_required()
    if auth: return auth
    try:
        cfg = save_public_config(request.get_json(force=True) or {})
        return jsonify({"ok": True, "config": {k:v for k,v in cfg.items() if k not in ("telegram_bot_token", "telegram_chat_id")}})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.post("/api/telegram/test")
def api_telegram_test():
    auth = api_auth_required()
    if auth: return auth
    try:
        send_telegram("🤖 INTESA DASHBOARD – testmeddelande")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.post("/api/monitor/run")
def api_monitor_run():
    auth = api_auth_required()
    if auth: return auth
    try:
        monitor_once()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    start_monitor()
    host = "0.0.0.0" if os.getenv("SERVER_MODE") == "1" else "127.0.0.1"
    app.run(debug=False, host=host, port=5000)
