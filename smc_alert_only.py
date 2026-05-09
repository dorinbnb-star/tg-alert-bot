"""
SMC Alert System — Scanner fara executie automata
Gaseste setup, trimite alerta TG, tu decizi.
"""

import os
import time
import requests
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("smc_alerts.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

MIN_RR = 1.8
SCAN_INTERVAL = 300
COOLDOWN_HOURS = 4
OHLC_DELAY = 3

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "UNIUSDT",
    "AAVEUSDT", "TIAUSDT", "WLDUSDT", "WIFUSDT"
]

COIN_MAP = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "UNIUSDT": "uniswap",
    "AAVEUSDT": "aave",
    "TIAUSDT": "celestia",
    "WLDUSDT": "worldcoin-wld",
    "WIFUSDT": "dogwifcoin"
}

sent_setups = {}


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials lipsa.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        logger.error(f"Eroare Telegram: {e}")


# ─────────────────────────────────────────────
# DATE DE PIATA — COINGECKO
# ─────────────────────────────────────────────

def get_ticker(symbol: str) -> dict:
    coin_id = COIN_MAP.get(symbol)
    if not coin_id:
        return {}
    try:
        r = requests.get(
            f"{COINGECKO_BASE_URL}/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true"
            },
            timeout=10
        )
        r.raise_for_status()
        data = r.json().get(coin_id, {})
        return {
            "lastPrice": str(data.get("usd", 0)),
            "price24hPcnt": str(data.get("usd_24h_change", 0) / 100),
        }
    except Exception as e:
        logger.error(f"Eroare ticker {symbol}: {e}")
        return {}


def get_ohlc_data(symbol: str):
    coin_id = COIN_MAP.get(symbol)
    if not coin_id:
        return []
    try:
        r = requests.get(
            f"{COINGECKO_BASE_URL}/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": "14"},
            timeout=20
        )
        r.raise_for_status()
        raw = r.json()
        ohlc = []
        for candle in raw:
            try:
                ohlc.append([
                    int(candle[0]),
                    float(candle[1]),
                    float(candle[2]),
                    float(candle[3]),
                    float(candle[4]),
                    0.0
                ])
            except (ValueError, IndexError):
                continue
        return ohlc
    except Exception as e:
        logger.error(f"Eroare ohlc {symbol}: {e}")
        return []


