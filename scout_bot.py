import os
import logging
import requests
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

FILTERS = {
    "max_age_hours": 2,
    "min_liquidity_usd": 5000,
    "max_liquidity_usd": 30000,
    "min_volume_24h": 3000,
    "min_txns_24h": 20,
    "require_socials": True,
}

SCAN_INTERVAL_MINUTES = 10

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

alerted_tokens = set()
subscribed_chats = set()


def fetch_dexscreener_new_pairs():
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data:
            address = item.get("tokenAddress")
            chain = item.get("chainId")
            if not address or not chain:
                continue
            pairs = fetch_dexscreener_token(address, chain)
            results.extend(pairs)
        return results
    except Exception as e:
        log.error(f"DexScreener error: {e}")
        return []


def fetch_dexscreener_token(address, chain):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=10)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        return [p for p in pairs if p.get("chainId") == chain]
    except Exception as e:
        log.error(f"DexScreener token error: {e}")
        return []


def fetch_gecko_networks():
    try:
        r = requests.get(
            "https://api.geckoterminal.com/api/v2/networks",
            timeout=10,
            headers={"Accept": "application/json;version=20230302"},
        )
        r.raise_for_status()
        return [n["id"] for n in r.json().get("data", [])]
    except Exception as e:
        log.error(f"GeckoTerminal networks error: {e}")
        return []


