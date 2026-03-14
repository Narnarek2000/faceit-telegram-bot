import os
import sqlite3
import logging
from collections import defaultdict
from typing import Optional, Dict, Tuple, List

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# CONFIG
# =========================
TG_BOT_TOKEN = "8692329888:AAGh-uUzW9z4HHVoVnenhRiXjM9aiAIL2s0"
FACEIT_API_KEY = "6dc92495-d0e2-45f1-a658-d52b02229bfb"

BASE_URL = "https://open.faceit.com/data/v4"
REQUEST_TIMEOUT = 20
DB_PATH = "/data/faceit_bot.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# chat_id -> {
#   player_id: {
#       "nickname": str,
#       "last_match_id": str,
#       "active_match_id": str,
#       "last_known_elo": str
#   }
# }
TRACKED_PLAYERS: Dict[int, Dict[str, Dict[str, str]]] = {}


# =========================
# DATABASE
# =========================
def get_db_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_players (
            chat_id INTEGER NOT NULL,
            player_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            last_match_id TEXT DEFAULT '',
            active_match_id TEXT DEFAULT '',
            last_known_elo TEXT DEFAULT '',
            PRIMARY KEY (chat_id, player_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            chat_id INTEGER NOT NULL,
            player_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            baseline_elo TEXT DEFAULT '',
            PRIMARY KEY (chat_id, player_id)
        )
    """)

    try:
        cur.execute("ALTER TABLE favorites ADD COLUMN baseline_elo TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def load_tracked_players_from_db():
    global TRACKED_PLAYERS
    TRACKED_PLAYERS = {}

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT chat_id, player_id, nickname, last_match_id, active_match_id, last_known_elo
        FROM tracked_players
    """)
    rows = cur.fetchall()
    conn.close()

    for chat_id, player_id, nickname, last_match_id, active_match_id, last_known_elo in rows:
        if chat_id not in TRACKED_PLAYERS:
            TRACKED_PLAYERS[chat_id] = {}

        TRACKED_PLAYERS[chat_id][player_id] = {
            "nickname": nickname,
            "last_match_id": last_match_id or "",
            "active_match_id": active_match_id or "",
            "last_known_elo": last_known_elo or "",
        }


def save_tracked_player(
    chat_id: int,
    player_id: str,
    nickname: str,
    last_match_id: str = "",
    active_match_id: str = "",
    last_known_elo: str = "",
):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO tracked_players
        (chat_id, player_id, nickname, last_match_id, active_match_id, last_known_elo)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (chat_id, player_id, nickname, last_match_id, active_match_id, last_known_elo))
    conn.commit()
    conn.close()


def update_tracked_player_state(
    chat_id: int,
    player_id: str,
    last_match_id: Optional[str] = None,
    active_match_id: Optional[str] = None,
    last_known_elo: Optional[str] = None,
):
    conn = get_db_connection()
    cur = conn.cursor()

    fields = []
    values = []

    if last_match_id is not None:
        fields.append("last_match_id = ?")
        values.append(last_match_id)

    if active_match_id is not None:
        fields.append("active_match_id = ?")
        values.append(active_match_id)

    if last_known_elo is not None:
        fields.append("last_known_elo = ?")
        values.append(last_known_elo)

    if not fields:
        conn.close()
        return

    values.extend([chat_id, player_id])

    cur.execute(f"""
        UPDATE tracked_players
        SET {", ".join(fields)}
        WHERE chat_id = ? AND player_id = ?
    """, values)

    conn.commit()
    conn.close()


def delete_tracked_player(chat_id: int, player_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM tracked_players
        WHERE chat_id = ? AND player_id = ?
    """, (chat_id, player_id))
    conn.commit()
    conn.close()


def clear_tracked_players(chat_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM tracked_players
        WHERE chat_id = ?
    """, (chat_id,))
    conn.commit()
    conn.close()


def add_favorite(chat_id: int, player_id: str, nickname: str, baseline_elo: str = ""):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO favorites (chat_id, player_id, nickname, baseline_elo)
        VALUES (?, ?, ?, ?)
    """, (chat_id, player_id, nickname, baseline_elo))
    conn.commit()
    conn.close()


def remove_favorite(chat_id: int, player_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM favorites
        WHERE chat_id = ? AND player_id = ?
    """, (chat_id, player_id))
    conn.commit()
    conn.close()


def get_favorites(chat_id: int) -> List[Tuple[str, str, str]]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT player_id, nickname, baseline_elo
        FROM favorites
        WHERE chat_id = ?
        ORDER BY nickname COLLATE NOCASE
    """, (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def update_favorite_baseline(chat_id: int, player_id: str, baseline_elo: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE favorites
        SET baseline_elo = ?
        WHERE chat_id = ? AND player_id = ?
    """, (baseline_elo, chat_id, player_id))
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
        params={"nickname": nickname, "game": "cs2", "limit": 10, "offset": 0},
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


def to_float(value, default=0.0) -> float:
    try:
        return float(str(value).replace("%", "").strip())
    except Exception:
        return default


def to_int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


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

def detect_lobby(match_details):

    if not isinstance(match_details, dict):
        return "N/A"

    voting = match_details.get("voting", {})

    if not isinstance(voting, dict):
        return "N/A"

    map_info = voting.get("map", {})

    if not isinstance(map_info, dict):
        return "N/A"

    pick = map_info.get("pick")

    if isinstance(pick, list):
        return ", ".join(pick)

    if pick:
        return str(pick)

    return "N/A"
def get_lobby_average_elo(match_details):

    if not isinstance(match_details, dict):
        return "N/A"

    teams = match_details.get("teams", {})

    elo_list = []

    for team in teams.values():

        roster = team.get("roster", [])

        for player in roster:

            player_id = player.get("player_id")

            if not player_id:
                continue

            details, err = get_player_details(player_id)

            if err or not details:
                continue

            cs2 = get_cs2_data(details)

            elo = safe_get(cs2, "faceit_elo")

            try:
                elo_list.append(int(elo))
            except:
                pass

    if not elo_list:
        return "N/A"

    return int(sum(elo_list) / len(elo_list))    
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


