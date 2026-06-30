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

APP_DIR = os.path.dirname(os.path.abspath(__file__))

def find_font(size=54):
    """Load font — checks repo fonts/ folder first."""
    candidates = [
        os.path.join(APP_DIR, "fonts", "DejaVuSans-Bold.ttf"),
        os.path.join(APP_DIR, "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                font = ImageFont.truetype(path, size)
                logger.info(f"✅ Font loaded: {path} @ {size}pt")
                return font
        except Exception as e:
            logger.debug(f"Font candidate failed ({path}): {e}")
            continue
    logger.error(f"❌ NO FONT FOUND — using tiny default")
    return ImageFont.load_default()

def find_font_fit(draw, text, max_width, start_size=220, min_size=60):
    """Auto-shrink font until text fits within max_width."""
    for size in range(start_size, min_size - 1, -4):
        font = find_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            return font, size
    return find_font(min_size), min_size

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SIGNAL_FILE = "/tmp/mt5_signal.json"
LOG_FILE    = "/tmp/mt5_log.json"
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
VIP_CHANNEL = os.environ.get("VIP_CHANNEL", "-1004347840465")
WHATSAPP_URL = os.environ.get("WHATSAPP_URL", "https://web-production-6cec8d.up.railway.app")
CHART_IMG_KEY = os.environ.get("CHART_IMG_KEY", "GhKjWUCZA61Lx0OwoNZvp8AhcLtTkWee702zMySE")
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

tp_close_lock   = threading.Lock()
tp_close_recent = {}

# Public base URL of THIS service (RayGoldSignals), used so WhatsApp's
# bot (index.js) can fetch generated images by URL. Set this in Railway
# env vars to your RayGoldSignals public domain, e.g.
# https://web-production-f54d0.up.railway.app
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# ─────────────────────────────────────────────
# IMAGE CACHE — lets WhatsApp's bot fetch generated images by URL
# ─────────────────────────────────────────────

image_cache_lock = threading.Lock()
image_cache = {}  # id -> (bytes, content_type, created_at)
IMAGE_CACHE_MAX_AGE = 600  # seconds — auto-expire old entries

def host_image(image_bytes, content_type="image/jpeg"):
    """Store image bytes in memory and return a public URL for it."""
    import time
    img_id = str(uuid.uuid4())[:12]
    with image_cache_lock:
        # purge old entries
        now = time.time()
        expired = [k for k, v in image_cache.items() if now - v[2] > IMAGE_CACHE_MAX_AGE]
        for k in expired:
            del image_cache[k]
        image_cache[img_id] = (image_bytes, content_type, now)

    if not PUBLIC_BASE_URL:
        logger.warning("⚠️ PUBLIC_BASE_URL not set — image URL will be relative and WON'T work for WhatsApp!")
    return f"{PUBLIC_BASE_URL}/image/{img_id}"

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

    direction = None
    if re.search(r'\bsell\b', text, re.IGNORECASE):
        direction = "SELL"
    elif re.search(r'\bbuy\b', text, re.IGNORECASE):
        direction = "BUY"
    if not direction:
        return None

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

    tps = []
    for m in re.finditer(r'TP\s*(\d+)\s+([3-9][0-9]{2,3}(?:\.[0-9]+)?)', text, re.IGNORECASE):
        tp_num = int(m.group(1))
        if tp_num <= 3:
            tps.append(float(m.group(2)))

    if not tps:
        for m in re.finditer(r'TP\s*[:\s]\s*([3-9][0-9]{2,3}(?:\.[0-9]+)?)', text, re.IGNORECASE):
            tps.append(float(m.group(1)))

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
        "id":        str(uuid.uuid4())[:8],
        "pair":      "XAUUSD",
        "direction": direction,
        "entry":     round(entry, 2),
        "sl":        round(sl, 2),
        "tp1":       round(tps[0], 2),
        "tp2":       round(tps[1], 2),
        "tp3":       round(tps[2], 2),
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

def generate_profit_card(close_type, profit_gbp):
    """Generate profit card with auto-fit font so number always fits."""
    if not PIL_AVAILABLE:
        return None
    try:
        W, H = 800, 600
        bw   = 18
        PAD  = 60   # horizontal padding inside border
        GOLD = (212, 175, 55)

        img  = Image.new("RGB", (W, H), (5, 5, 10))
        draw = ImageDraw.Draw(img)

        # Gold border
        draw.rectangle([0,    0,    W,    bw],   fill=GOLD)
        draw.rectangle([0,    H-bw, W,    H],    fill=GOLD)
        draw.rectangle([0,    0,    bw,   H],    fill=GOLD)
        draw.rectangle([W-bw, 0,    W,    H],    fill=GOLD)

        tp_labels = {
            "TP1": "TP1 SMASHED",
            "TP2": "TP2 SMASHED",
            "TP3": "ALL TARGETS HIT",
            "TP4": "TP4 SMASHED",
            "TP5": "TP5 FULL SEND",
        }
        tp_label   = tp_labels.get(close_type, f"{close_type} HIT")
        profit_str = f"£{profit_gbp:,.2f}"   # no + sign — more space for number
        tagline    = "PREMIUM TRADE"

        max_w = W - (bw * 2) - (PAD * 2)   # usable width for profit number

        f_label   = find_font(64)
        f_tagline = find_font(52)
        f_profit, profit_size = find_font_fit(draw, profit_str, max_w)
        logger.info(f"Profit font auto-sized to {profit_size}pt for '{profit_str}'")

        # ── TP LABEL ──────────────────────────────
        bbox = draw.textbbox((0, 0), tp_label, font=f_label)
        tw   = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, 50), tp_label, font=f_label, fill=(255, 255, 255))

        # ── DIVIDER ───────────────────────────────
        draw.rectangle([40, 130, W-40, 134], fill=GOLD)

        # ── PROFIT — centred, auto-sized ──────────
        bbox     = draw.textbbox((0, 0), profit_str, font=f_profit)
        tw, th   = bbox[2]-bbox[0], bbox[3]-bbox[1]
        px       = (W - tw) // 2
        py       = 150 + ((310 - th) // 2)   # vertically centred in zone 130-460

        draw.text((px+4, py+4), profit_str, font=f_profit, fill=(0, 60, 0))   # shadow
        draw.text((px,   py),   profit_str, font=f_profit, fill=(0, 238, 80)) # green

        # ── DIVIDER ───────────────────────────────
        draw.rectangle([40, 460, W-40, 464], fill=GOLD)

        # ── TAGLINE ───────────────────────────────
        bbox = draw.textbbox((0, 0), tagline, font=f_tagline)
        tw   = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, 492), tagline, font=f_tagline, fill=GOLD)

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        buf.seek(0)
        result = buf.read()
        logger.info(f"✅ Profit card: {len(result)} bytes")
        return result

    except Exception as e:
        logger.error(f"❌ Profit card error: {e}", exc_info=True)
        return None

