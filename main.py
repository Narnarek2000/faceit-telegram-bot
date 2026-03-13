import os
import sqlite3
import logging
from typing import Optional, Dict, Tuple

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================
# CONFIG
# =========================
TG_BOT_TOKEN = "8692329888:AAGh-uUzW9z4HHVoVnenhRiXjM9aiAIL2s0"
FACEIT_API_KEY = "6dc92495-d0e2-45f1-a658-d52b02229bfb"


BASE_URL = "https://open.faceit.com/data/v4"
REQUEST_TIMEOUT = 20
DB_PATH = "trackers.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# chat_id -> {
#   "player_id1": {"nickname": "...", "last_match_id": "...", "active_match_id": "..."},
#   "player_id2": {...}
# }
TRACKED_PLAYERS: Dict[int, Dict[str, Dict[str, str]]] = {}


# =========================
# DATABASE
# =========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_players (
            chat_id INTEGER NOT NULL,
            player_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            last_match_id TEXT DEFAULT '',
            active_match_id TEXT DEFAULT '',
            PRIMARY KEY (chat_id, player_id)
        )
    """)
    conn.commit()
    conn.close()


def load_tracked_players_from_db():
    global TRACKED_PLAYERS
    TRACKED_PLAYERS = {}

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT chat_id, player_id, nickname, last_match_id, active_match_id
        FROM tracked_players
    """)
    rows = cur.fetchall()
    conn.close()

    for chat_id, player_id, nickname, last_match_id, active_match_id in rows:
        if chat_id not in TRACKED_PLAYERS:
            TRACKED_PLAYERS[chat_id] = {}

        TRACKED_PLAYERS[chat_id][player_id] = {
            "nickname": nickname,
            "last_match_id": last_match_id or "",
            "active_match_id": active_match_id or "",
        }


def save_tracked_player(chat_id: int, player_id: str, nickname: str, last_match_id: str = "", active_match_id: str = ""):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO tracked_players
        (chat_id, player_id, nickname, last_match_id, active_match_id)
        VALUES (?, ?, ?, ?, ?)
    """, (chat_id, player_id, nickname, last_match_id, active_match_id))
    conn.commit()
    conn.close()


def update_tracked_player_state(chat_id: int, player_id: str, last_match_id: Optional[str] = None, active_match_id: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if last_match_id is not None and active_match_id is not None:
        cur.execute("""
            UPDATE tracked_players
            SET last_match_id = ?, active_match_id = ?
            WHERE chat_id = ? AND player_id = ?
        """, (last_match_id, active_match_id, chat_id, player_id))
    elif last_match_id is not None:
        cur.execute("""
            UPDATE tracked_players
            SET last_match_id = ?
            WHERE chat_id = ? AND player_id = ?
        """, (last_match_id, chat_id, player_id))
    elif active_match_id is not None:
        cur.execute("""
            UPDATE tracked_players
            SET active_match_id = ?
            WHERE chat_id = ? AND player_id = ?
        """, (active_match_id, chat_id, player_id))

    conn.commit()
    conn.close()


def delete_tracked_player(chat_id: int, player_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM tracked_players
        WHERE chat_id = ? AND player_id = ?
    """, (chat_id, player_id))
    conn.commit()
    conn.close()