def calculate_form_stats(recent_data: Optional[dict]) -> dict:
    items = recent_data.get("items", []) if isinstance(recent_data, dict) else []

    if not items:
        return {
            "matches": 0,
            "wins": 0,
            "losses": 0,
            "avg_kd": 0.0,
            "avg_hs": 0.0,
            "avg_kr": 0.0,
            "avg_adr": 0.0,
            "score": 0.0,
        }

    wins = 0
    losses = 0
    kd_sum = 0.0
    hs_sum = 0.0
    kr_sum = 0.0
    adr_sum = 0.0

    for item in items:
        stats = item.get("stats", {})
        result = parse_result(safe_get(stats, "Result"))
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1

        kd_sum += to_float(safe_get(stats, "K/D Ratio", 0))
        hs_sum += to_float(safe_get(stats, "Headshots %", 0))
        kr_sum += to_float(safe_get(stats, "K/R Ratio", 0))
        adr_sum += to_float(safe_get(stats, "ADR", 0))

    matches = len(items)
    avg_kd = kd_sum / matches
    avg_hs = hs_sum / matches
    avg_kr = kr_sum / matches
    avg_adr = adr_sum / matches
    score = (wins * 2.0) + avg_kd + (avg_hs / 100.0) + avg_kr

    return {
        "matches": matches,
        "wins": wins,
        "losses": losses,
        "avg_kd": avg_kd,
        "avg_hs": avg_hs,
        "avg_kr": avg_kr,
        "avg_adr": avg_adr,
        "score": score,
    }


