
#!/usr/bin/env python3
# amazon_price_tracker_bot.py
# One-file Amazon category price tracker with Telegram alerts + affiliate links.
# NOTE: Scraping Amazon may violate their Terms. For production, prefer Amazon Product Advertising API.

import os
import time
import logging
from datetime import datetime
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup
from telegram import Bot
import schedule

# =========================
# ==== USER SETTINGS ======
# =========================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "7234182173"
)
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "1402152106"  # e.g., 123456789 or channel username
)
AMAZON_AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "welldecore-21")

# Price filter
MIN_PRICE = float(os.getenv("MIN_PRICE", 150))
MAX_PRICE = float(os.getenv("MAX_PRICE", 1000))

# Mega-deal alert threshold (if MRP visible on listing)
MEGA_MIN = 80.0
MEGA_MAX = 95.0

# Check interval in minutes
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", 3))

# Categories: put your preferred Amazon search/category URLs here (India site shown)
CATEGORIES: Dict[str, str] = {
    "mobiles": "https://amzn.to/4fJChj3",
    "accessories": "https://amzn.to/41eAsEU",
    "home": "https://amzn.to/45rKAwC",
    "watches": "https://amzn.to/4lEzWHD"
}

# HTTP headers to reduce blocking
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/117.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9"
}

BASE_DOMAIN = "https://www.amazon.in"

# =========================
# ====== LOGGING ==========
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# =========================
# ====== SCRAPING =========
# =========================

def normalize_price(text: str) -> Optional[float]:
    """Convert '₹1,234.00' or '1,234' to float."""
    if not text:
        return None
    try:
        t = text.replace("₹", "").replace(",", "").strip()
        if t.count(".") > 1:
            parts = t.split(".")
            t = "".join(parts[:-1]) + "." + parts[-1]
        return float(t)
    except Exception:
        return None


def parse_price_from_listing(item) -> Optional[float]:
    """Extract current price on search results card."""
    offscreen = item.find("span", class_="a-offscreen")
    if offscreen and offscreen.text:
        p = normalize_price(offscreen.text)
        if p is not None:
            return p

    whole = item.find("span", class_="a-price-whole")
    if whole and whole.get_text(strip=True):
        s = whole.get_text(strip=True).replace(",", "")
        frac = item.find("span", class_="a-price-fraction")
        if frac and frac.get_text(strip=True):
            s += "." + frac.get_text(strip=True)
        try:
            return float(s)
        except Exception:
            return None

    return None


def parse_mrp_from_listing(item) -> Optional[float]:
    """Extract strike-through MRP if present to compute discount."""
    strike = item.find("span", class_="a-text-price")
    if strike:
        off = strike.find("span", class_="a-offscreen")
        if off and off.text:
            return normalize_price(off.text)
    return None


def fetch_category_products(category_url: str, max_items: int = 12) -> List[Dict]:
    """Fetch and parse top products from a category/search page."""
    products: List[Dict] = []
    try:
        r = requests.get(category_url, headers=HEADERS, timeout=25)
        r.raise_for_status()
    except Exception as e:
        logging.warning(f"Failed to GET category page: {e}")
        return products

    soup = BeautifulSoup(r.content, "html.parser")
    cards = soup.find_all("div", {"data-component-type": "s-search-result"})
    for it in cards:
        if len(products) >= max_items:
            break
        try:
            h2 = it.find("h2")
            if not h2 or not h2.a:
                continue
            title = h2.get_text(strip=True)
            href = h2.a.get("href") or ""
            link = href if href.startswith("http") else (BASE_DOMAIN + href)

            price = parse_price_from_listing(it)
            if price is None:
                continue

            mrp = parse_mrp_from_listing(it)

            entry = {
                "title": title,
                "price": price,
                "mrp": mrp,
                "link": link,
                "high_alert": False
            }

            if mrp and mrp > 0:
                discount = (mrp - price) / mrp * 100.0
                if MEGA_MIN <= discount <= MEGA_MAX:
                    entry["high_alert"] = True
                    entry["discount"] = discount

            if (MIN_PRICE <= price <= MAX_PRICE) or entry["high_alert"]:
                products.append(entry)

        except Exception:
            continue

    return products


# =========================
# ====== MESSAGING ========
# =========================

def affiliate(url: str) -> str:
    """Append affiliate tag to URL."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tag={AMAZON_AFFILIATE_TAG}"


def build_message(product: Dict, category_name: str) -> str:
    """Build a human-readable message."""
    ts = datetime.now().strftime("%d-%b-%Y %H:%M")
    title = product.get("title", "Product")
    price = product.get("price")
    mrp = product.get("mrp")
    link = affiliate(product.get("link", ""))

    if product.get("high_alert") and mrp:
        discount = product.get("discount")
        lines = [
            "🚨🚨 MEGA DEAL ALERT 🚨🚨",
            f"Category: {category_name}",
            f"Title: {title}",
            f"MRP: ₹{int(mrp)}",
            f"Offer Price: ₹{int(round(price))}",
            f"Discount: {discount:.1f}% OFF",
            f"Time: {ts}",
            "CTA: Hurry! Limited stock!",
            f"Link: {link}"
        ]
        return "\n".join(lines)

    mrp_part = f" (MRP: ₹{int(mrp)})" if mrp else ""
    lines = [
        f"📢 {category_name.upper()} Deal ({ts})",
        f"Title: {title}",
        f"Price: ₹{int(round(price))}{mrp_part}",
        "CTA: Grab it before it’s gone!",
        f"Link: {link}"
    ]
    return "\n".join(lines)


# =========================
# ====== BOT & JOBS =======
# =========================

def validate_config() -> bool:
    ok = True
    if not TELEGRAM_BOT_TOKEN or "7234182173" in TELEGRAM_BOT_TOKEN:
        logging.error("Please set a valid TELEGRAM_BOT_TOKEN.")
        ok = False
    if not TELEGRAM_CHAT_ID or "1402152106" in str(TELEGRAM_CHAT_ID):
        logging.error("Please set TELEGRAM_CHAT_ID (your chat or channel).")
        ok = False
    if not AMAZON_AFFILIATE_TAG or AMAZON_AFFILIATE_TAG == "welldecore-21":
        logging.warning("Using default affiliate tag. Replace with your own AMAZON_AFFILIATE_TAG.")
    if not CATEGORIES:
        logging.error("No categories defined. Fill CATEGORIES dict.")
        ok = False
    return ok


def run_check(bot: Bot) -> None:
    sent = 0
    for name, url in CATEGORIES.items():
        try:
            products = fetch_category_products(url, max_items=12)
            for p in products:
                msg = build_message(p, name)
                try:
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
                    sent += 1
                    time.sleep(0.6)
                except Exception as e:
                    logging.warning(f"Telegram send failed: {e}")
        except Exception as e:
            logging.warning(f"Error checking {name}: {e}")
    logging.info(f"Sent {sent} alert(s).")


def main():
    if not validate_config():
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🤖 Bot started. Monitoring categories…")
    except Exception as e:
        logging.error(f"Failed to send startup message: {e}")

    run_check(bot)

    schedule.every(CHECK_INTERVAL_MIN).minutes.do(run_check, bot=bot)

    while True:
        schedule.run_pending()
        time.sleep(2)


if __name__ == "__main__":
    main()
