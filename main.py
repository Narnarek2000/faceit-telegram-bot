import os
import logging
from typing import Optional, Dict, Tuple

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
##
# =========================
# CONFIG
# =========================
TG_BOT_TOKEN = os.getenv("8692329888:AAGh-uUzW9z4HHVoVnenhRiXjM9aiAIL2s0")
FACEIT_API_KEY = os.getenv("6dc92495-d0e2-45f1-a658-d52b02229bfb")

BASE_URL = "https://open.faceit.com/data/v4"
REQUEST_TIMEOUT = 20

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# chat_id -> tracking data
TRACKED_PLAYERS: Dict[int, Dict[str, str]] = {}


# =========================
# FACEIT API
# =========================
def faceit_request(path: str, params: Optional[dict] = None) -> Tuple[Optional[dict], Optional[str]]:
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {FACEIT_API_KEY}",
        "Accept": "application/json",
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)

        if r.status_code != 200:
            return None, f"FACEIT error {r.status_code}: {r.text[:300]}"

        if "application/json" not in r.headers.get("content-type", "").lower():
            return None, f"Non-JSON response: {r.text[:300]}"

        return r.json(), None

    except requests.RequestException as e:
        return None, f"Request error: {e}"


def search_player(nickname: str) -> Tuple[Optional[dict], Optional[str]]:
    data, error = faceit_request(
        "/search/players",
        params={
            "nickname": nickname,
            "game": "cs2",
            "limit": 10,
            "offset": 0,
        },
    )
    if error:
        return None, error

    items = data.get("items", [])
    if not items:
        return None, "Игрок не найден."

    exact = None
    for item in items:
        if item.get("nickname", "").lower() == nickname.lower():
            exact = item
            break

    return exact if exact else items[0], None