# ─────────────────────────────────────────────
# TELEGRAM + WHATSAPP SENDERS
# ─────────────────────────────────────────────

def send_photo_telegram(chat_id, photo_bytes, caption):
    try:
        files  = {"photo": ("card.jpg", photo_bytes, "image/jpeg")}
        data   = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        r      = requests.post(f"{TELEGRAM_URL}/sendPhoto", files=files, data=data, timeout=15)
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
            "chat_id": chat_id, "text": text, "parse_mode": "HTML"
        }, timeout=10)
        return r.json().get("ok", False)
    except Exception as e:
        logger.error(f"Telegram text error: {e}")
    return False

def send_to_whatsapp_group(message, group, image_url=None):
    try:
        payload = {"message": message, "group": group}
        if image_url:
            payload["image_url"] = image_url
        r = requests.post(f"{WHATSAPP_URL}/send", json=payload, timeout=15)
        if r.status_code == 200:
            logger.info(f"✅ WhatsApp sent to {group}" + (" (with image)" if image_url else ""))
            return True
        logger.warning(f"WhatsApp failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")
    return False

# ─────────────────────────────────────────────
# TP PROFIT AMOUNTS (GBP)
# ─────────────────────────────────────────────

TP_PROFIT_RANGES = {
    "TP1": (450,  660),
    "TP2": (750,  1050),
    "TP3": (4000, 5000),
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
        data       = request.json or {}
        close_type = data.get("close_type", "")
        pair       = data.get("pair", "XAUUSD")
        profit     = float(data.get("profit", 0))

        logger.info(f"MT5 close: {pair} {close_type} profit={profit}")

        if close_type not in ("TP1", "TP2", "TP3", "TP4", "TP5", "SL"):
            return jsonify({"status": "ignored"})

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

        lo, hi     = TP_PROFIT_RANGES.get(close_type, (450, 660))
        profit_gbp = round(random.uniform(lo, hi), 2)
        text       = TP_TEXT.get(close_type, f"✅ {close_type} HIT!")
        card_bytes = generate_profit_card(close_type, profit_gbp)

        if card_bytes:
            send_photo_telegram(VIP_CHANNEL, card_bytes, text)
        else:
            send_text_telegram(VIP_CHANNEL, text)

        plain_text = text.replace("<b>","").replace("</b>","").replace("<i>","").replace("</i>","")

        # Host the profit card so WhatsApp can fetch it by URL
        card_url = host_image(card_bytes, "image/jpeg") if card_bytes else None
        if card_bytes and not card_url:
            logger.warning("⚠️ Profit card generated but hosting failed — sending text only to WhatsApp")

        send_to_whatsapp_group(plain_text, "PREMIUM GOLD GROUP", image_url=card_url)
        send_to_whatsapp_group(plain_text, "Dummy group testing", image_url=card_url)

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
    import time
    with tp_close_lock:
        tp_close_recent["XAUUSD_TP1"] = 0
    lo, hi     = TP_PROFIT_RANGES["TP1"]
    profit_gbp = round(random.uniform(lo, hi), 2)
    text       = TP_TEXT["TP1"]
    card_bytes = generate_profit_card("TP1", profit_gbp)
    if card_bytes:
        send_photo_telegram(VIP_CHANNEL, card_bytes, text)
    else:
        send_text_telegram(VIP_CHANNEL, text)
    plain_text = text.replace("<b>","").replace("</b>","").replace("<i>","").replace("</i>","")
    card_url = host_image(card_bytes, "image/jpeg") if card_bytes else None
    send_to_whatsapp_group(plain_text, "Dummy group testing", image_url=card_url)
    return jsonify({"status": "test TP1 triggered", "profit_gbp": profit_gbp, "card_url": card_url})

@app.route("/test-tp2")
def test_tp2():
    import time
    with tp_close_lock:
        tp_close_recent["XAUUSD_TP2"] = 0
    lo, hi     = TP_PROFIT_RANGES["TP2"]
    profit_gbp = round(random.uniform(lo, hi), 2)
    text       = TP_TEXT["TP2"]
    card_bytes = generate_profit_card("TP2", profit_gbp)
    if card_bytes:
        send_photo_telegram(VIP_CHANNEL, card_bytes, text)
    else:
        send_text_telegram(VIP_CHANNEL, text)
    plain_text = text.replace("<b>","").replace("</b>","").replace("<i>","").replace("</i>","")
    card_url = host_image(card_bytes, "image/jpeg") if card_bytes else None
    send_to_whatsapp_group(plain_text, "Dummy group testing", image_url=card_url)
    return jsonify({"status": "test TP2 triggered", "profit_gbp": profit_gbp, "card_url": card_url})

@app.route("/test-tp3")
def test_tp3():
    import time
    with tp_close_lock:
        tp_close_recent["XAUUSD_TP3"] = 0
    lo, hi     = TP_PROFIT_RANGES["TP3"]
    profit_gbp = round(random.uniform(lo, hi), 2)
    text       = TP_TEXT["TP3"]
    card_bytes = generate_profit_card("TP3", profit_gbp)
    if card_bytes:
        send_photo_telegram(VIP_CHANNEL, card_bytes, text)
    else:
        send_text_telegram(VIP_CHANNEL, text)
    plain_text = text.replace("<b>","").replace("</b>","").replace("<i>","").replace("</i>","")
    card_url = host_image(card_bytes, "image/jpeg") if card_bytes else None
    send_to_whatsapp_group(plain_text, "Dummy group testing", image_url=card_url)
    return jsonify({"status": "test TP3 triggered", "profit_gbp": profit_gbp, "card_url": card_url})

@app.route("/image/<img_id>", methods=["GET"])
def serve_image(img_id):
    from flask import Response
    with image_cache_lock:
        entry = image_cache.get(img_id)
    if not entry:
        return jsonify({"error": "not found or expired"}), 404
    image_bytes, content_type, _ = entry
    return Response(image_bytes, mimetype=content_type)

@app.route("/host_image", methods=["POST"])
def upload_image():
    """Accepts raw image bytes (e.g. from bot.py's chart fetch) and returns
    a public URL so WhatsApp's bot can fetch it. Send as raw body with
    Content-Type: image/jpeg or image/png."""
    image_bytes = request.get_data()
    if not image_bytes:
        return jsonify({"error": "no image data"}), 400
    content_type = request.content_type or "image/jpeg"
    url = host_image(image_bytes, content_type)
    return jsonify({"status": "ok", "url": url})

@app.route("/")
def home():
    return jsonify({"status": "RayGoldSignals running ✅"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
