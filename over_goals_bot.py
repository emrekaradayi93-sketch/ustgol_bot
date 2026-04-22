"""
⚽ Dinamik Üst Gol Botu — Telegram Botu
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sistem Mantığı:
  80–85. dakikada canlı maçlara bakar.
  O anki toplam gol sayısı N ise → (N + 0.5) üst oranına bakar.
  Bu oran 1.50'den düşükse → 🎯 SİNYAL gönderir.

  Örnekler:
    Skor 0-0 (toplam 0 gol) → 0.5 üst oranı < 1.50 → sinyal
    Skor 1-1 (toplam 2 gol) → 2.5 üst oranı < 1.50 → sinyal
    Skor 2-1 (toplam 3 gol) → 3.5 üst oranı < 1.50 → sinyal

API'ler:
  - api-football.com  → canlı skor + dakika bilgisi
  - the-odds-api.com  → canlı üst/alt oranları

Gereksinimler:
  pip install "python-telegram-bot[job-queue]==21.6" aiohttp python-dotenv

.env:
  TELEGRAM_TOKEN=...
  FOOTBALL_API_KEY=...
  ODDS_API_KEY=...
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
ODDS_API_KEY     = os.getenv("ODDS_API_KEY")

FOOTBALL_BASE = "https://v3.football.api-sports.io"
ODDS_BASE     = "https://api.the-odds-api.com/v4"

# ─── Sistem Parametreleri ──────────────────────────────────────────────────────
MINUTE_START     = 80     # Tarama başlangıç dakikası
MINUTE_END       = 85     # Tarama bitiş dakikası
OVER_ODDS_MAX    = 1.50   # Bu oranın altındaysa sinyal ver

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# fixture_id → sinyal gönderildi mi
signaled: set[int] = set()


# ─── API Yardımcıları ──────────────────────────────────────────────────────────

async def get_live_fixtures() -> list:
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{FOOTBALL_BASE}/fixtures?live=all", headers=headers) as r:
            data = await r.json()
            return data.get("response", [])


async def get_over_odds(home: str, away: str, total_goals: int) -> Optional[float]:
    """
    The Odds API üzerinden (total_goals + 0.5) üst oranını çeker.
    Örnek: total_goals=2 → 'Over 2.5' oranını döndürür.
    """
    target_line = total_goals + 0.5  # 0→0.5, 1→1.5, 2→2.5 ...
    target_str  = f"Over {target_line}"

    url = f"{ODDS_BASE}/sports/soccer/odds"
    params = {
        "apiKey":      ODDS_API_KEY,
        "regions":     "eu",
        "markets":     "totals",   # üst/alt piyasası
        "oddsFormat":  "decimal",
    }

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    logger.warning(f"Odds API HTTP {r.status}")
                    return None
                data = await r.json()
    except Exception as e:
        logger.error(f"Odds API bağlantı hatası: {e}")
        return None

    home_l = home.lower()
    away_l = away.lower()

    for event in data:
        eh = event.get("home_team", "").lower()
        ea = event.get("away_team", "").lower()
        match = (home_l in eh or eh in home_l) and (away_l in ea or ea in away_l)
        if not match:
            continue

        for bm in event.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "totals":
                    continue
                for outcome in mkt.get("outcomes", []):
                    # "Over 2.5" gibi eşleştir
                    name  = outcome.get("name", "")
                    point = outcome.get("point", "")
                    if name == "Over" and float(point) == target_line:
                        return float(outcome["price"])
                    # Bazı bookmaker'lar "Over 2.5" şeklinde yazar
                    if name == target_str:
                        return float(outcome["price"])
    return None


# ─── Ana Tarama Döngüsü ────────────────────────────────────────────────────────

async def scan_over_goals(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Her 2 dakikada çalışır.
    80–85. dakikadaki maçları bulur, dinamik üst oranını kontrol eder.
    """
    subscribers: set = context.bot_data.get("subscribers", set())
    if not subscribers:
        return

    try:
        fixtures = await get_live_fixtures()

        # 80–85. dakikada olan 2. yarı maçları filtrele
        window_fixtures = [
            f for f in fixtures
            if f["fixture"]["status"]["short"] == "2H"
            and MINUTE_START <= (f["fixture"]["status"].get("elapsed") or 0) <= MINUTE_END
        ]

        for f in window_fixtures:
            fid = f["fixture"]["id"]
            if fid in signaled:
                continue

            home      = f["teams"]["home"]["name"]
            away      = f["teams"]["away"]["name"]
            home_g    = f["goals"]["home"] or 0
            away_g    = f["goals"]["away"] or 0
            total     = home_g + away_g
            elapsed   = f["fixture"]["status"].get("elapsed", "?")
            league    = f["league"]["name"]
            country   = f["league"]["country"]
            target_line = total + 0.5

            # Canlı üst oranını çek
            over_odds = await get_over_odds(home, away, total)

            if over_odds is None:
                logger.info(f"Oran bulunamadı: {home} vs {away} | Over {target_line}")
                continue

            logger.info(f"dk {elapsed} | {home} {home_g}-{away_g} {away} | Over {target_line} = {over_odds}")

            if over_odds >= OVER_ODDS_MAX:
                continue  # Koşul sağlanmadı

            # ✅ Sinyal koşulu sağlandı
            signaled.add(fid)

            msg = (
                f"🎯 *ÜST GOL SİNYALİ*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏆 {league} ({country})\n"
                f"⚽ *{home}* {home_g} – {away_g} *{away}*\n"
                f"⏱ Dakika: *{elapsed}'*\n"
                f"📊 Over *{target_line}* oranı: *{over_odds:.2f}* (< {OVER_ODDS_MAX})\n"
                f"🕐 {datetime.now().strftime('%H:%M')}"
            )

            for chat_id in subscribers:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.warning(f"Mesaj gönderilemedi {chat_id}: {e}")

    except Exception as e:
        logger.error(f"scan_over_goals hatası: {e}")