def clear_tracked_players(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM tracked_players
        WHERE chat_id = ?
    """, (chat_id,))
    conn.commit()
    conn.close()


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


def get_player_details(player_id: str) -> Tuple[Optional[dict], Optional[str]]:
    return faceit_request(f"/players/{player_id}")


def get_player_stats(player_id: str) -> Tuple[Optional[dict], Optional[str]]:
    return faceit_request(f"/players/{player_id}/stats/cs2")


def get_player_recent_stats(player_id: str, limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    return faceit_request(
        f"/players/{player_id}/games/cs2/stats",
        params={"limit": limit, "offset": 0},
    )


def get_player_history(player_id: str, limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    return faceit_request(
        f"/players/{player_id}/history",
        params={"game": "cs2", "limit": limit, "offset": 0},
    )


def get_match_details(match_id: str) -> Tuple[Optional[dict], Optional[str]]:
    return faceit_request(f"/matches/{match_id}")


# =========================
# HELPERS
# =========================
def safe_get(data: Optional[dict], key: str, default="N/A"):
    if not isinstance(data, dict):
        return default
    value = data.get(key, default)
    if value in ("", None):
        return default
    return value


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


def parse_result(value) -> str:
    s = str(value).strip().lower()
    if s in ("1", "win", "won"):
        return "WIN"
    if s in ("0", "loss", "lose", "lost"):
        return "LOSS"
    return str(value)


def format_percent(value) -> str:
    if value in (None, "", "N/A"):
        return "N/A"
    text = str(value)
    return text if text.endswith("%") else f"{text}%"


def get_lifetime_stats(stats_data: Optional[dict]) -> dict:
    lifetime = stats_data.get("lifetime", {}) if isinstance(stats_data, dict) else {}
    return {
        "matches": safe_get(lifetime, "Matches"),
        "winrate": format_percent(safe_get(lifetime, "Win Rate %")),
        "kd": safe_get(lifetime, "Average K/D Ratio"),
        "hs": format_percent(safe_get(lifetime, "Average Headshots %")),
        "kr": safe_get(lifetime, "Average K/R Ratio"),
        "adr": safe_get(lifetime, "Average ADR"),
    }


def get_cs2_data(details: dict) -> dict:
    return details.get("games", {}).get("cs2", {})


def parse_recent_match(item: dict) -> str:
    stats = item.get("stats", {})

    map_name = safe_get(stats, "Map")
    result = parse_result(safe_get(stats, "Result"))
    kills = safe_get(stats, "Kills", "0")
    deaths = safe_get(stats, "Deaths", "0")
    kd = safe_get(stats, "K/D Ratio")
    hs = format_percent(safe_get(stats, "Headshots %"))
    kr = safe_get(stats, "K/R Ratio")
    score = stats.get("Final Score") or stats.get("Score") or "N/A"

    return (
        f"• {map_name} — {result}\n"
        f"  Score: {score}\n"
        f"  K/D: {kills}/{deaths} ({kd}) | HS: {hs} | K/R: {kr}"
    )


def find_match_stats_in_recent(recent_data: Optional[dict], match_id: str) -> Optional[dict]:
    if not isinstance(recent_data, dict):
        return None

    items = recent_data.get("items", [])
    for item in items:
        current_match_id = item.get("match_id") or item.get("id")
        if current_match_id == match_id:
            return item
    return None


def build_faceit_text(details: dict, stats_data: Optional[dict]) -> str:
    nickname = safe_get(details, "nickname")
    country = safe_get(details, "country")
    cs2 = get_cs2_data(details)

    elo = safe_get(cs2, "faceit_elo")
    level = safe_get(cs2, "skill_level")
    region = safe_get(cs2, "region")

    life = get_lifetime_stats(stats_data)

    return (
        f"🎮 {nickname}\n"
        f"⭐ Level: {level}\n"
        f"🏆 ELO: {elo}\n"
        f"🌍 Region: {region}\n"
        f"🏳️ Country: {country}\n\n"
        f"📊 Matches: {life['matches']}\n"
        f"📈 Winrate: {life['winrate']}\n"
        f"🔫 K/D: {life['kd']}\n"
        f"🎯 HS: {life['hs']}\n"
        f"⚡ K/R: {life['kr']}\n"
        f"💥 ADR: {life['adr']}"
    )


def build_elo_text(details: dict) -> str:
    nickname = safe_get(details, "nickname")
    cs2 = get_cs2_data(details)

    return (
        f"🏆 FACEIT ELO\n\n"
        f"🎮 {nickname}\n"
        f"⭐ Level: {safe_get(cs2, 'skill_level')}\n"
        f"🏆 ELO: {safe_get(cs2, 'faceit_elo')}\n"
        f"🌍 Region: {safe_get(cs2, 'region')}\n"
        f"🏳️ Country: {safe_get(details, 'country')}"
    )


def build_last5_text(details: dict, recent_data: Optional[dict], recent_error: Optional[str]) -> str:
    nickname = safe_get(details, "nickname")

    if recent_error:
        return f"Не удалось получить последние матчи для {nickname}.\n{recent_error}"

    items = recent_data.get("items", []) if isinstance(recent_data, dict) else []
    if not items:
        return f"У игрока {nickname} нет последних матчей или FACEIT их не отдал."

    text = f"🕑 Последние 5 матчей {nickname}\n\n"
    for item in items[:5]:
        text += parse_recent_match(item) + "\n\n"

    return text.strip()


def format_match_found_message(nickname: str, match_id: str, match_details: Optional[dict]) -> str:
    if not isinstance(match_details, dict):
        return (
            f"🔥 {nickname} нашёл матч\n\n"
            f"🎮 Игрок: {nickname}\n"
            f"🆔 Match ID: {match_id}"
        )

    status = safe_get(match_details, "status")
    competition_name = safe_get(match_details, "competition_name")
    region = safe_get(match_details, "region")

    map_name = "N/A"
    voting = match_details.get("voting", {})
    if isinstance(voting, dict):
        map_info = voting.get("map", {})
        if isinstance(map_info, dict):
            map_name = map_info.get("pick", "N/A")

    return (
        f"🔥 {nickname} нашёл матч\n\n"
        f"🎮 Игрок: {nickname}\n"
        f"🆔 Match ID: {match_id}\n"
        f"📍 Status: {status}\n"
        f"🏆 Queue: {competition_name}\n"
        f"🌍 Region: {region}\n"
        f"🗺 Map: {map_name}"
    )


def format_match_finished_message(nickname: str, match_id: str, recent_match_stats: Optional[dict]) -> str:
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
    hs = format_percent(safe_get(stats, "Headshots %"))
    kr = safe_get(stats, "K/R Ratio")
    score = stats.get("Final Score") or stats.get("Score") or "N/A"

    result_emoji = "🟢" if result == "WIN" else ("🔴" if result == "LOSS" else "⚪")

    return (
        f"{result_emoji} Матч {nickname} завершён\n\n"
        f"🎮 Игрок: {nickname}\n"
        f"🆔 Match ID: {match_id}\n"
        f"🗺 Map: {map_name}\n"
        f"🏁 Result: {result}\n"
        f"📈 Score: {score}\n"
        f"🔫 K/D: {kills}/{deaths} ({kd})\n"
        f"🎯 HS: {hs}\n"
        f"⚡ K/R: {kr}"
    )


def load_player_full(nickname: str, recent_limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    player, error = search_player(nickname)
    if error:
        return None, error

    player_id = extract_player_id(player)
    if not player_id:
        return None, "Не удалось получить player_id."

    details, details_error = get_player_details(player_id)
    if details_error:
        return None, details_error

    stats_data, _ = get_player_stats(player_id)
    recent_data, recent_error = get_player_recent_stats(player_id, recent_limit)
    history_data, _ = get_player_history(player_id, 1)

    return {
        "player_id": player_id,
        "details": details,
        "stats": stats_data,
        "recent": recent_data,
        "recent_error": recent_error,
        "history": history_data,
    }, None


# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Privet 👋\n\n"
        "Команды:\n"
        "/faceit nickname — общая стата\n"
        "/last5 nickname — последние 5 матчей\n"
        "/elo nickname — elo / level / region\n"
        "/trackfull nickname — добавить игрока в слежку\n"
        "/untrackfull nickname — убрать игрока из слежки\n"
        "/tracklist — список отслеживаемых игроков\n"
        "/cleartrack — очистить весь список\n\n"
        "После рестарта список подтянется из базы."
    )
    await update.message.reply_text(text)


async def faceit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /faceit nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Загружаю статистику...")

    player_data, error = load_player_full(nickname, recent_limit=5)
    if error:
        await msg.edit_text(error)
        return

    text = build_faceit_text(player_data["details"], player_data["stats"])
    await msg.edit_text(text)


async def last5_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /last5 nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Загружаю последние 5 матчей...")

    player_data, error = load_player_full(nickname, recent_limit=5)
    if error:
        await msg.edit_text(error)
        return

    text = build_last5_text(
        player_data["details"],
        player_data["recent"],
        player_data["recent_error"],
    )
    await msg.edit_text(text)


async def elo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /elo nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Загружаю elo...")

    player_data, error = load_player_full(nickname, recent_limit=1)
    if error:
        await msg.edit_text(error)
        return

    text = build_elo_text(player_data["details"])
    await msg.edit_text(text)


async def trackfull_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /trackfull nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Ищу игрока и добавляю в слежку...")

    player_data, error = load_player_full(nickname, recent_limit=1)
    if error:
        await msg.edit_text(error)
        return

    details = player_data["details"]
    player_id = player_data["player_id"]
    history = player_data["history"]

    last_match_id = extract_last_match_id(history) or ""
    chat_id = update.effective_chat.id
    found_nick = safe_get(details, "nickname", nickname)

    if chat_id not in TRACKED_PLAYERS:
        TRACKED_PLAYERS[chat_id] = {}

    TRACKED_PLAYERS[chat_id][player_id] = {
        "nickname": found_nick,
        "last_match_id": last_match_id,
        "active_match_id": "",
    }

    save_tracked_player(
        chat_id=chat_id,
        player_id=player_id,
        nickname=found_nick,
        last_match_id=last_match_id,
        active_match_id=""
    )

    total = len(TRACKED_PLAYERS[chat_id])

    await msg.edit_text(
        f"👀 Добавил {found_nick} в слежку.\n\n"
        f"Сейчас отслеживаю игроков: {total}"
    )


async def untrackfull_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /untrackfull nickname")
        return

    nickname = " ".join(context.args).strip().lower()
    chat_id = update.effective_chat.id

    if chat_id not in TRACKED_PLAYERS or not TRACKED_PLAYERS[chat_id]:
        await update.message.reply_text("Список слежки пуст.")
        return

    found_player_id = None
    found_nick = None

    for player_id, data in TRACKED_PLAYERS[chat_id].items():
        if data["nickname"].lower() == nickname:
            found_player_id = player_id
            found_nick = data["nickname"]
            break

    if not found_player_id:
        await update.message.reply_text("Такого игрока нет в списке слежки.")
        return

    del TRACKED_PLAYERS[chat_id][found_player_id]
    delete_tracked_player(chat_id, found_player_id)

    if not TRACKED_PLAYERS[chat_id]:
        del TRACKED_PLAYERS[chat_id]

    await update.message.reply_text(f"🛑 Убрал {found_nick} из слежки.")


async def tracklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in TRACKED_PLAYERS or not TRACKED_PLAYERS[chat_id]:
        await update.message.reply_text("Список слежки пуст.")
        return

    text = "👀 Отслеживаемые игроки:\n\n"
    for _, data in TRACKED_PLAYERS[chat_id].items():
        text += (
            f"🎮 {data['nickname']}\n"
            f"🧩 Last match: {data.get('last_match_id', 'N/A')}\n"
            f"🔥 Active match: {data.get('active_match_id', '') or 'нет'}\n\n"
        )

    await update.message.reply_text(text.strip())


async def cleartrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id in TRACKED_PLAYERS:
        count = len(TRACKED_PLAYERS[chat_id])
        del TRACKED_PLAYERS[chat_id]
        clear_tracked_players(chat_id)
        await update.message.reply_text(f"🧹 Очистил список слежки. Удалено игроков: {count}")
    else:
        await update.message.reply_text("Список слежки и так пуст.")


# =========================
# BACKGROUND TRACKER
# =========================
async def track_matches_job(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, players in list(TRACKED_PLAYERS.items()):
        for player_id, data in list(players.items()):
            nickname = data["nickname"]
            old_last_match_id = data.get("last_match_id", "")
            active_match_id = data.get("active_match_id", "")

            history, history_error = get_player_history(player_id, limit=1)
            if history_error:
                logger.warning("History error for %s: %s", nickname, history_error)
                continue

            new_last_match_id = extract_last_match_id(history)
            if not new_last_match_id:
                continue

            if old_last_match_id and new_last_match_id != old_last_match_id and not active_match_id:
                TRACKED_PLAYERS[chat_id][player_id]["active_match_id"] = new_last_match_id
                update_tracked_player_state(chat_id, player_id, active_match_id=new_last_match_id)

                match_details, _ = get_match_details(new_last_match_id)
                text = format_match_found_message(nickname, new_last_match_id, match_details)

                try:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    logger.warning("Failed to send match found message to chat %s: %s", chat_id, e)

                continue

            if active_match_id:
                recent_data, recent_error = get_player_recent_stats(player_id, limit=5)
                if recent_error:
                    logger.warning("Recent stats error for %s: %s", nickname, recent_error)
                    continue

                match_stats = find_match_stats_in_recent(recent_data, active_match_id)
                if match_stats:
                    text = format_match_finished_message(nickname, active_match_id, match_stats)

                    try:
                        await context.bot.send_message(chat_id=chat_id, text=text)
                    except Exception as e:
                        logger.warning("Failed to send finished match message to chat %s: %s", chat_id, e)

                    TRACKED_PLAYERS[chat_id][player_id]["last_match_id"] = active_match_id
                    TRACKED_PLAYERS[chat_id][player_id]["active_match_id"] = ""
                    update_tracked_player_state(
                        chat_id,
                        player_id,
                        last_match_id=active_match_id,
                        active_match_id=""
                    )
                    continue

            if not old_last_match_id:
                TRACKED_PLAYERS[chat_id][player_id]["last_match_id"] = new_last_match_id
                update_tracked_player_state(chat_id, player_id, last_match_id=new_last_match_id)


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

    init_db()
    load_tracked_players_from_db()

    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("faceit", faceit_command))
    app.add_handler(CommandHandler("last5", last5_command))
    app.add_handler(CommandHandler("elo", elo_command))
    app.add_handler(CommandHandler("trackfull", trackfull_command))
    app.add_handler(CommandHandler("untrackfull", untrackfull_command))
    app.add_handler(CommandHandler("tracklist", tracklist_command))
    app.add_handler(CommandHandler("cleartrack", cleartrack_command))

    app.job_queue.run_repeating(track_matches_job, interval=45, first=10)

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
