import logging
import time
from typing import Optional, Dict, Any, Tuple

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================
# SETTINGS
# =========================================
TG_BOT_TOKEN = "8692329888:AAGh-uUzW9z4HHVoVnenhRiXjM9aiAIL2s0"
FACEIT_API_KEY = "6dc92495-d0e2-45f1-a658-d52b02229bfb"


BASE_URL = "https://open.faceit.com/data/v4"
REQUEST_TIMEOUT = 20

# Сюда бот временно сохраняет отслеживание игрока:
# { chat_id: { "nickname": "NaPi", "player_id": "...", "last_match_id": "..." } }
WATCHLIST: Dict[int, Dict[str, str]] = {}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================================
# FACEIT API
# =========================================
def faceit_request(path: str, params: Optional[dict] = None) -> Tuple[Optional[dict], Optional[str]]:
    """
    FACEIT docs указывают API keys для доступа к FACEIT APIs.
    Для совместимости пробуем 2 варианта заголовков.
    """
    url = f"{BASE_URL}{path}"

    headers_variants = [
        {
            "Authorization": f"Bearer {FACEIT_API_KEY}",
            "Accept": "application/json",
        },
        {
            "Authorization": f"Bearer {FACEIT_API_KEY}",
        },
        {
            "x-api-key": FACEIT_API_KEY,
            "Accept": "application/json",
        },
    ]

    last_error = "Unknown FACEIT API error"

    for headers in headers_variants:
        try:
            response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)

            if response.status_code in (401, 403):
                last_error = f"Authorization error {response.status_code}: {response.text[:300]}"
                continue

            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                last_error = f"Non-JSON response {response.status_code}: {response.text[:300]}"
                continue

            data = response.json()

            if response.status_code >= 400:
                return None, f"FACEIT error {response.status_code}: {data}"

            return data, None

        except requests.RequestException as e:
            last_error = f"Request error: {e}"

    return None, last_error


def search_player(nickname: str) -> Tuple[Optional[dict], Optional[str]]:
    # Docs: /search/players with nickname + optional game. 
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
    # Docs: /players/{player_id}
    return faceit_request(f"/players/{player_id}")


def get_player_stats(player_id: str) -> Tuple[Optional[dict], Optional[str]]:
    # Docs: /players/{player_id}/stats/{game_id}
    return faceit_request(f"/players/{player_id}/stats/cs2")