def get_player_history(player_id: str, limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    return faceit_request(
        f"/players/{player_id}/history",
        params={
            "game": "cs2",
            "limit": limit,
            "offset": 0,
        },
    )


def get_match_details(match_id: str) -> Tuple[Optional[dict], Optional[str]]:
    return faceit_request(f"/matches/{match_id}")


def get_player_recent_stats(player_id: str, limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    return faceit_request(
        f"/players/{player_id}/games/cs2/stats",
        params={
            "limit": limit,
            "offset": 0,
        },
    )


# =========================
# HELPERS
# =========================
def extract_player_id(player: dict) -> Optional[str]:
    return player.get("player_id") or player.get("id") or player.get("guid")


def extract_last_match_id(history_data: Optional[dict]) -> Optional[str]:
    if not isinstance(history_data, dict):
        return None

    items = history_data.get("items", [])
    if not items:
        return None

    first = items[0]
    return first.get("match_id") or first.get("id")


def safe_get(data: Optional[dict], key: str, default="N/A"):
    if not isinstance(data, dict):
        return default
    value = data.get(key, default)
    if value in ("", None):
        return default
    return value


def parse_result(value) -> str:
    s = str(value).strip().lower()
    if s in ("1", "win", "won"):
        return "WIN"
    if s in ("0", "loss", "lose", "lost"):
        return "LOSS"
    return str(value)


def find_match_stats_in_recent(recent_data: Optional[dict], match_id: str) -> Optional[dict]:
    if not isinstance(recent_data, dict):
        return None

    items = recent_data.get("items", [])
    for item in items:
        current_match_id = item.get("match_id") or item.get("id")
        if current_match_id == match_id:
            return item

    return None


def format_start_message(nickname: str, match_id: str, match_details: Optional[dict]) -> str:
    if not isinstance(match_details, dict):
        return (
            f"🔥 {nickname} начал новый матч\n\n"
            f"🎮 Игрок: {nickname}\n"
            f"🆔 Match ID: {match_id}"
        )

    status = safe_get(match_details, "status")
    competition_name = safe_get(match_details, "competition_name")
    region = safe_get(match_details, "region")
    started_at = safe_get(match_details, "started_at")

    map_name = "N/A"
    voting = match_details.get("voting", {})
    if isinstance(voting, dict):
        map_info = voting.get("map", {})
        if isinstance(map_info, dict):
            map_name = map_info.get("pick", "N/A")

    return (
        f"🔥 {nickname} начал новый матч\n\n"
        f"🎮 Игрок: {nickname}\n"
        f"🆔 Match ID: {match_id}\n"
        f"📍 Status: {status}\n"
        f"🏆 Queue: {competition_name}\n"
        f"🌍 Region: {region}\n"
        f"🗺 Map: {map_name}\n"
        f"⏰ Started: {started_at}"
    )


def format_finish_message(nickname: str, match_id: str, recent_match_stats: Optional[dict]) -> str:
    if not isinstance(recent_match_stats, dict):
        return (
            f"✅ Матч {nickname} завершён\n\n"
            f"🎮 Игрок: {nickname}\n"
            f"🆔 Match ID: {match_id}\n"
            f"📊 Подробная статистика не найдена."
        )

    stats = recent_match_stats.get("stats", {})
    map_name = safe_get(stats, "Map")
    result = parse_result(safe_get(stats, "Result"))
    kills = safe_get(stats, "Kills", "0")
    deaths = safe_get(stats, "Deaths", "0")
    kd = safe_get(stats, "K/D Ratio")
    hs = safe_get(stats, "Headshots %")
    kr = safe_get(stats, "K/R Ratio")
    score = stats.get("Final Score") or stats.get("Score") or "N/A"

    return (
        f"✅ Матч {nickname} завершён\n\n"
        f"🎮 Игрок: {nickname}\n"
        f"🆔 Match ID: {match_id}\n"
        f"🗺 Map: {map_name}\n"
        f"🏁 Result: {result}\n"
        f"📈 Score: {score}\n"
        f"🔫 K/D: {kills}/{deaths} ({kd})\n"
        f"🎯 HS: {hs}%\n"
        f"⚡ K/R: {kr}"
    )


# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Privet 👋\n\n"
        "Команды:\n"
        "/trackfull nickname — полное слежение за матчем\n"
        "/untrackfull — остановить слежение\n"
        "/trackstatus — статус слежения\n\n"
        "Пример:\n"
        "/trackfull NaPi"
    )
    await update.message.reply_text(text)


async def trackfull_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /trackfull nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Ищу игрока и включаю полное слежение...")

    player, error = search_player(nickname)
    if error:
        await msg.edit_text(error)
        return

    player_id = extract_player_id(player)
    if not player_id:
        await msg.edit_text("Не удалось получить player_id.")
        return

    found_nick = player.get("nickname", nickname)

    history, history_error = get_player_history(player_id, limit=1)
    if history_error:
        await msg.edit_text(f"Игрок найден, но не удалось получить историю матчей:\n{history_error}")
        return

    last_match_id = extract_last_match_id(history) or ""
    chat_id = update.effective_chat.id

    TRACKED_PLAYERS[chat_id] = {
        "nickname": found_nick,
        "player_id": player_id,
        "last_match_id": last_match_id,
        "active_match_id": "",
        "match_started_sent": "0",
    }

    await msg.edit_text(
        f"👀 Полное слежение включено за {found_nick}\n\n"
        f"Я сообщу, когда начнётся новый матч и когда он закончится."
    )


async def untrackfull_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in TRACKED_PLAYERS:
        await update.message.reply_text("Сейчас в этом чате ни за кем не слежу.")
        return

    nickname = TRACKED_PLAYERS[chat_id]["nickname"]
    del TRACKED_PLAYERS[chat_id]

    await update.message.reply_text(f"🛑 Полное слежение за {nickname} остановлено.")


async def trackstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in TRACKED_PLAYERS:
        await update.message.reply_text("Слежение сейчас не активно.")
        return

    data = TRACKED_PLAYERS[chat_id]
    text = (
        f"👀 Активное слежение\n\n"
        f"🎮 Игрок: {data['nickname']}\n"
        f"🆔 Player ID: {data['player_id']}\n"
        f"🧩 Последний матч: {data.get('last_match_id', 'N/A')}\n"
        f"🔥 Активный матч: {data.get('active_match_id', 'N/A') or 'нет'}"
    )
    await update.message.reply_text(text)


# =========================
# BACKGROUND TRACKER
# =========================
async def track_matches_job(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, data in list(TRACKED_PLAYERS.items()):
        nickname = data["nickname"]
        player_id = data["player_id"]
        old_last_match_id = data.get("last_match_id", "")
        active_match_id = data.get("active_match_id", "")
        match_started_sent = data.get("match_started_sent", "0")

        history, history_error = get_player_history(player_id, limit=1)
        if history_error:
            logger.warning("History error for %s: %s", nickname, history_error)
            continue

        new_last_match_id = extract_last_match_id(history)
        if not new_last_match_id:
            continue

        # если появился новый матч — значит он начался
        if old_last_match_id and new_last_match_id != old_last_match_id and not active_match_id:
            TRACKED_PLAYERS[chat_id]["active_match_id"] = new_last_match_id
            TRACKED_PLAYERS[chat_id]["match_started_sent"] = "1"

            match_details, _ = get_match_details(new_last_match_id)
            text = format_start_message(nickname, new_last_match_id, match_details)

            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.warning("Failed to send start message to chat %s: %s", chat_id, e)

            continue

        # если активный матч есть — проверяем, закончился ли он
        if active_match_id:
            recent_data, recent_error = get_player_recent_stats(player_id, limit=5)
            if recent_error:
                logger.warning("Recent stats error for %s: %s", nickname, recent_error)
                continue

            match_stats = find_match_stats_in_recent(recent_data, active_match_id)

            # если матч уже попал в recent stats, считаем его завершённым
            if match_stats:
                text = format_finish_message(nickname, active_match_id, match_stats)

                try:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    logger.warning("Failed to send finish message to chat %s: %s", chat_id, e)

                TRACKED_PLAYERS[chat_id]["last_match_id"] = active_match_id
                TRACKED_PLAYERS[chat_id]["active_match_id"] = ""
                TRACKED_PLAYERS[chat_id]["match_started_sent"] = "0"
                continue

        # первый запуск — просто сохраняем последний матч
        if not old_last_match_id:
            TRACKED_PLAYERS[chat_id]["last_match_id"] = new_last_match_id


# =========================
# MAIN
# =========================
def main():
    if not TG_BOT_TOKEN:
        print("Нет TG_BOT_TOKEN в Variables.")
        return

    if not FACEIT_API_KEY:
        print("Нет FACEIT_API_KEY в Variables.")
        return

    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trackfull", trackfull_command))
    app.add_handler(CommandHandler("untrackfull", untrackfull_command))
    app.add_handler(CommandHandler("trackstatus", trackstatus_command))

    # каждые 45 секунд проверяем
    app.job_queue.run_repeating(track_matches_job, interval=45, first=10)

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()