def get_live_match_info(player_id: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    history, history_error = get_player_history(player_id, limit=1)
    if history_error:
        return None, None, history_error

    last_match_id = extract_last_match_id(history)
    if not last_match_id:
        return None, None, None

    recent, recent_error = get_player_recent_stats(player_id, limit=5)
    if recent_error:
        return None, None, recent_error

    match_stats = find_match_stats_in_recent(recent, last_match_id)
    if match_stats:
        return None, None, None

    match_details, match_error = get_match_details(last_match_id)
    if match_error:
        return last_match_id, None, match_error

    status = safe_get(match_details, "status")
    if status == "FINISHED":
        return None, None, None

    return last_match_id, match_details, None


def build_match_lobby_text(match_details: Optional[dict]) -> str:
    if not isinstance(match_details, dict):
        return ""

    teams = match_details.get("teams", {})
    if not isinstance(teams, dict) or not teams:
        return ""

    lines = []
    lobby_elo_values = []

    for team_key, team_data in teams.items():
        team_name = safe_get(team_data, "nickname", team_key.upper())
        roster = team_data.get("roster", [])

        team_lines = [f"{team_name}"]
        team_elo_values = []

        for player in roster:
            player_nick = safe_get(player, "nickname", "unknown")
            player_id = player.get("player_id") or player.get("id")

            player_elo = "N/A"
            if player_id:
                details, err = get_player_details(player_id)
                if not err and details:
                    cs2 = get_cs2_data(details)
                    player_elo = safe_get(cs2, "faceit_elo", "N/A")
                    elo_int = to_int(player_elo, 0)
                    if elo_int > 0:
                        team_elo_values.append(elo_int)
                        lobby_elo_values.append(elo_int)

            team_lines.append(f"• {player_nick} — {player_elo}")

        if team_elo_values:
            avg_team_elo = sum(team_elo_values) / len(team_elo_values)
            team_lines.insert(1, f"⭐ Avg team elo: {avg_team_elo:.0f}")

        lines.append("\n".join(team_lines))

    header = ""
    if lobby_elo_values:
        avg_lobby_elo = sum(lobby_elo_values) / len(lobby_elo_values)
        header = f"⭐ Avg lobby elo: {avg_lobby_elo:.0f}\n\n"

    return header + "\n\n".join(lines)


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


def build_form5_text(details: dict, recent_data: Optional[dict], recent_error: Optional[str]) -> str:
    nickname = safe_get(details, "nickname")
    if recent_error:
        return f"Не удалось получить форму за 5 матчей для {nickname}.\n{recent_error}"

    form = calculate_form_stats(recent_data)
    if form["matches"] == 0:
        return f"Нет данных по последним матчам для {nickname}."

    return (
        f"🔥 Форма {nickname} за последние {form['matches']} матчей\n\n"
        f"✅ Wins: {form['wins']}\n"
        f"❌ Losses: {form['losses']}\n"
        f"🔫 Avg K/D: {form['avg_kd']:.2f}\n"
        f"🎯 Avg HS: {form['avg_hs']:.1f}%\n"
        f"⚡ Avg K/R: {form['avg_kr']:.2f}\n"
        f"💥 Avg ADR: {form['avg_adr']:.1f}\n"
        f"📈 Form score: {form['score']:.2f}"
    )


def build_compare_form_text(details1: dict, recent1: Optional[dict], details2: dict, recent2: Optional[dict]) -> str:
    n1 = safe_get(details1, "nickname")
    n2 = safe_get(details2, "nickname")

    cs2_1 = get_cs2_data(details1)
    cs2_2 = get_cs2_data(details2)

    f1 = calculate_form_stats(recent1)
    f2 = calculate_form_stats(recent2)

    return (
        f"⚔️ Сравнение формы за последние 5 матчей\n\n"
        f"🎮 {n1} vs {n2}\n\n"
        f"🏆 ELO: {safe_get(cs2_1, 'faceit_elo')} vs {safe_get(cs2_2, 'faceit_elo')}\n"
        f"⭐ Level: {safe_get(cs2_1, 'skill_level')} vs {safe_get(cs2_2, 'skill_level')}\n"
        f"✅ Wins: {f1['wins']} vs {f2['wins']}\n"
        f"❌ Losses: {f1['losses']} vs {f2['losses']}\n"
        f"🔫 Avg K/D: {f1['avg_kd']:.2f} vs {f2['avg_kd']:.2f}\n"
        f"🎯 Avg HS: {f1['avg_hs']:.1f}% vs {f2['avg_hs']:.1f}%\n"
        f"⚡ Avg K/R: {f1['avg_kr']:.2f} vs {f2['avg_kr']:.2f}\n"
        f"💥 Avg ADR: {f1['avg_adr']:.1f} vs {f2['avg_adr']:.1f}\n"
        f"📈 Form score: {f1['score']:.2f} vs {f2['score']:.2f}"
    )


def build_maps30_text(details: dict, recent30: Optional[dict], recent_error: Optional[str]) -> str:
    nickname = safe_get(details, "nickname")
    if recent_error:
        return f"Не удалось получить карты за 30 матчей для {nickname}.\n{recent_error}"

    items = recent30.get("items", []) if isinstance(recent30, dict) else []
    if not items:
        return f"Нет данных по 30 матчам для {nickname}."

    maps = defaultdict(lambda: {
        "matches": 0,
        "wins": 0,
        "kd_sum": 0.0,
        "hs_sum": 0.0,
        "kr_sum": 0.0,
        "adr_sum": 0.0,
    })

    for item in items:
        stats = item.get("stats", {})
        map_name = safe_get(stats, "Map", "Unknown")
        result = parse_result(safe_get(stats, "Result"))
        kd = to_float(safe_get(stats, "K/D Ratio", 0))
        hs = to_float(safe_get(stats, "Headshots %", 0))
        kr = to_float(safe_get(stats, "K/R Ratio", 0))
        adr = to_float(safe_get(stats, "ADR", 0))

        maps[map_name]["matches"] += 1
        maps[map_name]["kd_sum"] += kd
        maps[map_name]["hs_sum"] += hs
        maps[map_name]["kr_sum"] += kr
        maps[map_name]["adr_sum"] += adr
        if result == "WIN":
            maps[map_name]["wins"] += 1

    ranked = []
    for map_name, data in maps.items():
        matches = data["matches"]
        ranked.append({
            "map": map_name,
            "matches": matches,
            "winrate": (data["wins"] / matches) * 100 if matches else 0.0,
            "avg_kd": data["kd_sum"] / matches if matches else 0.0,
            "avg_hs": data["hs_sum"] / matches if matches else 0.0,
            "avg_kr": data["kr_sum"] / matches if matches else 0.0,
            "avg_adr": data["adr_sum"] / matches if matches else 0.0,
        })

    ranked.sort(key=lambda x: (x["matches"], x["winrate"], x["avg_kd"]), reverse=True)

    text = f"🗺 Лучшие карты {nickname} за последние {len(items)} матчей\n\n"
    for row in ranked[:8]:
        text += (
            f"• {row['map']}\n"
            f"  Matches: {row['matches']} | Winrate: {row['winrate']:.1f}%\n"
            f"  Avg K/D: {row['avg_kd']:.2f} | Avg HS: {row['avg_hs']:.1f}%\n"
            f"  Avg K/R: {row['avg_kr']:.2f} | Avg ADR: {row['avg_adr']:.1f}\n\n"
        )
    return text.strip()


def format_elo_delta(old_elo: str, new_elo: str) -> str:
    old_val = to_int(old_elo, 0)
    new_val = to_int(new_elo, 0)
    diff = new_val - old_val

    if old_val == 0 and new_val == 0:
        return "N/A"
    if diff > 0:
        return f"{old_val} → {new_val} (+{diff})"
    if diff < 0:
        return f"{old_val} → {new_val} ({diff})"
    return f"{old_val} → {new_val} (0)"


def format_match_found_message(nickname: str, match_id: str, match_details: Optional[dict]) -> str:

    if not isinstance(match_details, dict):
        return (
            f"🔥 {nickname} нашёл матч\n\n"
            f"🆔 Match ID: {match_id}"
        )

    competition_name = safe_get(match_details, "competition_name")
    region = safe_get(match_details, "region")
    status = safe_get(match_details, "status")

    # MAP VOTE
    map_name = "N/A"
    voting = match_details.get("voting", {})
    if isinstance(voting, dict):
        map_info = voting.get("map", {})
        if isinstance(map_info, dict):
            pick = map_info.get("pick", "N/A")
            if isinstance(pick, list):
                map_name = ", ".join(pick)
            else:
                map_name = str(pick)

    # LOBBY ELO
    lobby_text = build_match_lobby_text(match_details)

    text = (
        f"🔥 {nickname} нашёл матч\n\n"
        f"🗺 Map vote: {map_name}\n"
        f"🏆 Queue: {competition_name}\n"
        f"🌍 Region: {region}\n"
        f"📍 Status: {status}"
    )

    if lobby_text:
        text += f"\n\n{lobby_text}"

    text += "\n\n👀 Слежение активно\nЯ отправлю результат после окончания матча"

    return text


def format_match_finished_message(
    nickname: str,
    match_id: str,
    recent_match_stats: Optional[dict],
    elo_before: str,
    elo_after: str,
) -> str:
    if not isinstance(recent_match_stats, dict):
        return (
            f"✅ Матч {nickname} завершён\n\n"
            f"🎮 Игрок: {nickname}\n"
            f"🆔 Match ID: {match_id}\n"
            f"🏆 ELO: {format_elo_delta(elo_before, elo_after)}\n"
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
    elo_text = format_elo_delta(elo_before, elo_after)

    return (
        f"{result_emoji} Матч {nickname} завершён\n\n"
        f"🎮 Игрок: {nickname}\n"
        f"🆔 Match ID: {match_id}\n"
        f"🗺 Map: {map_name}\n"
        f"🏁 Result: {result}\n"
        f"📈 Score: {score}\n"
        f"🔫 K/D: {kills}/{deaths} ({kd})\n"
        f"🎯 HS: {hs}\n"
        f"⚡ K/R: {kr}\n"
        f"🏆 ELO: {elo_text}"
    )


def build_player_keyboard(player_id: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("📊 Stats", callback_data=f"stats|{player_id}"),
            InlineKeyboardButton("🔥 Form5", callback_data=f"form5|{player_id}"),
        ],
        [
            InlineKeyboardButton("🕑 Last5", callback_data=f"last5|{player_id}"),
            InlineKeyboardButton("🏆 Elo", callback_data=f"elo|{player_id}"),
        ],
        [
            InlineKeyboardButton("🗺 Maps30", callback_data=f"maps30|{player_id}"),
            InlineKeyboardButton("⭐ Fav+", callback_data=f"favadd|{player_id}"),
        ],
        [
            InlineKeyboardButton("👀 Track", callback_data=f"track|{player_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🟢 Fav Live", callback_data="menu_favlive"),
            InlineKeyboardButton("📈 Fav Gainers", callback_data="menu_favgainers"),
        ],
        [
            InlineKeyboardButton("📉 Fav Losers", callback_data="menu_favlosers"),
            InlineKeyboardButton("👀 Tracklist", callback_data="menu_tracklist"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def load_player_full_by_nick(nickname: str, recent_limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
    player, error = search_player(nickname)
    if error:
        return None, error

    player_id = extract_player_id(player)
    if not player_id:
        return None, "Не удалось получить player_id."

    return load_player_full_by_id(player_id, recent_limit)


def load_player_full_by_id(player_id: str, recent_limit: int = 5) -> Tuple[Optional[dict], Optional[str]]:
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
        "/menu\n"
        "/faceit nickname\n"
        "/last5 nickname\n"
        "/elo nickname\n"
        "/form5 nickname\n"
        "/compareform nick1 nick2\n"
        "/maps30 nickname\n\n"
        "/fav add nickname\n"
        "/fav remove nickname\n"
        "/fav list\n"
        "/favlive\n"
        "/favelo\n"
        "/favkd\n"
        "/favform\n"
        "/favgainers\n"
        "/favlosers\n\n"
        "/trackfull nickname\n"
        "/untrackfull nickname\n"
        "/tracklist\n"
        "/cleartrack"
    )
    await update.message.reply_text(text, reply_markup=build_main_menu_keyboard())



async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = (
        "📚 FACEIT BOT — команды\n\n"

        "🔎 Игроки\n"
        "/faceit nickname — профиль игрока\n"
        "/elo nickname — текущий ELO\n"
        "/last5 nickname — последние 5 матчей\n"
        "/form5 nickname — форма за 5 матчей\n"
        "/compareform nick1 nick2 — сравнить форму\n"
        "/maps30 nickname — лучшие карты\n\n"

        "⭐ Фавориты\n"
        "/fav add nickname — добавить игрока\n"
        "/fav remove nickname — удалить игрока\n"
        "/fav list — список фаворитов\n"
        "/favlive — кто играет сейчас\n"
        "/favelo — рейтинг по elo\n"
        "/favkd — рейтинг по kd\n"
        "/favform — рейтинг по форме\n"
        "/favgainers — рост elo\n"
        "/favlosers — падение elo\n\n"

        "👀 Слежка\n"
        "/trackfull nickname — начать слежку\n"
        "/untrackfull nickname — убрать слежку\n"
        "/tracklist — список слежки\n"
        "/trackstatus nickname — статус слежки\n"
        "/tracklive — кто сейчас в игре\n"
        "/cleartrack — очистить слежку\n\n"

        "⚙️ Другое\n"
        "/menu — главное меню\n"
        "/help — список команд"
    )

    await update.message.reply_text(text)
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выбери действие:",
        reply_markup=build_main_menu_keyboard()
    )


async def faceit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /faceit nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Загружаю статистику...")

    player_data, error = load_player_full_by_nick(nickname, recent_limit=5)
    if error:
        await msg.edit_text(error)
        return

    text = build_faceit_text(player_data["details"], player_data["stats"])
    await msg.edit_text(text, reply_markup=build_player_keyboard(player_data["player_id"]))


async def last5_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /last5 nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Загружаю последние 5 матчей...")

    player_data, error = load_player_full_by_nick(nickname, recent_limit=5)
    if error:
        await msg.edit_text(error)
        return

    text = build_last5_text(player_data["details"], player_data["recent"], player_data["recent_error"])
    await msg.edit_text(text, reply_markup=build_player_keyboard(player_data["player_id"]))


async def elo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /elo nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Загружаю elo...")

    player_data, error = load_player_full_by_nick(nickname, recent_limit=1)
    if error:
        await msg.edit_text(error)
        return

    text = build_elo_text(player_data["details"])
    await msg.edit_text(text, reply_markup=build_player_keyboard(player_data["player_id"]))


async def form5_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /form5 nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Считаю форму за 5 матчей...")

    player_data, error = load_player_full_by_nick(nickname, recent_limit=5)
    if error:
        await msg.edit_text(error)
        return

    text = build_form5_text(player_data["details"], player_data["recent"], player_data["recent_error"])
    await msg.edit_text(text, reply_markup=build_player_keyboard(player_data["player_id"]))


async def compareform_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Напиши так: /compareform nick1 nick2")
        return

    nick1 = context.args[0].strip()
    nick2 = context.args[1].strip()

    msg = await update.message.reply_text("Сравниваю форму игроков...")

    p1, err1 = load_player_full_by_nick(nick1, recent_limit=5)
    if err1:
        await msg.edit_text(f"Ошибка с первым игроком: {err1}")
        return

    p2, err2 = load_player_full_by_nick(nick2, recent_limit=5)
    if err2:
        await msg.edit_text(f"Ошибка со вторым игроком: {err2}")
        return

    text = build_compare_form_text(p1["details"], p1["recent"], p2["details"], p2["recent"])
    await msg.edit_text(text)


async def maps30_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /maps30 nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Собираю карты за 30 матчей...")

    player_data, error = load_player_full_by_nick(nickname, recent_limit=30)
    if error:
        await msg.edit_text(error)
        return

    text = build_maps30_text(player_data["details"], player_data["recent"], player_data["recent_error"])
    await msg.edit_text(text, reply_markup=build_player_keyboard(player_data["player_id"]))


async def fav_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Используй: /fav add nickname | /fav remove nickname | /fav list")
        return

    action = context.args[0].lower()
    chat_id = update.effective_chat.id

    if action == "list":
        favorites = get_favorites(chat_id)
        if not favorites:
            await update.message.reply_text("Список фаворитов пуст.")
            return

        text = "⭐ Фавориты:\n\n"
        for _, nickname, _baseline in favorites:
            text += f"• {nickname}\n"
        await update.message.reply_text(text.strip())
        return

    if len(context.args) < 2:
        await update.message.reply_text("Напиши так: /fav add nickname или /fav remove nickname")
        return

    nickname = " ".join(context.args[1:]).strip()

    if action == "add":
        msg = await update.message.reply_text("Добавляю в фавориты...")
        player_data, error = load_player_full_by_nick(nickname, recent_limit=1)
        if error:
            await msg.edit_text(error)
            return

        player_id = player_data["player_id"]
        found_nick = safe_get(player_data["details"], "nickname", nickname)
        current_elo = safe_get(get_cs2_data(player_data["details"]), "faceit_elo", "")
        add_favorite(chat_id, player_id, found_nick, str(current_elo))
        await msg.edit_text(f"⭐ Добавил {found_nick} в фавориты.")
        return

    if action == "remove":
        favorites = get_favorites(chat_id)
        found_player_id = None
        found_nick = None

        for player_id, nick, _baseline in favorites:
            if nick.lower() == nickname.lower():
                found_player_id = player_id
                found_nick = nick
                break

        if not found_player_id:
            await update.message.reply_text("Такого игрока нет в фаворитах.")
            return

        remove_favorite(chat_id, found_player_id)
        await update.message.reply_text(f"🗑 Убрал {found_nick} из фаворитов.")
        return

    await update.message.reply_text("Используй: /fav add nickname | /fav remove nickname | /fav list")


async def favlive_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    favorites = get_favorites(chat_id)

    if not favorites:
        await update.message.reply_text("Список фаворитов пуст.")
        return

    msg = await update.message.reply_text("Проверяю, кто сейчас в матче...")

    lines = []
    for player_id, nickname, _baseline in favorites:
        match_id, match_details, err = get_live_match_info(player_id)
        if not match_id:
            continue

        status = safe_get(match_details, "status", "N/A")
        queue = safe_get(match_details, "competition_name", "N/A")
        region = safe_get(match_details, "region", "N/A")

        map_name = "N/A"
        voting = match_details.get("voting", {}) if isinstance(match_details, dict) else {}
        if isinstance(voting, dict):
            map_info = voting.get("map", {})
            if isinstance(map_info, dict):
                pick = map_info.get("pick", "N/A")
                if isinstance(pick, list):
                    map_name = ", ".join(pick)
                else:
                    map_name = str(pick)

        lines.append(
            f"🟢 {nickname}\n"
            f"🗺 {map_name}\n"
            f"🏆 {queue}\n"
            f"🌍 {region}\n"
            f"📍 {status}\n"
        )

    if not lines:
        await msg.edit_text("Сейчас никто из фаворитов не играет.")
        return

    text = "🟢 Сейчас в матче:\n\n" + "\n".join(lines)
    await msg.edit_text(text[:4000])


async def favgainers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    favorites = get_favorites(chat_id)

    if not favorites:
        await update.message.reply_text("Список фаворитов пуст.")
        return

    msg = await update.message.reply_text("Считаю рост ELO...")

    rows = []
    for player_id, nickname, baseline_elo in favorites:
        details, err = get_player_details(player_id)
        if err or not details:
            continue

        cs2 = get_cs2_data(details)
        current_elo = to_int(safe_get(cs2, "faceit_elo", 0), 0)
        base_elo = to_int(baseline_elo, 0)

        if base_elo == 0:
            base_elo = current_elo
            update_favorite_baseline(chat_id, player_id, str(current_elo))

        diff = current_elo - base_elo
        rows.append((nickname, base_elo, current_elo, diff))

    if not rows:
        await msg.edit_text("Не удалось загрузить фаворитов.")
        return

    rows.sort(key=lambda x: x[3], reverse=True)

    text = "📈 Fav Gainers\n\n"
    for i, (nickname, base_elo, current_elo, diff) in enumerate(rows, start=1):
        sign = f"+{diff}" if diff > 0 else str(diff)
        text += f"{i}. {nickname} — {base_elo} → {current_elo} ({sign})\n"

    await msg.edit_text(text[:4000])


async def favlosers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    favorites = get_favorites(chat_id)

    if not favorites:
        await update.message.reply_text("Список фаворитов пуст.")
        return

    msg = await update.message.reply_text("Считаю падение ELO...")

    rows = []
    for player_id, nickname, baseline_elo in favorites:
        details, err = get_player_details(player_id)
        if err or not details:
            continue

        cs2 = get_cs2_data(details)
        current_elo = to_int(safe_get(cs2, "faceit_elo", 0), 0)
        base_elo = to_int(baseline_elo, 0)

        if base_elo == 0:
            base_elo = current_elo
            update_favorite_baseline(chat_id, player_id, str(current_elo))

        diff = current_elo - base_elo
        rows.append((nickname, base_elo, current_elo, diff))

    if not rows:
        await msg.edit_text("Не удалось загрузить фаворитов.")
        return

    rows.sort(key=lambda x: x[3])

    text = "📉 Fav Losers\n\n"
    for i, (nickname, base_elo, current_elo, diff) in enumerate(rows, start=1):
        sign = f"+{diff}" if diff > 0 else str(diff)
        text += f"{i}. {nickname} — {base_elo} → {current_elo} ({sign})\n"

    await msg.edit_text(text[:4000])


async def favelo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    favorites = get_favorites(chat_id)
    if not favorites:
        await update.message.reply_text("Список фаворитов пуст.")
        return

    msg = await update.message.reply_text("Сравниваю фаворитов по ELO...")
    rows = []

    for player_id, nick, _baseline in favorites:
        details, err = get_player_details(player_id)
        if err or not details:
            continue
        cs2 = get_cs2_data(details)
        rows.append({
            "nickname": safe_get(details, "nickname", nick),
            "elo": to_int(safe_get(cs2, "faceit_elo", 0), 0),
            "level": safe_get(cs2, "skill_level"),
        })

    if not rows:
        await msg.edit_text("Не удалось загрузить фаворитов.")
        return

    rows.sort(key=lambda x: x["elo"], reverse=True)
    text = "🏆 Фавориты по ELO\n\n"
    for i, row in enumerate(rows, start=1):
        text += f"{i}. {row['nickname']} — {row['elo']} elo | lvl {row['level']}\n"

    await msg.edit_text(text.strip())


async def favkd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    favorites = get_favorites(chat_id)
    if not favorites:
        await update.message.reply_text("Список фаворитов пуст.")
        return

    msg = await update.message.reply_text("Сравниваю фаворитов по avg K/D за 5 матчей...")
    rows = []

    for player_id, nick, _baseline in favorites:
        details, err1 = get_player_details(player_id)
        recent, err2 = get_player_recent_stats(player_id, 5)
        if err1 or err2 or not details:
            continue

        form = calculate_form_stats(recent)
        rows.append({
            "nickname": safe_get(details, "nickname", nick),
            "avg_kd": form["avg_kd"],
            "wins": form["wins"],
            "avg_hs": form["avg_hs"],
        })

    if not rows:
        await msg.edit_text("Не удалось загрузить фаворитов.")
        return

    rows.sort(key=lambda x: (x["avg_kd"], x["wins"], x["avg_hs"]), reverse=True)
    text = "🔫 Фавориты по avg K/D за 5 матчей\n\n"
    for i, row in enumerate(rows, start=1):
        text += f"{i}. {row['nickname']} — K/D {row['avg_kd']:.2f} | Wins {row['wins']} | HS {row['avg_hs']:.1f}%\n"

    await msg.edit_text(text.strip())


async def favform_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    favorites = get_favorites(chat_id)
    if not favorites:
        await update.message.reply_text("Список фаворитов пуст.")
        return

    msg = await update.message.reply_text("Сравниваю фаворитов по форме за 5 матчей...")
    rows = []

    for player_id, nick, _baseline in favorites:
        details, err1 = get_player_details(player_id)
        recent, err2 = get_player_recent_stats(player_id, 5)
        if err1 or err2 or not details:
            continue

        form = calculate_form_stats(recent)
        rows.append({
            "nickname": safe_get(details, "nickname", nick),
            "score": form["score"],
            "wins": form["wins"],
            "avg_kd": form["avg_kd"],
            "avg_hs": form["avg_hs"],
        })

    if not rows:
        await msg.edit_text("Не удалось загрузить фаворитов.")
        return

    rows.sort(key=lambda x: (x["score"], x["wins"], x["avg_kd"]), reverse=True)
    text = "🔥 Фавориты по форме за 5 матчей\n\n"
    for i, row in enumerate(rows, start=1):
        text += f"{i}. {row['nickname']} — score {row['score']:.2f} | Wins {row['wins']} | K/D {row['avg_kd']:.2f} | HS {row['avg_hs']:.1f}%\n"

    await msg.edit_text(text.strip())


async def trackfull_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /trackfull nickname")
        return

    nickname = " ".join(context.args).strip()
    msg = await update.message.reply_text("Ищу игрока и добавляю в слежку...")

    player_data, error = load_player_full_by_nick(nickname, recent_limit=5)
    if error:
        await msg.edit_text(error)
        return

    details = player_data["details"]
    player_id = player_data["player_id"]
    history = player_data["history"]
    recent = player_data["recent"]

    found_nick = safe_get(details, "nickname", nickname)
    current_elo = safe_get(get_cs2_data(details), "faceit_elo", "")
    chat_id = update.effective_chat.id

    last_match_id = extract_last_match_id(history) or ""
    active_match_id = ""

    if last_match_id:
        match_stats = find_match_stats_in_recent(recent, last_match_id)
        if not match_stats:
            match_details, _ = get_match_details(last_match_id)
            status = safe_get(match_details, "status")
            if status != "FINISHED":
                active_match_id = last_match_id

    if chat_id not in TRACKED_PLAYERS:
        TRACKED_PLAYERS[chat_id] = {}

    TRACKED_PLAYERS[chat_id][player_id] = {
        "nickname": found_nick,
        "last_match_id": last_match_id,
        "active_match_id": active_match_id,
        "last_known_elo": str(current_elo),
    }

    save_tracked_player(
        chat_id=chat_id,
        player_id=player_id,
        nickname=found_nick,
        last_match_id=last_match_id,
        active_match_id=active_match_id,
        last_known_elo=str(current_elo),
    )

    total = len(TRACKED_PLAYERS[chat_id])

    if active_match_id:
        match_details, _ = get_match_details(active_match_id)
        text = format_match_found_message(found_nick, active_match_id, match_details)

        await msg.edit_text(
            f"{text}\n\n"
            f"👀 Слежение уже включено.\n"
            f"Сейчас отслеживаю игроков: {total}"
        )
    else:
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
            f"🔥 Active match: {data.get('active_match_id', '') or 'нет'}\n"
            f"🏆 Last known elo: {data.get('last_known_elo', '') or 'N/A'}\n\n"
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
async def trackstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text("Напиши так: /trackstatus nickname")
        return

    nickname = " ".join(context.args).strip().lower()
    chat_id = update.effective_chat.id

    if chat_id not in TRACKED_PLAYERS:
        await update.message.reply_text("Слежка не запущена.")
        return

    for player_id, data in TRACKED_PLAYERS[chat_id].items():

        if data["nickname"].lower() == nickname:

            text = (
                f"📡 Tracking status\n\n"
                f"🎮 Player: {data['nickname']}\n"
                f"🧩 Last match: {data.get('last_match_id', 'N/A')}\n"
                f"🔥 Active match: {data.get('active_match_id') or 'нет'}\n"
                f"🏆 Last known elo: {data.get('last_known_elo', 'N/A')}"
            )

            await update.message.reply_text(text)
            return

    await update.message.reply_text("Игрок не найден в списке слежки.")

# =========================
# BUTTONS
# =========================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    chat_id = query.message.chat_id

    if data == "menu_favlive":
        favorites = get_favorites(chat_id)
        if not favorites:
            await query.edit_message_text("Список фаворитов пуст.", reply_markup=build_main_menu_keyboard())
            return

        lines = []
        for fav_player_id, nickname, _baseline in favorites:
            match_id, match_details, _err = get_live_match_info(fav_player_id)
            if not match_id:
                continue

            status = safe_get(match_details, "status", "N/A")
            queue = safe_get(match_details, "competition_name", "N/A")
            region = safe_get(match_details, "region", "N/A")

            map_name = "N/A"
            voting = match_details.get("voting", {}) if isinstance(match_details, dict) else {}
            if isinstance(voting, dict):
                map_info = voting.get("map", {})
                if isinstance(map_info, dict):
                    pick = map_info.get("pick", "N/A")
                    if isinstance(pick, list):
                        map_name = ", ".join(pick)
                    else:
                        map_name = str(pick)

            lines.append(
                f"🟢 {nickname}\n"
                f"🗺 {map_name}\n"
                f"🏆 {queue}\n"
                f"🌍 {region}\n"
                f"📍 {status}\n"
            )

        if not lines:
            await query.edit_message_text("Сейчас никто из фаворитов не играет.", reply_markup=build_main_menu_keyboard())
            return

        text = "🟢 Сейчас в матче:\n\n" + "\n".join(lines)
        await query.edit_message_text(text[:4000], reply_markup=build_main_menu_keyboard())
        return

    if data == "menu_favgainers":
        favorites = get_favorites(chat_id)
        if not favorites:
            await query.edit_message_text("Список фаворитов пуст.", reply_markup=build_main_menu_keyboard())
            return

        rows = []
        for fav_player_id, nickname, baseline_elo in favorites:
            details, err = get_player_details(fav_player_id)
            if err or not details:
                continue

            cs2 = get_cs2_data(details)
            current_elo = to_int(safe_get(cs2, "faceit_elo", 0), 0)
            base_elo = to_int(baseline_elo, 0)

            if base_elo == 0:
                base_elo = current_elo
                update_favorite_baseline(chat_id, fav_player_id, str(current_elo))

            diff = current_elo - base_elo
            rows.append((nickname, base_elo, current_elo, diff))

        rows.sort(key=lambda x: x[3], reverse=True)

        text = "📈 Fav Gainers\n\n"
        for i, (nickname, base_elo, current_elo, diff) in enumerate(rows, start=1):
            sign = f"+{diff}" if diff > 0 else str(diff)
            text += f"{i}. {nickname} — {base_elo} → {current_elo} ({sign})\n"

        await query.edit_message_text(text[:4000], reply_markup=build_main_menu_keyboard())
        return

    if data == "menu_favlosers":
        favorites = get_favorites(chat_id)
        if not favorites:
            await query.edit_message_text("Список фаворитов пуст.", reply_markup=build_main_menu_keyboard())
            return

        rows = []
        for fav_player_id, nickname, baseline_elo in favorites:
            details, err = get_player_details(fav_player_id)
            if err or not details:
                continue

            cs2 = get_cs2_data(details)
            current_elo = to_int(safe_get(cs2, "faceit_elo", 0), 0)
            base_elo = to_int(baseline_elo, 0)

            if base_elo == 0:
                base_elo = current_elo
                update_favorite_baseline(chat_id, fav_player_id, str(current_elo))

            diff = current_elo - base_elo
            rows.append((nickname, base_elo, current_elo, diff))

        rows.sort(key=lambda x: x[3])

        text = "📉 Fav Losers\n\n"
        for i, (nickname, base_elo, current_elo, diff) in enumerate(rows, start=1):
            sign = f"+{diff}" if diff > 0 else str(diff)
            text += f"{i}. {nickname} — {base_elo} → {current_elo} ({sign})\n"

        await query.edit_message_text(text[:4000], reply_markup=build_main_menu_keyboard())
        return

    if data == "menu_tracklist":
        if chat_id not in TRACKED_PLAYERS or not TRACKED_PLAYERS[chat_id]:
            await query.edit_message_text("Список слежки пуст.", reply_markup=build_main_menu_keyboard())
            return

        text = "👀 Отслеживаемые игроки:\n\n"
        for _, tracked in TRACKED_PLAYERS[chat_id].items():
            text += (
                f"🎮 {tracked['nickname']}\n"
                f"🧩 Last match: {tracked.get('last_match_id', 'N/A')}\n"
                f"🔥 Active match: {tracked.get('active_match_id', '') or 'нет'}\n"
                f"🏆 Last known elo: {tracked.get('last_known_elo', '') or 'N/A'}\n\n"
            )

        await query.edit_message_text(text[:4000], reply_markup=build_main_menu_keyboard())
        return

    try:
        action, player_id = data.split("|", 1)
    except Exception:
        await query.edit_message_text("Ошибка callback data.")
        return

    if action == "favadd":
        details, err = get_player_details(player_id)
        if err or not details:
            await query.message.reply_text("Не удалось добавить в фавориты.")
            return

        nick = safe_get(details, "nickname")
        current_elo = safe_get(get_cs2_data(details), "faceit_elo", "")
        add_favorite(chat_id, player_id, nick, str(current_elo))
        await query.message.reply_text(f"⭐ Добавил {nick} в фавориты.")
        return

    if action == "track":
        details, err1 = get_player_details(player_id)
        history, err2 = get_player_history(player_id, 1)
        recent, _err3 = get_player_recent_stats(player_id, 5)

        if err1 or not details:
            await query.message.reply_text("Не удалось добавить игрока в слежку.")
            return

        nick = safe_get(details, "nickname")
        current_elo = safe_get(get_cs2_data(details), "faceit_elo", "")
        last_match_id = extract_last_match_id(history) if not err2 else ""
        active_match_id = ""

        if last_match_id:
            match_stats = find_match_stats_in_recent(recent, last_match_id)
            if not match_stats:
                match_details, _ = get_match_details(last_match_id)
                status = safe_get(match_details, "status")
                if status != "FINISHED":
                    active_match_id = last_match_id

        if chat_id not in TRACKED_PLAYERS:
            TRACKED_PLAYERS[chat_id] = {}

        TRACKED_PLAYERS[chat_id][player_id] = {
            "nickname": nick,
            "last_match_id": last_match_id or "",
            "active_match_id": active_match_id,
            "last_known_elo": str(current_elo),
        }

        save_tracked_player(
            chat_id=chat_id,
            player_id=player_id,
            nickname=nick,
            last_match_id=last_match_id or "",
            active_match_id=active_match_id,
            last_known_elo=str(current_elo),
        )

        if active_match_id:
            match_details, _ = get_match_details(active_match_id)
            text = format_match_found_message(nick, active_match_id, match_details)
            await query.message.reply_text(text)
        else:
            await query.message.reply_text(f"👀 Добавил {nick} в слежку.")
        return

    recent_limit = 5
    if action == "maps30":
        recent_limit = 30

    player_data, error = load_player_full_by_id(player_id, recent_limit=recent_limit)
    if error:
        await query.edit_message_text(error)
        return

    if action == "stats":
        text = build_faceit_text(player_data["details"], player_data["stats"])
    elif action == "form5":
        text = build_form5_text(player_data["details"], player_data["recent"], player_data["recent_error"])
    elif action == "last5":
        text = build_last5_text(player_data["details"], player_data["recent"], player_data["recent_error"])
    elif action == "elo":
        text = build_elo_text(player_data["details"])
    elif action == "maps30":
        text = build_maps30_text(player_data["details"], player_data["recent"], player_data["recent_error"])
    else:
        text = "Неизвестная кнопка."

    await query.edit_message_text(text, reply_markup=build_player_keyboard(player_id))
async def tracklive_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id

    if chat_id not in TRACKED_PLAYERS or not TRACKED_PLAYERS[chat_id]:
        await update.message.reply_text("Список слежки пуст.")
        return

    msg = await update.message.reply_text("Проверяю live матчи...")

    lines = []

    for player_id, data in TRACKED_PLAYERS[chat_id].items():

        nickname = data["nickname"]

        match_id, match_details, err = get_live_match_info(player_id)

        if err:
            continue

        if not match_id:
            continue

        map_name = "N/A"
        queue = safe_get(match_details, "competition_name")
        region = safe_get(match_details, "region")
        status = safe_get(match_details, "status")

        voting = match_details.get("voting", {})
        if isinstance(voting, dict):
            map_info = voting.get("map", {})
            if isinstance(map_info, dict):
                pick = map_info.get("pick")
                if isinstance(pick, list):
                    map_name = ", ".join(pick)
                elif pick:
                    map_name = str(pick)

        lines.append(
            f"🔥 {nickname}\n"
            f"🗺 Map: {map_name}\n"
            f"🏆 Queue: {queue}\n"
            f"🌍 Region: {region}\n"
            f"📍 Status: {status}\n"
        )

    if not lines:
        await msg.edit_text("Сейчас никто из отслеживаемых игроков не в матче.")
        return

    text = "🔥 Live matches\n\n" + "\n".join(lines)

    await msg.edit_text(text[:4000])

# =========================
# BACKGROUND TRACKER
# =========================
async def track_matches_job(context: ContextTypes.DEFAULT_TYPE):

    for chat_id, players in list(TRACKED_PLAYERS.items()):

        for player_id, data in list(players.items()):

            nickname = data["nickname"]
            active_match_id = data.get("active_match_id", "")
            last_match_id = data.get("last_match_id", "")
            elo_before = data.get("last_known_elo", "")

            # --- ПРОВЕРКА LIVE МАТЧА ---
            match_id, match_details, err = get_live_match_info(player_id)

            if match_id and not active_match_id and match_id != last_match_id:

                TRACKED_PLAYERS[chat_id][player_id]["active_match_id"] = match_id
                TRACKED_PLAYERS[chat_id][player_id]["last_match_id"] = match_id

                active_match_id = match_id

                update_tracked_player_state(
                    chat_id,
                    player_id,
                    active_match_id=match_id
                )

                text = format_match_found_message(
                    nickname,
                    match_id,
                    match_details
                )

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text[:4000]
                )

                continue


            # --- ПРОВЕРКА ПРОПУЩЕННОГО МАТЧА ---  
            history, _ = get_player_history(player_id, limit=1)
            new_last_match_id = extract_last_match_id(history)
            if new_last_match_id and new_last_match_id != last_match_id and not active_match_id:

               recent_data, _ = get_player_recent_stats(player_id, 5)
               match_stats = find_match_stats_in_recent(recent_data, new_last_match_id)

               details, _ = get_player_details(player_id)

               elo_after = safe_get(
                   get_cs2_data(details),
                   "faceit_elo",
                   elo_before
               )

               text = format_match_finished_message(
                   nickname,
                   new_last_match_id,
                   match_stats,
                   elo_before,
                   elo_after
               )

               await context.bot.send_message(
                   chat_id=chat_id,
                   text=text[:4000]
               )

               TRACKED_PLAYERS[chat_id][player_id]["last_match_id"] = new_last_match_id
               TRACKED_PLAYERS[chat_id][player_id]["last_known_elo"] = str(elo_after)

               update_tracked_player_state(
                   chat_id,
                   player_id,
                   last_match_id=new_last_match_id,
                   last_known_elo=str(elo_after)
               )

               continue

            
            # --- ПРОВЕРКА ЗАКОНЧИЛСЯ ЛИ МАТЧ ---
            if active_match_id:

                match_details, _ = get_match_details(active_match_id)
                status = safe_get(match_details, "status")

                if status == "FINISHED":

                    recent_data, _ = get_player_recent_stats(player_id, 5)

                    match_stats = find_match_stats_in_recent(
                        recent_data,
                        active_match_id
                    )

                    details, _ = get_player_details(player_id)

                    elo_after = safe_get(
                        get_cs2_data(details),
                        "faceit_elo",
                        elo_before
                    )

                    text = format_match_finished_message(
                        nickname,
                        active_match_id,
                        match_stats,
                        elo_before,
                        elo_after
                    )

                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text[:4000]
                    )

                    TRACKED_PLAYERS[chat_id][player_id]["last_match_id"] = active_match_id
                    TRACKED_PLAYERS[chat_id][player_id]["active_match_id"] = ""
                    TRACKED_PLAYERS[chat_id][player_id]["last_known_elo"] = str(elo_after)

                    update_tracked_player_state(
                        chat_id,
                        player_id,
                        last_match_id=active_match_id,
                        active_match_id="",
                        last_known_elo=str(elo_after)
                    )
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
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("faceit", faceit_command))
    app.add_handler(CommandHandler("last5", last5_command))
    app.add_handler(CommandHandler("elo", elo_command))
    app.add_handler(CommandHandler("form5", form5_command))
    app.add_handler(CommandHandler("compareform", compareform_command))
    app.add_handler(CommandHandler("maps30", maps30_command))
    app.add_handler(CommandHandler("fav", fav_command))
    app.add_handler(CommandHandler("favlive", favlive_command))
    app.add_handler(CommandHandler("favelo", favelo_command))
    app.add_handler(CommandHandler("trackstatus", trackstatus_command))
    app.add_handler(CommandHandler("favkd", favkd_command))
    app.add_handler(CommandHandler("favform", favform_command))
    app.add_handler(CommandHandler("favgainers", favgainers_command))
    app.add_handler(CommandHandler("favlosers", favlosers_command))
    app.add_handler(CommandHandler("tracklive", tracklive_command))
    app.add_handler(CommandHandler("trackfull", trackfull_command))
    app.add_handler(CommandHandler("untrackfull", untrackfull_command))
    app.add_handler(CommandHandler("tracklist", tracklist_command))
    app.add_handler(CommandHandler("cleartrack", cleartrack_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    app.job_queue.run_repeating(track_matches_job, interval=15, first=10)

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