# ─── Komutlar ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers: set = context.bot_data.setdefault("subscribers", set())
    subscribers.add(update.effective_chat.id)
    await update.message.reply_text(
        "🎯 *Dinamik Üst Gol Botu*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Sistem şu kriteri arar:\n\n"
        "✅ Maç *80–85. dakikada*\n"
        "✅ O anki toplam gol + 0.5 üst oranı *< 1.50*\n\n"
        "Örnekler:\n"
        "• Skor 0-0 → Over 0.5 oranı < 1.50\n"
        "• Skor 1-1 → Over 2.5 oranı < 1.50\n"
        "• Skor 2-0 → Over 2.5 oranı < 1.50\n\n"
        "Bildirimler açıldı! Sinyal gelince haber veririm. 📲\n\n"
        "• /stop — Bildirimleri kapat\n"
        "• /help — Yardım",
        parse_mode="Markdown",
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs: set = context.bot_data.get("subscribers", set())
    subs.discard(update.effective_chat.id)
    await update.message.reply_text("🔕 Bildirimler kapatıldı. Tekrar açmak için /start yaz.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *Komutlar*\n\n"
        "• /start — Botu başlat & bildirimleri aç\n"
        "• /stop — Bildirimleri kapat\n"
        "• /help — Bu menü\n\n"
        f"Bot her 2 dakikada 80–85. dakika aralığındaki\n"
        f"maçları tarar ve Over N+0.5 oranı < {OVER_ODDS_MAX} ise sinyal gönderir.",
        parse_mode="Markdown",
    )


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    for key, name in [
        (TELEGRAM_TOKEN, "TELEGRAM_TOKEN"),
        (FOOTBALL_API_KEY, "FOOTBALL_API_KEY"),
        (ODDS_API_KEY, "ODDS_API_KEY"),
    ]:
        if not key:
            raise ValueError(f"{name} .env dosyasında bulunamadı!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop",  stop_command))
    app.add_handler(CommandHandler("help",  help_command))

    # Her 2 dakikada tara
    app.job_queue.run_repeating(scan_over_goals, interval=120, first=15)

    logger.info("🎯 Dinamik Üst Gol Botu başlatıldı!")

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot çalışıyor, durdurmak için Ctrl+C bas.")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