# ─────────────────────────────────────────────
# CALCULE SMC
# ─────────────────────────────────────────────

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def calculate_atr(ohlc, period=14):
    if len(ohlc) < period + 1:
        return 0
    trs = []
    for i in range(1, len(ohlc)):
        tr = max(
            ohlc[i][2] - ohlc[i][3],
            abs(ohlc[i][2] - ohlc[i-1][4]),
            abs(ohlc[i][3] - ohlc[i-1][4])
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


def detect_fvg(ohlc):
    for i in range(len(ohlc) - 1, 1, -1):
        if ohlc[i][2] < ohlc[i-2][3]:
            return {"found": True, "type": "BEARISH"}
        if ohlc[i][3] > ohlc[i-2][2]:
            return {"found": True, "type": "BULLISH"}
    return {"found": False}


def detect_sweep(ohlc, lookback=10):
    if len(ohlc) < lookback + 2:
        return {"found": False}
    recent = ohlc[-lookback-1:-1]
    last = ohlc[-1]
    swing_high = max(c[2] for c in recent)
    swing_low = min(c[3] for c in recent)
    if last[2] > swing_high and last[4] < swing_high:
        return {"found": True, "type": "SWEEP_HIGH"}
    if last[3] < swing_low and last[4] > swing_low:
        return {"found": True, "type": "SWEEP_LOW"}
    return {"found": False}


def detect_bos(ohlc):
    if len(ohlc) < 6:
        return {"found": False}
    closes = [c[4] for c in ohlc]
    prev_high = max(c[2] for c in ohlc[-6:-2])
    prev_low = min(c[3] for c in ohlc[-6:-2])
    if closes[-1] > prev_high:
        return {"found": True, "type": "BOS_BULLISH"}
    if closes[-1] < prev_low:
        return {"found": True, "type": "BOS_BEARISH"}
    return {"found": False}


# ─────────────────────────────────────────────
# ANALIZA COIN
# ─────────────────────────────────────────────

def analyze_coin(symbol: str):
    ticker = get_ticker(symbol)
    try:
        price = float(ticker.get("lastPrice", 0))
    except (ValueError, TypeError):
        return None
    if not price:
        return None

    ohlc = get_ohlc_data(symbol)
    if len(ohlc) < 20:
        return None

    closes = [c[4] for c in ohlc]
    ema50 = calculate_ema(closes, min(50, len(closes)))
    rsi = calculate_rsi(closes)
    atr = calculate_atr(ohlc)
    fvg = detect_fvg(ohlc)
    sweep = detect_sweep(ohlc)
    bos = detect_bos(ohlc)

    if ema50 == 0 or not fvg.get("found"):
        return None
    if not (bos.get("found") or sweep.get("found")):
        return None

    pct = (price - ema50) / ema50 * 100
    is_premium = pct > 1.5
    is_discount = pct < -1.5

    if not (is_premium or is_discount):
        return None

    if is_premium and (
        fvg.get("type") == "BEARISH" or
        sweep.get("type") == "SWEEP_HIGH" or
        bos.get("type") == "BOS_BEARISH"
    ):
        direction = "SHORT"
        sl = price + atr * 1.5
        tp1 = price - atr * 2
        tp2 = price - atr * 4

    elif is_discount and (
        fvg.get("type") == "BULLISH" or
        sweep.get("type") == "SWEEP_LOW" or
        bos.get("type") == "BOS_BULLISH"
    ):
        direction = "LONG"
        sl = price - atr * 1.5
        tp1 = price + atr * 2
        tp2 = price + atr * 4
    else:
        return None

    rr = round(abs(tp1 - price) / abs(sl - price), 2) if abs(sl - price) > 0 else 0
    if rr < MIN_RR:
        return None

    score = 0
    patterns = []
    if fvg.get("found"):
        score += 30
        patterns.append(f"FVG {fvg.get('type')}")
    if sweep.get("found"):
        score += 35
        patterns.append(f"Sweep {sweep.get('type')}")
    if bos.get("found"):
        score += 25
        patterns.append(f"BOS {bos.get('type')}")
    if direction == "SHORT" and rsi > 65:
        score += 10
        patterns.append(f"RSI {rsi:.0f} overbought")
    if direction == "LONG" and rsi < 35:
        score += 10
        patterns.append(f"RSI {rsi:.0f} oversold")

    if score < 45:
        return None

    return {
        "symbol": symbol.replace("USDT", ""),
        "full_symbol": symbol,
        "price": price,
        "direction": direction,
        "zone": "Premium" if is_premium else "Discount",
        "pct": round(pct, 1),
        "rsi": round(rsi, 1),
        "atr": round(atr, 4),
        "sl": round(sl, 4),
        "tp1": round(tp1, 4),
        "tp2": round(tp2, 4),
        "rr": rr,
        "patterns": patterns,
        "score": score
    }


# ─────────────────────────────────────────────
# FORMAT ALERTA TG
# ─────────────────────────────────────────────

def format_alert(setup: dict) -> str:
    emoji = "🔴" if setup["direction"] == "SHORT" else "🟢"
    zone_emoji = "🏔" if setup["zone"] == "Premium" else "🏕"
    patterns_str = " | ".join(setup["patterns"])

    return (
        f"{emoji} *SETUP SMC — {setup['symbol']}/USDT*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 Direcție: *{setup['direction']}*\n"
        f"{zone_emoji} Zonă: {setup['zone']} ({setup['pct']:+.1f}% vs EMA50)\n"
        f"📊 Pattern: {patterns_str}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🎯 Entry: *${setup['price']:,.4f}*\n"
        f"🛑 SL: ${setup['sl']:,.4f}\n"
        f"💰 TP1: ${setup['tp1']:,.4f}\n"
        f"💰 TP2: ${setup['tp2']:,.4f}\n"
        f"⚖️ R:R 1:{setup['rr']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"RSI: {setup['rsi']} | Score: {setup['score']}/100\n"
        f"⚠️ _Doar alertă — decizi tu intrarea_"
    )


# ─────────────────────────────────────────────
# SCANNER PRINCIPAL
# ─────────────────────────────────────────────

def scan_watchlist():
    global sent_setups

    now = datetime.utcnow()
    expired = [s for s, t in sent_setups.items() if now - t > timedelta(hours=COOLDOWN_HOURS)]
    for s in expired:
        del sent_setups[s]

    logger.info(f"Scanare watchlist: {len(WATCHLIST)} perechi")

    setups = []
    for symbol in WATCHLIST:
        base = symbol.replace("USDT", "")
        if base in sent_setups:
            logger.info(f"  {base} — cooldown activ, skip")
            continue

        logger.info(f"  Analizez {symbol}...")
        result = analyze_coin(symbol)
        if result:
            setups.append(result)
            logger.info(f"  Setup gasit: {base} {result['direction']} score={result['score']}")
        else:
            logger.info(f"  — {base}: niciun setup SMC valid")

        time.sleep(OHLC_DELAY)

    if not setups:
        logger.info("Niciun setup SMC valid in watchlist.")
        return

    setups.sort(key=lambda x: x["score"], reverse=True)
    for setup in setups[:2]:
        msg = format_alert(setup)
        send_telegram(msg)
        sent_setups[setup["symbol"]] = now
        logger.info(f"Alerta trimisa: {setup['symbol']} {setup['direction']}")
        time.sleep(1)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    logger.info("SMC Alert System pornit — fara executie automata.")
    logger.info(f"Watchlist: {', '.join(WATCHLIST)}")
    logger.info(f"Scanare la fiecare {SCAN_INTERVAL//60} minute | Cooldown {COOLDOWN_HOURS}h per simbol")

    send_telegram(
        "🤖 *SMC Alert System pornit*\n"
        f"Scanez {len(WATCHLIST)} perechi la fiecare {SCAN_INTERVAL//60} min.\n"
        "Vei primi alerta cand gasesc setup SMC valid."
    )

    while True:
        try:
            scan_watchlist()
        except Exception as e:
            logger.error(f"Eroare scanare: {e}")
        logger.info(f"Urmatoarea scanare in {SCAN_INTERVAL//60} minute...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()