def fetch_gecko_new_pools(network):
    try:
        r = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/{network}/new_pools",
            params={"page": 1},
            headers={"Accept": "application/json;version=20230302"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.error(f"GeckoTerminal {network} error: {e}")
        return []


def normalize_dexscreener(pair):
    try:
        info = pair.get("info") or {}
        socials = info.get("socials") or []
        websites = info.get("websites") or []
        twitter = next((s["url"] for s in socials if s.get("type") == "twitter"), None)
        telegram = next((s["url"] for s in socials if s.get("type") == "telegram"), None)
        created_at = pair.get("pairCreatedAt")
        age_hours = None
        if created_at:
            created_dt = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
        volume = (pair.get("volume") or {}).get("h24", 0) or 0
        txns = pair.get("txns", {})
        buys = (txns.get("h24") or {}).get("buys", 0)
        sells = (txns.get("h24") or {}).get("sells", 0)
        base_token = pair.get("baseToken") or {}
        website = next((w["url"] for w in websites if w.get("url")), None)
        return {
            "id": f"dex_{pair.get('chainId')}_{base_token.get('address')}",
            "name": base_token.get("name", "Unknown"),
            "symbol": base_token.get("symbol", "???"),
            "chain": pair.get("chainId", "unknown"),
            "dex": pair.get("dexId", "unknown"),
            "address": base_token.get("address", ""),
            "liquidity": liquidity,
            "volume_24h": volume,
            "txns_24h": buys + sells,
            "age_hours": age_hours,
            "twitter": twitter,
            "telegram": telegram,
            "website": website,
            "url": pair.get("url", ""),
            "source": "DexScreener",
        }
    except Exception as e:
        log.debug(f"Normalize DexScreener error: {e}")
        return None


def normalize_gecko(pool, network):
    try:
        attrs = pool.get("attributes") or {}
        liquidity = float(attrs.get("reserve_in_usd") or 0)
        volume = float((attrs.get("volume_usd") or {}).get("h24") or 0)
        txns_h24 = (attrs.get("transactions") or {}).get("h24") or {}
        total_txns = (txns_h24.get("buys") or 0) + (txns_h24.get("sells") or 0)
        created_at_str = attrs.get("pool_created_at")
        age_hours = None
        if created_at_str:
            created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        name = attrs.get("name", "Unknown")
        address = attrs.get("address", "")
        return {
            "id": f"gecko_{network}_{address}",
            "name": name,
            "symbol": name.split(" / ")[0] if "/" in name else name,
            "chain": network,
            "dex": (pool.get("relationships") or {}).get("dex", {}).get("data", {}).get("id", "unknown"),
            "address": address,
            "liquidity": liquidity,
            "volume_24h": volume,
            "txns_24h": total_txns,
            "age_hours": age_hours,
            "twitter": None,
            "telegram": None,
            "website": None,
            "url": f"https://www.geckoterminal.com/{network}/pools/{address}",
            "source": "GeckoTerminal",
        }
    except Exception as e:
        log.debug(f"Normalize GeckoTerminal error: {e}")
        return None


def passes_filters(token):
    age = token.get("age_hours")
    liq = token.get("liquidity", 0)
    vol = token.get("volume_24h", 0)
    txns = token.get("txns_24h", 0)
    has_socials = bool(token.get("twitter") or token.get("telegram"))
    if age is not None and age > FILTERS["max_age_hours"]:
        return False
    if liq < FILTERS["min_liquidity_usd"]:
        return False
    if liq > FILTERS["max_liquidity_usd"]:
        return False
    if vol < FILTERS["min_volume_24h"]:
        return False
    if txns < FILTERS["min_txns_24h"]:
        return False
    if FILTERS["require_socials"] and not has_socials:
        return False
    return True


async def run_full_scan():
    found = []
    log.info("Scanning DexScreener...")
    for pair in fetch_dexscreener_new_pairs():
        token = normalize_dexscreener(pair)
        if token and token["id"] not in alerted_tokens and passes_filters(token):
            found.append(token)
    log.info("Scanning GeckoTerminal...")
    for network in fetch_gecko_networks():
        for pool in fetch_gecko_new_pools(network):
            token = normalize_gecko(pool, network)
            if token and token["id"] not in alerted_tokens and passes_filters(token):
                found.append(token)
    for t in found:
        alerted_tokens.add(t["id"])
    log.info(f"Scan done. {len(found)} leads found.")
    return found


def format_alert(token):
    age_str = f"{token['age_hours']:.1f}h old" if token.get("age_hours") is not None else "Age unknown"
    socials = []
    if token.get("twitter"):
        socials.append(f"Twitter/X: {token['twitter']}")
    if token.get("telegram"):
        socials.append(f"Telegram: {token['telegram']}")
    if token.get("website"):
        socials.append(f"Website: {token['website']}")
    socials_str = "\n".join(socials) if socials else "No socials found"
    return (
        f"New Lead: {token['name']} (${token['symbol']})\n"
        f"Chain: {token['chain']} | DEX: {token['dex']}\n"
        f"Age: {age_str}\n"
        f"Liquidity: ${token['liquidity']:,.0f}\n"
        f"Volume: ${token['volume_24h']:,.0f}\n"
        f"Txns: {token['txns_24h']}\n"
        f"---\n"
        f"{socials_str}\n"
        f"---\n"
        f"Chart: {token['url']}\n"
        f"Source: {token['source']}\n"
        f"Pitch: Community Manager / Raider / Project Manager"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribed_chats.add(update.effective_chat.id)
    keyboard = [
        [InlineKeyboardButton("Scan Now", callback_data="scan")],
        [InlineKeyboardButton("My Filters", callback_data="filters")],
        [InlineKeyboardButton("Auto-Alerts ON", callback_data="subscribe")],
    ]
    await update.message.reply_text(
        "Crypto Launch Scout Bot\n\n"
        "I scan DexScreener and GeckoTerminal across ALL chains for brand new token launches.\n\n"
        "Filters:\n"
        "- Under 2 hours old\n"
        "- $5K to $30K liquidity\n"
        "- $3K+ volume\n"
        "- 20+ transactions\n"
        "- Must have X or Telegram\n\n"
        "Tap Scan Now to find leads!",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text("Scanning all chains... please wait 30-60 seconds.")
    leads = await run_full_scan()
    if not leads:
        await msg.edit_text("No leads found right now. Try again in a few minutes!")
        return
    await msg.edit_text(f"Found {len(leads)} lead(s)! Sending now...")
    for token in leads[:20]:
        try:
            await update.effective_message.reply_text(
                format_alert(token),
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.error(f"Send error: {e}")


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = FILTERS
    await update.effective_message.reply_text(
        f"Current Filters\n\n"
        f"Max age: {f['max_age_hours']} hours\n"
        f"Min liquidity: ${f['min_liquidity_usd']:,}\n"
        f"Max liquidity: ${f['max_liquidity_usd']:,}\n"
        f"Min volume: ${f['min_volume_24h']:,}\n"
        f"Min txns: {f['min_txns_24h']}\n"
        f"Require socials: Yes"
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribed_chats.add(update.effective_chat.id)
    await update.effective_message.reply_text(
        f"Subscribed! You will get auto-alerts every {SCAN_INTERVAL_MINUTES} minutes.\n"
        "Type /unsubscribe to stop."
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribed_chats.discard(update.effective_chat.id)
    await update.effective_message.reply_text("Unsubscribed from auto-alerts.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "scan":
        await cmd_scan(update, context)
    elif query.data == "filters":
        await cmd_filters(update, context)
    elif query.data == "subscribe":
        await cmd_subscribe(update, context)


async def auto_scan_job(app):
    if not subscribed_chats:
        return
    leads = await run_full_scan()
    for chat_id in list(subscribed_chats):
        for token in leads[:15]:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=format_alert(token),
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.error(f"Auto-alert error: {e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CallbackQueryHandler(button_handler))
    if SCAN_INTERVAL_MINUTES > 0:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(auto_scan_job, "interval", minutes=SCAN_INTERVAL_MINUTES, args=[app])
        scheduler.start()
    log.info("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
