import os
import re
import json
import uuid
import logging
from flask import Flask, request, jsonify
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

SIGNAL_FILE = "/tmp/mt5_signal.json"
LOG_FILE = "/tmp/mt5_log.json"

def save_signal(signal):
    with open(SIGNAL_FILE, "w") as f:
        json.dump(signal, f)

def load_signal():
    try:
        with open(SIGNAL_FILE, "r") as f:
            return json.load(f)
    except:
        return {"id": "none", "pair": "XAUUSD", "direction": "none"}

def log_event(event):
    try:
        try:
            with open(LOG_FILE, "r") as f:
                logs = json.load(f)
        except:
            logs = []
        logs.append({"time": datetime.utcnow().isoformat(), **event})
        logs = logs[-50:]
        with open(LOG_FILE, "w") as f:
            json.dump(logs, f)
    except Exception as e:
        logger.error(f"Log error: {e}")

def parse_signal(text):
    text = text.strip()

    # ── Direction ──────────────────────────────
    direction = None
    if re.search(r'\bsell\b', text, re.IGNORECASE):
        direction = "SELL"
    elif re.search(r'\bbuy\b', text, re.IGNORECASE):
        direction = "BUY"
    if not direction:
        return None

    # ── Entry ──────────────────────────────────
    # Look for entry range on the FIRST line only (avoids TP4 Open confusion)
    first_lines = "\n".join(text.splitlines()[:3])
    range_match = re.search(
        r'(4[0-9]{2,3}(?:\.[0-9]+)?)\s*[-–]\s*(4[0-9]{2,3}(?:\.[0-9]+)?)',
        first_lines
    )
    if range_match:
        p1 = float(range_match.group(1))
        p2 = float(range_match.group(2))
        entry = max(p1, p2) if direction == "BUY" else min(p1, p2)
    else:
        entry_match = re.search(
            r'(?:buy|sell|now|@|entry|limit)\s*[:\s]?\s*(4[0-9]{2,3}(?:\.[0-9]+)?)',
            first_lines, re.IGNORECASE
        )
        if entry_match:
            entry = float(entry_match.group(1))
        else:
            prices = re.findall(r'\b4[0-9]{2,3}(?:\.[0-9]+)?\b', first_lines)
            entry = float(prices[0]) if prices else None

    if not entry:
        return None

    # ── TPs — skip TP4+ and "Open" lines ──────
    tps = []
    for m in re.finditer(
        r'\btp\s*([1-3])\s*[:\s]?\s*(4[0-9]{2,3}(?:\.[0-9]+)?)',
        text, re.IGNORECASE
    ):
        tps.append(float(m.group(2)))

    # If no numbered TPs found, grab any TP values
    if not tps:
        for m in re.finditer(
            r'\btp\s*[:\s]?\s*(4[0-9]{2,3}(?:\.[0-9]+)?)',
            text, re.IGNORECASE
        ):
            tps.append(float(m.group(1)))

    # ── SL ─────────────────────────────────────
    sl_match = re.search(
        r'(?:sl|stop\s*loss|stoploss)[:\s🚫☹️]*\s*(4[0-9]{2,3}(?:\.[0-9]+)?)',
        text, re.IGNORECASE
    )
    sl = float(sl_match.group(1)) if sl_match else None

    if not sl or not tps:
        return None

    # Validate direction makes sense with prices
    # For BUY: TPs should be above entry, SL below
    # For SELL: TPs should be below entry, SL above
    if direction == "BUY" and sl > entry:
        logger.warning(f"BUY signal but SL {sl} > entry {entry} — skipping")
        return None
    if direction == "SELL" and sl < entry:
        logger.warning(f"SELL signal but SL {sl} < entry {entry} — skipping")
        return None

    # Pad TPs to 3
    while len(tps) < 3:
        tps.append(tps[-1])

    return {
        "id": str(uuid.uuid4())[:8],
        "pair": "XAUUSD",
        "direction": direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tps[0], 2),
        "tp2": round(tps[1], 2),
        "tp3": round(tps[2], 2),
    }

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/mt5_signal", methods=["GET"])
def get_signal():
    signal = load_signal()
    logger.info(f"MT5 polled: {signal.get('id')} {signal.get('direction')}")
    if signal.get("direction") not in (None, "none"):
        save_signal({"id": "none", "pair": "XAUUSD", "direction": "none"})
        logger.info("Signal cleared after serving to MT5")
    return jsonify(signal)

@app.route("/mt5_close", methods=["POST"])
def close_signal():
    data = request.json or {}
    logger.info(f"MT5 close: {data}")
    log_event({"type": "MT5_CLOSE", **data})
    return jsonify({"status": "ok"})

@app.route("/new_signal", methods=["POST"])
def new_signal():
    data = request.json or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "no text"}), 400
    signal = parse_signal(text)
    if not signal:
        logger.warning(f"Could not parse signal: {text[:80]}")
        return jsonify({"error": "could not parse"}), 422
    save_signal(signal)
    log_event({"type": "NEW_SIGNAL", **signal})
    logger.info(f"✅ Signal saved: {signal}")
    return jsonify({"status": "ok", "signal": signal})

@app.route("/clear_signal", methods=["POST"])
def clear_signal():
    save_signal({"id": "none", "pair": "XAUUSD", "direction": "none"})
    return jsonify({"status": "cleared"})

@app.route("/status", methods=["GET"])
def status():
    signal = load_signal()
    try:
        with open(LOG_FILE, "r") as f:
            logs = json.load(f)
    except:
        logs = []
    return jsonify({"current_signal": signal, "recent_logs": logs[-10:]})

@app.route("/test-buy")
def test_buy():
    signal = {"id": "test01", "pair": "XAUUSD", "direction": "BUY",
              "entry": 4340.00, "sl": 4325.00, "tp1": 4350.00, "tp2": 4360.00, "tp3": 4370.00}
    save_signal(signal)
    return jsonify({"status": "test BUY saved", "signal": signal})

@app.route("/test-sell")
def test_sell():
    signal = {"id": "test02", "pair": "XAUUSD", "direction": "SELL",
              "entry": 4350.00, "sl": 4365.00, "tp1": 4340.00, "tp2": 4330.00, "tp3": 4320.00}
    save_signal(signal)
    return jsonify({"status": "test SELL saved", "signal": signal})

@app.route("/")
def home():
    return jsonify({"status": "RayGoldSignals running ✅"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
