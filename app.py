import os
import re
import json
import uuid
import random
import logging
import threading
import requests
from io import BytesIO
from datetime import datetime
from flask import Flask, request, jsonify

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
BUNDLED_BOLD_FONT = os.path.join(FONTS_DIR, "DejaVuSans-Bold.ttf")

APP_DIR = os.path.dirname(os.path.abspath(__file__))

def find_font(size=54):
    candidates = [
        os.path.join(APP_DIR, "fonts", "DejaVuSans-Bold.ttf"),
        os.path.join(APP_DIR, "DejaVuSans-Bold.ttf"),  # root of repo
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                logger.info(f"Using font: {path}")
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    logger.error("NO FONT FOUND")
    return ImageFont.load_default()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SIGNAL_FILE = "/tmp/mt5_signal.json"
LOG_FILE = "/tmp/mt5_log.json"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
VIP_CHANNEL = os.environ.get("VIP_CHANNEL", "-1004347840465")
WHATSAPP_URL = os.environ.get("WHATSAPP_URL", "https://web-production-6cec8d.up.railway.app")
CHART_IMG_KEY = os.environ.get("CHART_IMG_KEY", "GhKjWUCZA61Lx0OwoNZvp8AhcLtTkWee702zMySE")
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

tp_close_lock = threading.Lock()
tp_close_recent = {}

# ─────────────────────────────────────────────
# SIGNAL STORAGE
# ─────────────────────────────────────────────

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

# ─────────────────────────────────────────────
# SIGNAL PARSER
# ─────────────────────────────────────────────

def parse_signal(text):
    text = text.strip().replace('`', '')

    # Direction
    direction = None
    if re.search(r'\bsell\b', text, re.IGNORECASE):
        direction = "SELL"
    elif re.search(r'\bbuy\b', text, re.IGNORECASE):
        direction = "BUY"
    if not direction:
        return None

    # Entry — look in first 5 lines
    first_lines = "\n".join(text.splitlines()[:5])
    range_match = re.search(
        r'([3-9][0-9]{2,3}(?:\.[0-9]+)?)\s*[-–]\s*([3-9][0-9]{2,3}(?:\.[0-9]+)?)',
        first_lines
    )
    if range_match:
        p1 = float(range_match.group(1))
        p2 = float(range_match.group(2))
        entry = max(p1, p2) if direction == "BUY" else min(p1, p2)
    else:
        prices = re.findall(r'\b[3-9][0-9]{2,3}(?:\.[0-9]+)?\b', first_lines)
        entry = float(prices[0]) if prices else None

    if not entry:
        return None

    # TPs — handle ✅ TP1 4013.00 format
    tps = []
    for m in re.finditer(r'TP\s*(\d+)\s+([3-9][0-9]{2,3}(?:\.[0-9]+)?)', text, re.IGNORECASE):
        tp_num = int(m.group(1))
        if tp_num <= 3:
            tps.append(float(m.group(2)))

    if not tps:
        for m in re.finditer(r'TP\s*[:\s]\s*([3-9][0-9]{2,3}(?:\.[0-9]+)?)', text, re.IGNORECASE):
            tps.append(float(m.group(1)))

    # SL — handle 🛑 SL 4025.00 format
    sl_match = re.search(r'SL\s+([3-9][0-9]{2,3}(?:\.[0-9]+)?)', text, re.IGNORECASE)
    sl = float(sl_match.group(1)) if sl_match else None

    if not sl or not tps:
        logger.warning(f"Missing SL or TPs — sl={sl} tps={tps}")
        return None

    if direction == "BUY" and sl > entry:
        return None
    if direction == "SELL" and sl < entry:
        return None

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
# CHART IMAGE
# ─────────────────────────────────────────────

def get_chart_image():
    try:
        url = (
            f"https://api.chart-img.com/v1/tradingview/advanced-chart"
            f"?symbol=OANDA:XAUUSD&interval=5m&theme=dark"
            f"&key={CHART_IMG_KEY}"
        )
        r = requests.get(url, timeout=15)
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            logger.info("✅ Chart image fetched")
            return r.content
        logger.warning(f"Chart fetch failed: {r.status_code}")
    except Exception as e:
        logger.error(f"Chart error: {e}")
    return None

# ─────────────────────────────────────────────
# PROFIT CARD GENERATOR
# ─────────────────────────────────────────────

def find_font(size=54):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except:
            continue
    return ImageFont.load_default()

def generate_profit_card(close_type, profit_gbp, chart_bytes=None):
    if not PIL_AVAILABLE:
        return None
    try:
        W, H = 800, 600
        img = Image.new("RGB", (W, H), (5, 5, 10))
        draw = ImageDraw.Draw(img)

        bw = 18
        draw.rectangle([0, 0, W, bw], fill=(212, 175, 55))
        draw.rectangle([0, H-bw, W, H], fill=(212, 175, 55))
        draw.rectangle([0, 0, bw, H], fill=(212, 175, 55))
        draw.rectangle([W-bw, 0, W, H], fill=(212, 175, 55))

        tp_labels = {
            "TP1": "TP1 SMASHED",
            "TP2": "TP2 SMASHED",
            "TP3": "ALL TARGETS HIT",
            "TP4": "TP4 SMASHED",
            "TP5": "TP5 FULL SEND",
        }
        tp_label   = tp_labels.get(close_type, f"{close_type} HIT")
        profit_str = f"+£{profit_gbp:,.2f}"
        tagline    = "PREMIUM TRADE"

        f_label   = find_font(60)
        f_profit  = find_font(150)
        f_tagline = find_font(48)

        bbox = draw.textbbox((0, 0), tp_label, font=f_label)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, 40), tp_label, font=f_label, fill=(255, 255, 255))

        draw.rectangle([40, 120, W-40, 124], fill=(212, 175, 55))

        bbox = draw.textbbox((0, 0), profit_str, font=f_profit)
        tw = bbox[2] - bbox[0]
        px = (W - tw) // 2
        draw.text((px + 4, 184), profit_str, font=f_profit, fill=(0, 60, 0))
        draw.text((px, 180), profit_str, font=f_profit, fill=(0, 238, 80))

        draw.rectangle([40, 450, W-40, 454], fill=(212, 175, 55))

        bbox = draw.textbbox((0, 0), tagline, font=f_tagline)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, 480), tagline, font=f_tagline, fill=(212, 175, 55))

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        buf.seek(0)
        result = buf.read()
        logger.info(f"Profit card generated: {len(result)} bytes")
        return result

    except Exception as e:
        logger.error(f"Profit card error: {e}", exc_info=True)
        return None

