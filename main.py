#!/usr/bin/env python3
# amazon_price_tracker_bot.py
# One-file Amazon category price tracker with Telegram alerts + affiliate links.
# NOTE: Scraping Amazon may violate their Terms. For production, prefer Amazon Product Advertising API.

import os
import time
import json
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7234182173:AAHqHVhrFK6Z4O6lZMk7XdYbKZiOPlF7BFQ")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "@lootproductsofficial")  # e.g., 123456789
AMAZON_AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "welldecore-21")

# Price filter
MIN_PRICE = float(os.getenv("MIN_PRICE", 150))
MAX_PRICE = float(os.getenv("MAX_PRICE", 1000))

# Mega-deal alert threshold (if MRP visible on listing)
MEGA_MIN = 80.0
MEGA_MAX = 95.0

# Check interval in minutes
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", 5))

# Categories: put your preferred Amazon search/category URLs here (India site shown)
CATEGORIES: Dict[str, str] = {
    # Enable/disable by commenting/uncommenting lines below:
    "mobiles":      "https://amzn.to/4fJChj3",
    "accessories":  "https://amzn.to/41eAsEU",
    "home":         "https://amzn.to/45rKAwC",
    "watches" :     "https://amzn.to/4lEzWHD"
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
        # Some pages show prices like '1,29900' (rare) — handle basic decimal
        if t.count(".") > 1:
            # Fallback: remove all dots except last
            parts = t.split(".")
            t = "".join(parts[:-1]) + "." + parts[-1]
        return float(t)
    except Exception:
        return None


def parse_price_from_listing(item) -> Optional[float]:
    """Extract current price on search results card."""
    # 1) span.a-offscreen typically holds the visible price (e.g., ₹899.00)
    offscreen = item.find("span", class_="a-offscreen")
    if offscreen and offscreen.text:
        p = normalize_price(offscreen.text)
        if p is not None:
            return p

    # 2) Fallback: a-price-whole + a-price-fraction
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
    # Often in span.a-text-price > span.a-offscreen
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

            mrp = parse_mrp_from_listing(it)  # may be None

            entry = {
                "title": title,
                "price": price,
                "mrp": mrp,
                "link": link,
                "high_alert": False
            }

            # Mark as high alert if we can compute 80–95% off
            if mrp and mrp > 0:
                discount = (mrp - price) / mrp * 100.0
                if MEGA_MIN <= discount <= MEGA_MAX:
                    entry["high_alert"] = True
                    entry["discount"] = discount

            # We push BOTH: in-range items OR high-alerts
            if (MIN_PRICE <= price <= MAX_PRICE) or entry["high_alert"]:
                products.append(entry)

        except Exception:
            # Robust to minor HTML changes
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
    """Build a human-readable message without Markdown (avoid parse quirks)."""
    ts = datetime.now().strftime("%d-%b-%Y %H:%M")
    title = product.get("title", "Product")
    price = product.get("price")
    mrp = product.get("mrp")
    link = affiliate(product.get("link", ""))

    # High alert format
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

    # Normal deal format
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
    if not TELEGRAM_BOT_TOKEN or "REPLACE_WITH" in TELEGRAM_BOT_TOKEN:
        logging.error("Please set TELEGRAM_BOT_TOKEN.")
        ok = False
    if not TELEGRAM_CHAT_ID or "REPLACE_WITH" in str(TELEGRAM_CHAT_ID):
        logging.error("Please set TELEGRAM_CHAT_ID.")
        ok = False
    if not AMAZON_AFFILIATE_TAG or AMAZON_AFFILIATE_TAG == "yourtag-21":
        logging.warning("Using default affiliate tag. Replace AMAZON_AFFILIATE_TAG for your account.")
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
                    time.sleep(0.6)  # gentle pacing
                except Exception as e:
                    logging.warning(f"Telegram send failed: {e}")
        except Exception as e:
            logging.warning(f"Error checking {name}: {e}")
    logging.info(f"Sent {sent} alert(s).")


def main():
    if not validate_config():
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Send startup ping
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🤖 Bot started. Monitoring categories…")
    except Exception as e:
        logging.error(f"Failed to send startup message: {e}")

    # Run once on start
    run_check(bot)

    # Schedule periodic checks
    schedule.every(CHECK_INTERVAL_MIN).minutes.do(run_check, bot=bot)

    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