def get_player_recent_stats(player_id: str, limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    # Docs describe player statistics for a given amount of matches
    return faceit_request(
        f"/players/{player_id}/games/cs2/stats",
        params={"limit": limit, "offset": 0},
    )


def get_player_matches(player_id: str, limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    # Docs: retrieve all matches of a player
    return faceit_request(
        f"/players/{player_id}/history",
        params={"game": "cs2", "limit": limit, "offset": 0},
    )


# =========================================
# HELPERS
# =========================================
def safe_get(d: Optional[dict], key: str, default="N/A"):
    if not isinstance(d, dict):
        return default
    value = d.get(key, default)
    if value in ("", None):
        return default
    return value


def get_player_id(player_data: dict) -> Optional[str]:
    return (
        player_data.get("player_id")
        or player_data.get("guid")
        or player_data.get("id")
    )


def get_cs2_data(details: dict) -> dict:
    return details.get("games", {}).get("cs2", {})


def format_percent(value: str) -> str:
    if value in ("N/A", "", None):
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


def parse_result(result_value):
    value = str(result_value).strip().lower()
    if value in ("1", "win", "won"):
        return "WIN"
    if value in ("0", "loss", "lose", "lost"):
        return "LOSS"
    return str(result_value)


def parse_recent_match(item: dict) -> str:
    stats = item.get("stats", {})
    map_name = safe_get(stats, "Map", "Unknown map")
    result = parse_result(safe_get(stats, "Result", "N/A"))
    kills = safe_get(stats, "Kills", "0")
    deaths = safe_get(stats, "Deaths", "0")
    kd = safe_get(stats, "K/D Ratio", "N/A")
    hs = format_percent(safe_get(stats, "Headshots %", "N/A"))
    kr = safe_get(stats, "K/R Ratio", "N/A")
    score = stats.get("Final Score") or stats.get("Score") or "N/A"

    return (
        f"• {map_name} — {result}\n"
        f"  Score: {score}\n"
        f"  K/D: {kills}/{deaths} ({kd}) | HS: {hs} | K/R: {kr}"
    )


def parse_map_segments(stats_data: Optional[dict]) -> list:
    if not isinstance(stats_data, dict):
        return []
    segments = stats_data.get("segments", [])
    maps = []

    for seg in segments:
        label = seg.get("label") or seg.get("mode") or seg.get("type") or ""
        stats = seg.get("stats", {})

        # В разных ответах FACEIT структура может отличаться.
        map_name = (
            seg.get("label")
            or seg.get("mode")
            or stats.get("Map")
            or seg.get("name")
            or ""
        )

        matches = stats.get("Matches")
        wr = stats.get("Win Rate %")
        kd = stats.get("Average K/D Ratio")
        hs = stats.get("Average Headshots %")
        if map_name and matches not in (None, "", "0"):
            maps.append({
                "map": map_name,
                "matches": matches,
                "winrate": wr,
                "kd": kd,
                "hs": hs,
                "label": label,
            })

    return maps


def build_keyboard(nickname: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("📊 Stats", callback_data=f"stats|{nickname}"),
            InlineKeyboardButton("🕑 Last", callback_data=f"last|{nickname}"),
        ],
        [
            InlineKeyboardButton("🏆 Elo", callback_data=f"elo|{nickname}"),
            InlineKeyboardButton("🗺 Maps", callback_data=f"maps|{nickname}"),
        ],
        [
            InlineKeyboardButton("👀 Watch", callback_data=f"watch|{nickname}"),
            InlineKeyboardButton("🛑 Stop watch", callback_data=f"unwatch|{nickname}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def trim_text(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[:limit]


# =========================================
# LOAD FULL PLAYER DATA
# =========================================
def load_player_full(nickname: str, recent_limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    search_data, error = search_player(nickname)
    if error:
        return None, error

    player_id = get_player_id(search_data)
    if not player_id:
        return None, "Не удалось получить player_id."

    details, error = get_player_details(player_id)
    if error:
        return None, f"Не удалось получить details: {error}"

    stats_data, stats_error = get_player_stats(player_id)
    recent_data, recent_error = get_player_recent_stats(player_id, recent_limit)
    history_data, history_error = get_player_matches(player_id, 1)

    return {
        "player_id": player_id,
        "search": search_data,
        "details": details,
        "stats": stats_data,
        "stats_error": stats_error,
        "recent": recent_data,
        "recent_error": recent_error,
        "history": history_data,
        "history_error": history_error,
    }, None


# =========================================
# TEXT BUILDERS
# =========================================
def build_player_summary(details: dict, stats_data: Optional[dict]) -> str:
    nickname = safe_get(details, "nickname")
    country = safe_get(details, "country")
    avatar = safe_get(details, "avatar", "")
    cs2 = get_cs2_data(details)

    elo = safe_get(cs2, "faceit_elo")
    level = safe_get(cs2, "skill_level")
    region = safe_get(cs2, "region")

    lifetime = get_lifetime_stats(stats_data)

    text = (
        f"🎮 {nickname}\n"
        f"⭐ Level: {level}\n"
        f"🏆 ELO: {elo}\n"
        f"🌍 Region: {region}\n"
        f"🏳️ Country: {country}\n\n"
        f"📊 Matches: {lifetime['matches']}\n"
        f"📈 Winrate: {lifetime['winrate']}\n"
        f"🔫 K/D: {lifetime['kd']}\n"
        f"🎯 HS: {lifetime['hs']}\n"
        f"⚡ K/R: {lifetime['kr']}\n"
        f"💥 ADR: {lifetime['adr']}"
    )

    if avatar and avatar != "N/A":
        text += "\n\n📷 Avatar loaded above"

    return text


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


def build_last_text(details: dict, recent_data: Optional[dict], recent_error: Optional[str]) -> str:
    nickname = safe_get(details, "nickname")
    if recent_error:
        return f"Не удалось получить последние матчи для {nickname}.\n{recent_error}"

    items = recent_data.get("items", []) if isinstance(recent_data, dict) else []
    if not items:
        return f"У игрока {nickname} нет последних матчей или FACEIT их не отдал."

    text = f"🕑 Последние матчи {nickname}\n\n"
    for item in items[:5]:
        text += parse_recent_match(item) + "\n\n"
    return text.strip()


def build_compare_text(details1: dict, stats1: Optional[dict], details2: dict, stats2: Optional[dict]) -> str:
    cs2_1 = get_cs2_data(details1)
    cs2_2 = get_cs2_data(details2)

    life1 = get_lifetime_stats(stats1)
    life2 = get_lifetime_stats(stats2)

    n1 = safe_get(details1, "nickname")
    n2 = safe_get(details2, "nickname")

    return (
        f"⚔️ Сравнение игроков\n\n"
        f"🎮 {n1} vs {n2}\n\n"
        f"⭐ Level: {safe_get(cs2_1, 'skill_level')} vs {safe_get(cs2_2, 'skill_level')}\n"
        f"🏆 ELO: {safe_get(cs2_1, 'faceit_elo')} vs {safe_get(cs2_2, 'faceit_elo')}\n"
        f"📊 Matches: {life1['matches']} vs {life2['matches']}\n"
        f"📈 Winrate: {life1['winrate']} vs {life2['winrate']}\n"
        f"🔫 K/D: {life1['kd']} vs {life2['kd']}\n"
        f"🎯 HS: {life1['hs']} vs {life2['hs']}\n"
        f"⚡ K/R: {life1['kr']} vs {life2['kr']}\n"
        f"💥 ADR: {life1['adr']} vs {life2['adr']}"
    )


def build_mapstats_text(details: dict, stats_data: Optional[dict]) -> str:
    nickname = safe_get(details, "nickname")
    maps = parse_map_segments(stats_data)

    if not maps:
        return (
            f"🗺 Статистика по картам для {nickname}\n\n"
            f"FACEIT не отдал map segments в этом ответе."
        )

    # сортируем по числу матчей
    def to_int(v):
        try:
            return int(str(v))
        except Exception:
            return 0

    maps = sorted(maps, key=lambda x: to_int(x["matches"]), reverse=True)

    text = f"🗺 Лучшие карты {nickname}\n\n"
    for m in maps[:10]:
        text += (
            f"• {m['map']}\n"
            f"  Matches: {m['matches']} | Winrate: {format_percent(m['winrate'])} | "
            f"K/D: {m['kd']} | HS: {format_percent(m['hs'])}\n\n"
        )
    return text.strip()


# =========================================
# PHOTO SEND
# =========================================
async def send_or_edit_with_avatar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    nickname: str,
    avatar_url: str = "",
    edit_message: bool = False,
):
    text = trim_text(text)
    keyboard = build_keyboard(nickname)

    if update.callback_query:
        query = update.callback_query
        try:
            if avatar_url and avatar_url != "N/A":
                try:
                    await query.message.reply_photo(
                        photo=avatar_url,
                        caption=text,
                        reply_markup=keyboard,
                    )
                    await query.answer()
                    return
                except Exception:
                    pass

            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer()
            return
        except Exception:
            # запасной вариант
            await query.message.reply_text(text=text, reply_markup=keyboard)
            await query.answer()
            return

    msg = update.message
    if avatar_url and avatar_url != "N/A":
        try:
            await msg.reply_photo(
                photo=avatar_url,
                caption=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    await msg.reply_text(text, reply_markup=keyboard)


# =========================================
# WATCH / TRACK
# =========================================
def extract_last_match_id(history_data: Optional[dict]) -> Optional[str]:
    if not isinstance(history_data, dict):
        return None
    items = history_data.get("items", [])
    if not items:
        return None
    first = items[0]
    return first.get("match_id") or first.get("id")


async def watch_job(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, data in list(WATCHLIST.items()):
        nickname = data["nickname"]
        player_id = data["player_id"]
        old_match_id = data.get("last_match_id")

        history_data, history_error = get_player_matches(player_id, 1)
        if history_error:
            continue

        new_match_id = extract_last_match_id(history_data)
        if not new_match_id:
            continue

        if old_match_id and new_match_id != old_match_id:
            # новый матч найден
            WATCHLIST[chat_id]["last_match_id"] = new_match_id

            details, _ = get_player_details(player_id)
            recent, _ = get_player_recent_stats(player_id, 1)

            nickname_real = safe_get(details or {}, "nickname", nickname)
            avatar = safe_get(details or {}, "avatar", "")
            text = build_last_text(details or {"nickname": nickname_real}, recent, None)

            try:
                if avatar and avatar != "N/A":
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=avatar,
                        caption=f"🔥 Новый матч у {nickname_real}\n\n{text}",
                        reply_markup=build_keyboard(nickname_real),
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=trim_text(f"🔥 Новый матч у {nickname_real}\n\n{text}"),
                        reply_markup=build_keyboard(nickname_real),
                    )
            except Exception as e:
                logger.warning("Failed to notify chat %s: %s", chat_id, e)

        elif not old_match_id and new_match_id:
            WATCHLIST[chat_id]["last_match_id"] = new_match_id


# =========================================
# COMMANDS
# =========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Privet 👋\n\n"
        "Команды:\n"
        "/faceit nickname — общая стата\n"
        "/last nickname — последние матчи\n"
        "/compare nick1 nick2 — сравнение\n"
        "/elo nickname — elo / level / region\n"
        "/mapstats nickname — карты\n"
        "/watch nickname — следить за новым матчем\n"
        "/unwatch — остановить слежение\n\n"
        "Примеры:\n"
        "/faceit NaPi\n"
        "/last s1mple\n"
        "/compare s1mple donk\n"
        "/elo NaPi\n"
        "/mapstats NaPi"
    )
    await update.message.reply_text(text)


async def faceit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /faceit nickname")
        return

    nickname = " ".join(context.args).strip()
    loading = await update.message.reply_text("Ищу игрока...")

    player_data, error = load_player_full(nickname, recent_limit=5)
    if error:
        await loading.edit_text(error)
        return

    details = player_data["details"]
    stats_data = player_data["stats"]
    avatar = safe_get(details, "avatar", "")
    text = build_player_summary(details, stats_data)

    try:
        await loading.delete()
    except Exception:
        pass

    await send_or_edit_with_avatar(update, context, text, safe_get(details, "nickname", nickname), avatar)


async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /last nickname")
        return

    nickname = " ".join(context.args).strip()
    loading = await update.message.reply_text("Загружаю последние матчи...")

    player_data, error = load_player_full(nickname, recent_limit=5)
    if error:
        await loading.edit_text(error)
        return

    details = player_data["details"]
    recent = player_data["recent"]
    recent_error = player_data["recent_error"]
    avatar = safe_get(details, "avatar", "")
    text = build_last_text(details, recent, recent_error)

    try:
        await loading.delete()
    except Exception:
        pass

    await send_or_edit_with_avatar(update, context, text, safe_get(details, "nickname", nickname), avatar)


async def elo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /elo nickname")
        return

    nickname = " ".join(context.args).strip()
    player_data, error = load_player_full(nickname, recent_limit=1)
    if error:
        await update.message.reply_text(error)
        return

    details = player_data["details"]
    avatar = safe_get(details, "avatar", "")
    text = build_elo_text(details)

    await send_or_edit_with_avatar(update, context, text, safe_get(details, "nickname", nickname), avatar)


async def mapstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /mapstats nickname")
        return

    nickname = " ".join(context.args).strip()
    player_data, error = load_player_full(nickname, recent_limit=1)
    if error:
        await update.message.reply_text(error)
        return

    details = player_data["details"]
    stats_data = player_data["stats"]
    avatar = safe_get(details, "avatar", "")
    text = build_mapstats_text(details, stats_data)

    await send_or_edit_with_avatar(update, context, text, safe_get(details, "nickname", nickname), avatar)


async def compare_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Напиши так: /compare nick1 nick2")
        return

    nick1 = context.args[0].strip()
    nick2 = context.args[1].strip()

    msg = await update.message.reply_text("Сравниваю игроков...")

    p1, error1 = load_player_full(nick1, recent_limit=1)
    if error1:
        await msg.edit_text(f"Ошибка с первым игроком ({nick1}):\n{error1}")
        return

    p2, error2 = load_player_full(nick2, recent_limit=1)
    if error2:
        await msg.edit_text(f"Ошибка со вторым игроком ({nick2}):\n{error2}")
        return

    text = build_compare_text(
        p1["details"], p1["stats"],
        p2["details"], p2["stats"],
    )

    await msg.edit_text(trim_text(text))


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /watch nickname")
        return

    nickname = " ".join(context.args).strip()
    player_data, error = load_player_full(nickname, recent_limit=1)
    if error:
        await update.message.reply_text(error)
        return

    details = player_data["details"]
    player_id = player_data["player_id"]
    history = player_data["history"]
    chat_id = update.effective_chat.id
    last_match_id = extract_last_match_id(history)

    WATCHLIST[chat_id] = {
        "nickname": safe_get(details, "nickname", nickname),
        "player_id": player_id,
        "last_match_id": last_match_id or "",
    }

    await update.message.reply_text(
        f"👀 Слежу за игроком {safe_get(details, 'nickname', nickname)}.\n"
        f"Когда появится новый матч, бот напишет сюда."
    )


async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in WATCHLIST:
        nickname = WATCHLIST[chat_id]["nickname"]
        del WATCHLIST[chat_id]
        await update.message.reply_text(f"🛑 Слежение за {nickname} остановлено.")
    else:
        await update.message.reply_text("Сейчас ни за кем не слежу в этом чате.")


# =========================================
# CALLBACKS
# =========================================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        action, nickname = data.split("|", 1)
    except ValueError:
        await query.edit_message_text("Некорректная callback data.")
        return

    if action == "unwatch":
        chat_id = query.message.chat_id
        if chat_id in WATCHLIST:
            nick = WATCHLIST[chat_id]["nickname"]
            del WATCHLIST[chat_id]
            await query.message.reply_text(f"🛑 Слежение за {nick} остановлено.")
        else:
            await query.message.reply_text("Сейчас ни за кем не слежу.")
        return

    player_data, error = load_player_full(nickname, recent_limit=5)
    if error:
        await query.message.reply_text(error)
        return

    details = player_data["details"]
    stats_data = player_data["stats"]
    recent = player_data["recent"]
    recent_error = player_data["recent_error"]
    history = player_data["history"]
    player_id = player_data["player_id"]
    avatar = safe_get(details, "avatar", "")
    real_nick = safe_get(details, "nickname", nickname)

    if action == "stats":
        text = build_player_summary(details, stats_data)
    elif action == "last":
        text = build_last_text(details, recent, recent_error)
    elif action == "elo":
        text = build_elo_text(details)
    elif action == "maps":
        text = build_mapstats_text(details, stats_data)
    elif action == "watch":
        chat_id = query.message.chat_id
        WATCHLIST[chat_id] = {
            "nickname": real_nick,
            "player_id": player_id,
            "last_match_id": extract_last_match_id(history) or "",
        }
        text = f"👀 Слежу за {real_nick}. Когда будет новый матч — бот напишет сюда."
    else:
        text = "Неизвестное действие."

    await send_or_edit_with_avatar(update, context, text, real_nick, avatar, edit_message=True)


# =========================================
# MAIN
# =========================================
def main():
    if TG_BOT_TOKEN == "PASTE_YOUR_TELEGRAM_BOT_TOKEN":
        print("Вставь TG_BOT_TOKEN в код.")
        return

    if FACEIT_API_KEY == "PASTE_YOUR_FACEIT_SERVER_SIDE_API_KEY":
        print("Вставь FACEIT_API_KEY в код.")
        return

    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("faceit", faceit_command))
    app.add_handler(CommandHandler("last", last_command))
    app.add_handler(CommandHandler("compare", compare_command))
    app.add_handler(CommandHandler("elo", elo_command))
    app.add_handler(CommandHandler("mapstats", mapstats_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("unwatch", unwatch_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    # каждые 90 секунд проверяет новый матч
    app.job_queue.run_repeating(watch_job, interval=90, first=15)

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