# ─────────────────────────────────────────────
# TELEGRAM + WHATSAPP SENDERS
# ─────────────────────────────────────────────

def send_photo_telegram(chat_id, photo_bytes, caption):
    try:
        files = {"photo": ("chart.jpg", photo_bytes, "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        r = requests.post(f"{TELEGRAM_URL}/sendPhoto", files=files, data=data, timeout=15)
        result = r.json()
        if result.get("ok"):
            logger.info(f"✅ Photo sent to Telegram {chat_id}")
            return True
        logger.error(f"Telegram sendPhoto failed: {result}")
    except Exception as e:
        logger.error(f"Telegram photo error: {e}")
    return False

def send_text_telegram(chat_id, text):
    try:
        r = requests.post(f"{TELEGRAM_URL}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        return r.json().get("ok", False)
    except Exception as e:
        logger.error(f"Telegram text error: {e}")
    return False

def send_to_whatsapp_group(message, group, image_bytes=None):
    try:
        if image_bytes:
            import base64
            payload = {
                "message": message,
                "group": group,
                "image_url": None
            }
            # Send image as bytes via multipart would need index.js update
            # For now send text only to WhatsApp, chart via Telegram only
            payload = {"message": message, "group": group}
        else:
            payload = {"message": message, "group": group}

        r = requests.post(f"{WHATSAPP_URL}/send", json=payload, timeout=10)
        if r.status_code == 200:
            logger.info(f"✅ WhatsApp sent to {group}")
            return True
        logger.warning(f"WhatsApp failed: {r.status_code}")
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")
    return False

# ─────────────────────────────────────────────
# TP PROFIT AMOUNTS (GBP)
# ─────────────────────────────────────────────

TP_PROFIT_RANGES = {
    "TP1": (110, 165),
    "TP2": (170, 240),
    "TP3": (280, 420),
}

TP_TEXT = {
    "TP1": (
        "<b>✅ TP1 HIT!\n"
        "XAU/USD | GOLD</b>\n\n"
        "Close the trade or move SL to entry 🔒\n\n"
        "<i>This is the power of Kevin's Gold VIP 💎</i>"
    ),
    "TP2": (
        "<b>💥 TP2 HIT!\n"
        "XAU/USD | GOLD</b>\n\n"
        "Secure partials and hold for more 🎯\n\n"
        "<i>This is the power of Kevin's Gold VIP 💎</i>"
    ),
    "TP3": (
        "<b>🔥🔥 TP3 DESTROYED!\n"
        "XAU/USD | GOLD</b>\n\n"
        "What a trade! Close all positions 👑\n\n"
        "<i>This is the power of Kevin's Gold VIP 💎</i>"
    ),
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
def mt5_close():
    try:
        data = request.json or {}
        close_type = data.get("close_type", "")
        pair = data.get("pair", "XAUUSD")
        profit = float(data.get("profit", 0))

        logger.info(f"MT5 close: {pair} {close_type} profit={profit}")

        if close_type not in ("TP1", "TP2", "TP3", "TP4", "TP5", "SL"):
            return jsonify({"status": "ignored"})

        # Dedup — block same TP within 60s
        import time
        dedup_key = f"{pair}_{close_type}"
        now = time.time()
        with tp_close_lock:
            if now - tp_close_recent.get(dedup_key, 0) < 60:
                logger.info(f"Duplicate {close_type} blocked")
                return jsonify({"status": "duplicate_ignored"})
            tp_close_recent[dedup_key] = now

        log_event({"type": f"MT5_{close_type}", "pair": pair, "profit": profit})

        if close_type == "SL":
            text = "❌ SL HIT\nXAU/USD | GOLD\n\nSetup invalid. We will be looking for more trades 🔍"
            send_text_telegram(VIP_CHANNEL, text)
            send_to_whatsapp_group(text, "PREMIUM GOLD GROUP")
            return jsonify({"status": "ok"})

        # TP hit — generate profit card
        lo, hi = TP_PROFIT_RANGES.get(close_type, (110, 165))
        profit_gbp = round(random.uniform(lo, hi), 2)
        text = TP_TEXT.get(close_type, f"✅ {close_type} HIT!")

        # No chart — standalone profit card only
        card_bytes = generate_profit_card(close_type, profit_gbp)

        if card_bytes:
            send_photo_telegram(VIP_CHANNEL, card_bytes, text)
        else:
            send_text_telegram(VIP_CHANNEL, text)

        # WhatsApp — text only (image sending via WhatsApp needs index.js update)
        plain_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
        # PAUSED: send_to_whatsapp_group(plain_text, "PREMIUM GOLD GROUP")
        send_to_whatsapp_group(plain_text, "Dummy group testing")  # Testing

        return jsonify({"status": "ok", "profit_gbp": profit_gbp})

    except Exception as e:
        logger.error(f"mt5_close error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

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
              "entry": 4340.00, "sl": 4325.00, "tp1": 4342.00, "tp2": 4343.00, "tp3": 4370.00}
    save_signal(signal)
    return jsonify({"status": "test BUY saved", "signal": signal})

@app.route("/test-sell")
def test_sell():
    signal = {"id": "test02", "pair": "XAUUSD", "direction": "SELL",
              "entry": 4350.00, "sl": 4365.00, "tp1": 4348.00, "tp2": 4347.00, "tp3": 4320.00}
    save_signal(signal)
    return jsonify({"status": "test SELL saved", "signal": signal})

@app.route("/test-tp1")
def test_tp1():
    """Test endpoint to simulate MT5 TP1 hit"""
    import time
    dedup_key = "XAUUSD_TP1_test"
    now = time.time()
    with tp_close_lock:
        tp_close_recent[dedup_key] = 0  # reset so test always fires
    
    lo, hi = TP_PROFIT_RANGES.get("TP1", (110, 165))
    profit_gbp = round(random.uniform(lo, hi), 2)
    text = TP_TEXT.get("TP1", "✅ TP1 HIT!")

    chart_bytes = get_chart_image()
    card_bytes = generate_profit_card("TP1", profit_gbp, chart_bytes)

    if card_bytes:
        send_photo_telegram(VIP_CHANNEL, card_bytes, text)
    else:
        send_text_telegram(VIP_CHANNEL, text)

    plain_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    send_to_whatsapp_group(plain_text, "Dummy group testing")

    return jsonify({"status": "test TP1 triggered", "profit_gbp": profit_gbp})

@app.route("/")
def home():
    return jsonify({"status": "RayGoldSignals running ✅"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
