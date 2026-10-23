import json
import os
import sqlite3
import subprocess
import re
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from data_sources import (
    DataSourceError,
    fetch_api_football_context,
    fetch_api_football_matches,
    fetch_api_football_odds,
    fetch_api_football_results,
    fetch_upcoming_matches,
    supports_api_football_context,
)
from market import analyze_quote, lottery_result
from model import build_prediction
from optimizer import active_model_parameters, init_optimizer_tables, metrics as score_metrics, optimize_calibration, record_evaluation

ROOT = Path(__file__).parent
DB_PATH = ROOT / "football_predict.db"
UPLOAD_DIR = ROOT / "uploads"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def load_local_env():
    """Load simple KEY=VALUE settings without overriding the shell environment."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_local_env()


TEAM_ZH = {
    "Arsenal": "阿森纳", "Chelsea": "切尔西", "Manchester City": "曼城", "Manchester United": "曼联",
    "Liverpool": "利物浦", "Tottenham Hotspur": "托特纳姆热刺", "Tottenham": "托特纳姆热刺",
    "Newcastle United": "纽卡斯尔联", "Newcastle": "纽卡斯尔联", "Aston Villa": "阿斯顿维拉",
    "West Ham United": "西汉姆联", "West Ham": "西汉姆联", "Brighton": "布莱顿", "Brighton Hove & Albion": "布莱顿",
    "Everton": "埃弗顿", "Fulham": "富勒姆", "Crystal Palace": "水晶宫", "Wolverhampton Wanderers": "狼队",
    "Wolverhampton": "狼队", "Nottingham Forest": "诺丁汉森林", "Leicester City": "莱斯特城",
    "Real Madrid": "皇家马德里", "Barcelona": "巴塞罗那", "FC Barcelona": "巴塞罗那", "Atletico Madrid": "马德里竞技",
    "Atlético de Madrid": "马德里竞技", "Sevilla": "塞维利亚", "Villarreal": "比利亚雷亚尔", "Valencia": "瓦伦西亚",
    "Athletic Club": "毕尔巴鄂竞技", "Real Sociedad": "皇家社会", "Real Betis": "皇家贝蒂斯", "Girona": "赫罗纳",
    "Bayern Munich": "拜仁慕尼黑", "Bayern München": "拜仁慕尼黑", "Borussia Dortmund": "多特蒙德",
    "RB Leipzig": "莱比锡红牛", "Bayer Leverkusen": "勒沃库森", "Eintracht Frankfurt": "法兰克福",
    "VfB Stuttgart": "斯图加特", "Borussia Monchengladbach": "门兴格拉德巴赫", "Wolfsburg": "沃尔夫斯堡",
    "Inter": "国际米兰", "Inter Milan": "国际米兰", "AC Milan": "AC米兰", "Juventus": "尤文图斯",
    "Napoli": "那不勒斯", "Roma": "罗马", "Lazio": "拉齐奥", "Atalanta": "亚特兰大",
    "Paris Saint Germain": "巴黎圣日耳曼", "Paris Saint-Germain": "巴黎圣日耳曼", "Marseille": "马赛",
    "Lyon": "里昂", "Monaco": "摩纳哥", "Lille": "里尔", "Nice": "尼斯",
    "England": "英格兰", "France": "法国", "Germany": "德国", "Spain": "西班牙", "Italy": "意大利",
    "Portugal": "葡萄牙", "Netherlands": "荷兰", "Belgium": "比利时", "Brazil": "巴西", "Argentina": "阿根廷",
    "Japan": "日本", "Korea Republic": "韩国", "South Korea": "韩国", "China PR": "中国", "China": "中国",
    "Iran": "伊朗", "Australia": "澳大利亚", "Saudi Arabia": "沙特阿拉伯", "Qatar": "卡塔尔",
    "United Arab Emirates": "阿联酋", "UAE": "阿联酋", "United States": "美国", "USA": "美国", "Mexico": "墨西哥",
    "1899 Hoffenheim": "霍芬海姆", "AZ Alkmaar": "阿尔克马尔", "Aarhus": "奥胡斯", "Ajax": "阿贾克斯",
    "Anderlecht": "安德莱赫特", "Ararat-Armenia": "阿拉拉特亚美尼亚", "Benfica": "本菲卡", "Beşiktaş": "贝西克塔斯",
    "Borac Banja Luka": "巴尼亚卢卡战士", "Bournemouth": "伯恩茅斯", "Brann": "布兰", "CSKA Sofia": "索菲亚中央陆军",
    "Celje": "采列", "Celta Vigo": "塞尔塔", "Celtic": "凯尔特人", "Dinamo Zagreb": "萨格勒布迪纳摩",
    "Egnatia Rrogozhinë": "埃格纳蒂亚", "Estudiantes L.P.": "拉普拉塔大学生", "FC Copenhagen": "哥本哈根",
    "FC Iberia 1999": "伊比利亚1999", "FC Lugano": "卢加诺", "FC Midtjylland": "中日德兰", "FC Nordsjaelland": "北西兰",
    "FC Thun": "图恩", "FK Crvena Zvezda": "贝尔格莱德红星", "FK Jablonec": "亚布洛内茨", "Ferencvarosi TC": "费伦茨瓦罗斯",
    "Flamengo": "弗拉门戈", "Gent": "根特", "Getafe": "赫塔费", "HNK Hajduk Split": "海杜克",
    "Hapoel Beer Sheva": "贝尔谢巴工人", "Heart Of Midlothian": "哈茨", "Inter Club d'Escaldes": "埃斯卡尔德斯国际",
    "Jagiellonia": "乔治罗尼亚", "Kairat Almaty": "阿拉木图凯拉特", "Kauno Žalgiris": "考纳斯扎尔吉里斯", "KuPS": "古比斯",
    "Lech Poznan": "波兹南莱赫", "Levski Sofia": "索菲亚列夫斯基", "Lillestrom": "利勒斯特罗姆", "Lincoln Red Imps FC": "林肯红魔",
    "Mjallby AIF": "米亚尔比", "NEC Nijmegen": "奈梅亨", "OFI": "OFI克里特", "Olympiakos Piraeus": "奥林匹亚科斯",
    "Omonia Nicosia": "奥莫尼亚", "Pafos": "帕福斯", "Panathinaikos": "帕纳辛奈科斯", "Plzen": "比尔森胜利",
    "Red Bull Salzburg": "萨尔茨堡红牛", "Rennes": "雷恩", "Riga": "里加", "SC Braga": "布拉加",
    "SC Freiburg": "弗赖堡", "Sparta Praha": "布拉格斯巴达", "St. Truiden": "圣图尔登", "Sturm Graz": "格拉茨风暴",
    "Sunderland": "桑德兰", "Torreense": "托伦斯", "Trabzonspor": "特拉布宗体育", "Twente": "特温特",
    "Union St. Gilloise": "圣吉罗斯联合", "Universitatea Craiova": "克拉约瓦大学",
    "1. FC Köln": "科隆", "1. FC Union Berlin": "柏林联合", "1. FSV Mainz 05": "美因茨", "AC Monza": "蒙扎",
    "ACF Fiorentina": "佛罗伦萨", "AFC Bournemouth": "伯恩茅斯", "AJ Auxerre": "欧塞尔", "AS Monaco FC": "摩纳哥",
    "AS Roma": "罗马", "Angers SCO": "昂热", "Arsenal FC": "阿森纳", "Aston Villa FC": "阿斯顿维拉",
    "Atalanta BC": "亚特兰大", "Bayer 04 Leverkusen": "勒沃库森", "Bologna FC 1909": "博洛尼亚",
    "Borussia Mönchengladbach": "门兴格拉德巴赫", "Brentford FC": "布伦特福德", "Brighton & Hove Albion FC": "布莱顿",
    "CA Osasuna": "奥萨苏纳", "Cagliari Calcio": "卡利亚里", "Chelsea FC": "切尔西", "Club Atlético de Madrid": "马德里竞技",
    "Club Brugge KV": "布鲁日", "Como 1907": "科莫", "Coventry City FC": "考文垂", "Crystal Palace FC": "水晶宫",
    "Deportivo Alavés": "阿拉维斯", "ES Troyes AC": "特鲁瓦", "Elche CF": "埃尔切", "Everton FC": "埃弗顿",
    "FC Augsburg": "奥格斯堡", "FC Bayern München": "拜仁慕尼黑", "FC Internazionale Milano": "国际米兰",
    "FC Lorient": "洛里昂", "FC Porto": "波尔图", "FC Schalke 04": "沙尔克04", "FK Bodø/Glimt": "博德闪耀",
    "FK Shakhtar Donetsk": "顿涅茨克矿工", "Fenerbahçe SK": "费内巴切", "Feyenoord Rotterdam": "费耶诺德",
    "Frosinone Calcio": "弗罗西诺内", "Fulham FC": "富勒姆", "Galatasaray SK": "加拉塔萨雷", "Genoa CFC": "热那亚",
    "Getafe CF": "赫塔费", "Hamburger SV": "汉堡", "Hull City AFC": "赫尔城", "Ipswich Town FC": "伊普斯维奇",
    "Juventus FC": "尤文图斯", "LASK Linz": "林茨", "Le Havre AC": "勒阿弗尔", "Le Mans FC": "勒芒",
    "Leeds United FC": "利兹联", "Levante UD": "莱万特", "Lille OSC": "里尔", "Liverpool FC": "利物浦",
    "Manchester City FC": "曼城", "Manchester United FC": "曼联", "Málaga CF": "马拉加", "Newcastle United FC": "纽卡斯尔联",
    "Nottingham Forest FC": "诺丁汉森林", "OGC Nice": "尼斯", "Olympique Lyonnais": "里昂", "Olympique de Marseille": "马赛",
    "PAE AEK": "雅典AEK", "PSV": "埃因霍温", "Paris FC": "巴黎FC", "Paris Saint-Germain FC": "巴黎圣日耳曼",
    "Parma Calcio 1913": "帕尔马", "RC Celta de Vigo": "塞尔塔", "RC Deportivo La Coruña": "拉科鲁尼亚",
    "RC Strasbourg Alsace": "斯特拉斯堡", "RCD Espanyol de Barcelona": "西班牙人", "Racing Club de Lens": "朗斯",
    "Rayo Vallecano de Madrid": "巴列卡诺", "Real Betis Balompié": "皇家贝蒂斯", "Real Madrid CF": "皇家马德里",
    "Real Racing Club de Santander": "桑坦德竞技", "Real Sociedad de Fútbol": "皇家社会", "SC Paderborn 07": "帕德博恩",
    "SK Slavia Praha": "布拉格斯拉维亚", "SS Lazio": "拉齐奥", "SSC Napoli": "那不勒斯", "SV 07 Elversberg": "埃尔弗斯贝格",
    "SV Werder Bremen": "云达不莱梅", "Sabah FK": "萨巴赫", "Sevilla FC": "塞维利亚", "Sporting Clube de Portugal": "葡萄牙体育",
    "Stade Brestois 29": "布雷斯特", "Stade Rennais FC 1901": "雷恩", "Sunderland AFC": "桑德兰",
    "TSG 1899 Hoffenheim": "霍芬海姆", "Torino FC": "都灵", "Tottenham Hotspur FC": "托特纳姆热刺", "Toulouse FC": "图卢兹",
    "US Lecce": "莱切", "US Sassuolo Calcio": "萨索洛", "Udinese Calcio": "乌迪内斯", "Valencia CF": "瓦伦西亚",
    "Venezia FC": "威尼斯", "Viking FK": "维京", "Villarreal CF": "比利亚雷亚尔", "ŠK Slovan Bratislava": "布拉迪斯拉发斯拉夫人",
}

LEAGUE_ZH = {
    "Premier League": "英超", "La Liga": "西甲", "Primera Division": "西甲", "Primera División": "西甲", "Prime Division": "西甲", "Bundesliga": "德甲", "Serie A": "意甲", "Ligue 1": "法甲",
    "UEFA Champions League": "欧冠", "FIFA World Cup": "世界杯", "World Cup": "世界杯", "European Championship": "欧洲杯", "Euro Championship": "欧洲杯",
    "UEFA Europa League": "欧罗巴", "UEFA Conference League": "欧协联", "UEFA Europa Conference League": "欧协联",
    "Asian Cup": "亚洲杯", "Asian Games": "亚运会", "Copa America": "美洲杯", "Africa Cup of Nations": "非洲杯",
    "Olympics Men": "奥运男足", "FIFA Club World Cup": "世俱杯",
    "J1 League": "日职", "J2 League": "日乙", "CONMEBOL Libertadores": "解放者杯",
}


def localized_team_name(name):
    return TEAM_ZH.get(name, name)


def localized_league_name(name):
    return LEAGUE_ZH.get(name, name)


def localized_event(row):
    event = dict(row)
    event["home_team_zh"] = localized_team_name(event.get("home_team"))
    event["away_team_zh"] = localized_team_name(event.get("away_team"))
    event["league_zh"] = localized_league_name(event.get("league"))
    event["context_supported"] = supports_api_football_context(event)
    return event


def parse_event_time(kickoff_at):
    value = str(kickoff_at or "").strip()
    if value.endswith(" UTC"):
        value = value[:-4] + "+00:00"
    elif value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        kickoff = datetime.fromisoformat(value)
    except ValueError:
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=BEIJING_TZ)
    return kickoff


def beijing_date(kickoff_at):
    kickoff = parse_event_time(kickoff_at)
    return kickoff.astimezone(BEIJING_TZ).date().isoformat() if kickoff else None


def is_upcoming_event(kickoff_at):
    kickoff = parse_event_time(kickoff_at)
    if not kickoff:
        return False
    return kickoff > datetime.now(timezone.utc)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    UPLOAD_DIR.mkdir(exist_ok=True)
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            aliases TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS shortlist (
            event_id INTEGER PRIMARY KEY,
            lottery_handicap INTEGER,
            confirmed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS market_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            handicap REAL DEFAULT 0,
            source TEXT DEFAULT 'manual/demo',
            source_id TEXT,
            competition_type TEXT NOT NULL DEFAULT 'club',
            stage TEXT,
            venue TEXT,
            is_neutral INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS team_stats (
            team TEXT PRIMARY KEY,
            home_attack REAL NOT NULL,
            away_attack REAL NOT NULL,
            home_defense REAL NOT NULL,
            away_defense REAL NOT NULL,
            form REAL NOT NULL DEFAULT 1.0
        );
        CREATE TABLE IF NOT EXISTS api_team_stats_cache (
            cache_key TEXT PRIMARY KEY,
            league_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            team_name TEXT,
            payload TEXT NOT NULL,
            captured_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fixture_contexts (
            event_id INTEGER PRIMARY KEY,
            fixture_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );
        CREATE TABLE IF NOT EXISTS results (
            event_id INTEGER PRIMARY KEY,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );
        """
    )
    # Existing local databases created before source_id was introduced remain usable.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "source_id" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN source_id TEXT")
    migrations = {
        "competition_type": "TEXT NOT NULL DEFAULT 'club'",
        "stage": "TEXT",
        "venue": "TEXT",
        "is_neutral": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_source_id_unique ON events(source, source_id) WHERE source_id IS NOT NULL")
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if count == 0:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            ("英超", "阿森纳", "切尔西", "2026-08-15 22:00", 0.5),
            ("西甲", "皇家马德里", "塞维利亚", "2026-08-16 03:00", -1.0),
            ("德甲", "拜仁慕尼黑", "多特蒙德", "2026-08-16 21:30", -0.5),
        ]
        conn.executemany(
            "INSERT INTO events(league,home_team,away_team,kickoff_at,handicap,updated_at) VALUES(?,?,?,?,?,?)",
            [r + (now,) for r in rows],
        )
        stats = [
            ("阿森纳", 1.45, 1.15, 0.82, 1.02, 1.08),
            ("切尔西", 1.18, 1.02, 1.04, 1.16, 0.98),
            ("皇家马德里", 1.62, 1.40, 0.72, 0.90, 1.12),
            ("塞维利亚", 0.94, 0.86, 1.18, 1.24, 0.92),
            ("拜仁慕尼黑", 1.72, 1.45, 0.78, 0.98, 1.10),
            ("多特蒙德", 1.34, 1.20, 1.06, 1.18, 1.00),
        ]
        conn.executemany("INSERT INTO team_stats VALUES(?,?,?,?,?,?)", stats)
    conn.commit()
    init_optimizer_tables(conn)
    conn.commit()
    conn.close()


def upsert_events(rows):
    """Insert or refresh provider fixtures without duplicating source IDs."""
    conn = db()
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    updated = 0
    for row in rows:
        existing = conn.execute(
            "SELECT id FROM events WHERE source=? AND source_id=?",
            (row["source"], row["source_id"]),
        ).fetchone()
        values = (
            row["league"],
            row["home_team"],
            row["away_team"],
            row["kickoff_at"],
            row.get("status", "scheduled"),
            row.get("competition_type", "club"),
            row.get("stage"),
            row.get("venue"),
            int(bool(row.get("is_neutral", 0))),
            now,
        )
        if existing:
            conn.execute(
                """UPDATE events SET league=?, home_team=?, away_team=?, kickoff_at=?, status=?,
                    competition_type=?, stage=?, venue=?, is_neutral=?, updated_at=? WHERE id=?""",
                values + (existing["id"],),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO events(
                    league,home_team,away_team,kickoff_at,status,source,source_id,
                    competition_type,stage,venue,is_neutral,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                values[:5] + (row["source"], row["source_id"]) + values[5:],
            )
            inserted += 1
    conn.commit()
    conn.close()
    return {"inserted": inserted, "updated": updated}


CONTEXT_CACHE_SECONDS = 3600


def _parse_timestamp(value):
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00").replace(" UTC", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def sync_event_context(event, force=False):
    """Load a recent fixture context or refresh it through API-Football."""
    if not supports_api_football_context(event):
        raise DataSourceError("当前赛事不在接入范围，仅支持五大联赛、欧冠和指定国际赛事。")
    conn = db()
    existing = conn.execute(
        "SELECT payload, captured_at FROM fixture_contexts WHERE event_id=?", (event["id"],)
    ).fetchone()
    cached_team_stats = {
        row["cache_key"]: json.loads(row["payload"])
        for row in conn.execute("SELECT cache_key, payload FROM api_team_stats_cache")
    }
    conn.close()
    existing_time = _parse_timestamp(existing["captured_at"]) if existing else None
    if existing and not force and existing_time and (datetime.now(timezone.utc) - existing_time).total_seconds() < CONTEXT_CACHE_SECONDS:
        try:
            context = json.loads(existing["payload"])
        except json.JSONDecodeError:
            context = None
        if isinstance(context, dict) and "stats_fallback" in context:
            context["cached"] = True
            context["request_count"] = 0
            return context

    batch = fetch_api_football_context(event, cached_team_stats=cached_team_stats)
    team_cache = batch.pop("team_cache", {})
    context = dict(batch)
    context["cached"] = False
    conn = db()
    try:
        for cache_key, value in team_cache.items():
            conn.execute(
                """INSERT OR REPLACE INTO api_team_stats_cache
                   (cache_key, league_id, season, team_id, team_name, payload, captured_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    cache_key,
                    value["league_id"],
                    value["season"],
                    value["team_id"],
                    value.get("team_name"),
                    json.dumps(value, ensure_ascii=False),
                    context["captured_at"],
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO fixture_contexts(event_id,fixture_id,payload,captured_at) VALUES(?,?,?,?)",
            (event["id"], context["fixture_id"], json.dumps(context, ensure_ascii=False), context["captured_at"]),
        )
        conn.commit()
    finally:
        conn.close()
    return context


def predict(event, lottery_handicap=None, refresh_context=False):
    event = dict(event)
    context = None
    context_error = None
    if supports_api_football_context(event):
        try:
            context = sync_event_context(dict(event), force=refresh_context)
        except DataSourceError as exc:
            context_error = str(exc)
    conn = db()
    hs = (context or {}).get("home_stats")
    aw = (context or {}).get("away_stats")
    if hs is None:
        hs = conn.execute("SELECT * FROM team_stats WHERE team=?", (event["home_team"],)).fetchone()
    if aw is None:
        aw = conn.execute("SELECT * FROM team_stats WHERE team=?", (event["away_team"],)).fetchone()
    model_parameters = active_model_parameters(conn)
    active_row = conn.execute("SELECT version FROM model_versions WHERE status='active' LIMIT 1").fetchone()
    conn.close()
    result = build_prediction(
        hs,
        aw,
        temperature=model_parameters.get("temperature", 1.0),
        model_version=active_row["version"] if active_row else "poisson-mvp-0.2",
        lottery_handicap=lottery_handicap,
        context=context,
        goal_scales=model_parameters.get("goal_scales"),
    )
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["data_context"] = {
        "provider": "API-Football" if context else None,
        "fixture_id": (context or {}).get("fixture_id"),
        "captured_at": (context or {}).get("captured_at"),
        "cached": (context or {}).get("cached", False),
        "request_count": (context or {}).get("request_count", 0),
        "quota_remaining": (context or {}).get("quota_remaining"),
        "stats_available": bool((context or {}).get("home_stats") and (context or {}).get("away_stats")),
        "stats_fallback": bool((context or {}).get("stats_fallback")),
        "stats_seasons": (context or {}).get("stats_seasons") or {},
        "season": (context or {}).get("season"),
        "home_stats": (context or {}).get("home_stats"),
        "away_stats": (context or {}).get("away_stats"),
        "availability": (context or {}).get("availability") or {},
        "injuries": (context or {}).get("injuries") or {},
        "lineups": (context or {}).get("lineups") or {},
        "lineups_available": bool(((context or {}).get("lineups") or {}).get("available")),
        "injuries_available": bool(((context or {}).get("injuries") or {}).get("available")),
        "warnings": (context or {}).get("warnings", []),
        "error": context_error,
    }
    return result


def save_prediction(conn, event, result):
    cursor = conn.execute(
        "INSERT INTO predictions(event_id,model_version,generated_at,payload) VALUES(?,?,?,?)",
        (event["id"], result["model_version"], result["generated_at"], json.dumps(result, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO feature_snapshots(prediction_id,event_id,features,captured_at) VALUES(?,?,?,?)",
        (cursor.lastrowid, event["id"], json.dumps(result["features"], ensure_ascii=False), result["generated_at"]),
    )
    return cursor.lastrowid


def normalize_text(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.lower())


def match_events(ocr_text):
    """Find scheduled events whose two team names appear in the OCR text."""
    normalized = normalize_text(ocr_text)
    conn = db()
    events = conn.execute("SELECT * FROM events WHERE status='scheduled' ORDER BY kickoff_at").fetchall()
    conn.close()
    candidates = []
    for event in events:
        home, away = normalize_text(event["home_team"]), normalize_text(event["away_team"])
        home_hit, away_hit = home in normalized, away in normalized
        if home_hit or away_hit:
            score = (0.5 if home_hit else 0) + (0.5 if away_hit else 0)
            candidates.append({"event": localized_event(event), "match_score": score})
    return sorted(candidates, key=lambda item: item["match_score"], reverse=True)[:5]


def local_ocr(image_path):
    script = ROOT / "scripts" / "ocr.swift"
    completed = subprocess.run(
        ["/usr/bin/swift", str(script), str(image_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "本地 OCR 引擎运行失败")
    return json.loads(completed.stdout)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == '/api/shortlist':
            params = parse_qs(parsed_url.query)
            from_date = params.get('from_date', [''])[0]
            to_date = params.get('to_date', [''])[0]
            for value in (from_date, to_date):
                if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    return self.send_json({'error': '日期格式必须是 YYYY-MM-DD'}, 400)
            if from_date and to_date and from_date > to_date:
                return self.send_json({'error': '开始日期不能晚于结束日期'}, 400)
            with db() as conn:
                sql = 'SELECT e.*, s.lottery_handicap FROM shortlist s JOIN events e ON e.id=s.event_id'
                rows = conn.execute(sql).fetchall()
                rows = [
                    row for row in rows
                    if (not from_date or (beijing_date(row['kickoff_at']) or '') >= from_date)
                    and (not to_date or (beijing_date(row['kickoff_at']) or '') <= to_date)
                ]
                output = []
                for row in rows:
                    if not is_upcoming_event(row['kickoff_at']):
                        continue
                    item = localized_event(row)
                    context_row = conn.execute(
                        'SELECT payload, captured_at FROM fixture_contexts WHERE event_id=?', (row['id'],)
                    ).fetchone()
                    item['context'] = None
                    if context_row:
                        try:
                            context_payload = json.loads(context_row['payload'])
                            item['context'] = {
                                'captured_at': context_row['captured_at'],
                                'stats_available': bool(context_payload.get('home_stats') and context_payload.get('away_stats')),
                                'stats_fallback': bool(context_payload.get('stats_fallback')),
                                'stats_seasons': context_payload.get('stats_seasons') or {},
                                'lineups_available': bool((context_payload.get('lineups') or {}).get('available')),
                                'injuries_available': bool((context_payload.get('injuries') or {}).get('available')),
                            }
                        except json.JSONDecodeError:
                            pass
                    quotes = conn.execute('SELECT payload FROM market_quotes WHERE event_id=?', (row['id'],)).fetchall()
                    quotes = sorted((json.loads(q['payload']) for q in quotes), key=lambda q: datetime.fromisoformat(q['captured_at']))
                    item['market'] = quotes[-1] if quotes else None
                    item['eligible'] = False
                    if quotes:
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(quotes[-1]['captured_at'])).total_seconds()
                        try:
                            kickoff = datetime.fromisoformat(row['kickoff_at'].replace(' UTC', '+00:00').replace('Z', '+00:00'))
                            if kickoff.tzinfo is None:
                                from zoneinfo import ZoneInfo
                                kickoff = kickoff.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                            item['eligible'] = age <= 21600 and kickoff > datetime.now(timezone.utc) and row['status'] == 'scheduled' and row['source'] != 'manual/demo'
                        except ValueError:
                            pass
                        previous = next((q for q in reversed(quotes[:-1]) if q['source'] == quotes[-1]['source'] and q['captured_at'] != quotes[-1]['captured_at']), None)
                        item['change'] = {k: quotes[-1]['probabilities'][k] - previous['probabilities'][k] for k in previous['probabilities']} if previous else None
                    output.append(item)
            output.sort(key=lambda x: (x['eligible'], max(x['market']['probabilities'].values()) if x['market'] else 0), reverse=True)
            return self.send_json(output)
        if path == "/api/events":
            conn = db()
            events = [localized_event(row) for row in conn.execute("SELECT * FROM events ORDER BY kickoff_at") if is_upcoming_event(row["kickoff_at"])]
            conn.close()
            return self.send_json(events)
        if path.startswith("/api/events/"):
            event_id = path.rsplit("/", 1)[-1]
            conn = db()
            event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            conn.close()
            return self.send_json(localized_event(event) if event else {"error": "赛事不存在"}, 200 if event else 404)
        if path == "/api/predictions":
            conn = db()
            rows = conn.execute("""SELECT p.id, p.event_id, p.model_version, p.generated_at,
                e.league, e.home_team, e.away_team, p.payload,
                r.home_score, r.away_score
                FROM predictions p JOIN events e ON e.id=p.event_id
                LEFT JOIN results r ON r.event_id=p.event_id
                ORDER BY p.generated_at DESC LIMIT 100""").fetchall()
            conn.close()
            out = []
            for row in rows:
                item = dict(row)
                item["home_team_zh"] = localized_team_name(item["home_team"])
                item["away_team_zh"] = localized_team_name(item["away_team"])
                item["league_zh"] = localized_league_name(item["league"])
                item["payload"] = json.loads(item["payload"])
                item["result"] = None if item["home_score"] is None else f"{item['home_score']}:{item['away_score']}"
                out.append(item)
            return self.send_json(out)
        if path == "/api/metrics":
            conn = db()
            rows = conn.execute("""SELECT p.payload, e.actual, f.features FROM predictions p
                LEFT JOIN feature_snapshots f ON f.prediction_id=p.id
                JOIN prediction_evaluations e ON e.prediction_id=p.id WHERE e.market='win_draw_loss'""").fetchall()
            active = conn.execute("SELECT version, model_type, parameters, created_at FROM model_versions WHERE status='active' LIMIT 1").fetchone()
            model_parameters = active_model_parameters(conn)
            conn.close()
            result = score_metrics(
                rows,
                temperature=model_parameters.get("temperature", 1.0),
                goal_scales=model_parameters.get("goal_scales"),
            ) if rows else {"sample_count": 0, "accuracy": None, "log_loss": None, "brier_score": None}
            result["active_model"] = dict(active) if active else None
            return self.send_json(result)
        if path == "/api/health":
            return self.send_json({"ok": True, "database": str(DB_PATH.name)})
        if path == "/api/data-sources":
            football_data_token = str(os.getenv("FOOTBALL_DATA_TOKEN") or "").strip()
            return self.send_json({
                "football_data": {
                    "configured": bool(football_data_token) and football_data_token.isascii(),
                    "competitions": ["五大联赛", "欧冠", "世界杯", "欧洲杯"],
                },
                "api_football": {
                    "configured": bool(os.getenv("API_FOOTBALL_KEY")),
                    "competitions": ["亚洲杯", "亚运会", "美洲杯", "非洲杯", "奥运男足", "世俱杯", "日职", "日乙", "欧罗巴", "欧协联", "解放者杯"],
                    "free_daily_limit": 100,
                },
            })
        if path == "/" or path == "/index.html":
            return self.serve_file(ROOT / "static" / "index.html", "text/html; charset=utf-8")
        return self.serve_file(ROOT / "static" / path.lstrip("/"), None)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/upload":
            return self.upload_image()
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "请求格式错误"}, 400)
        if not isinstance(body, dict):
            return self.send_json({'error': '请求必须是对象'}, 400)
        if path in ('/api/shortlist/bulk', '/api/shortlist/bulk-remove'):
            event_ids = body.get('event_ids')
            if not isinstance(event_ids, list) or not event_ids or len(event_ids) > 50:
                return self.send_json({'error': '请提供 1–50 个赛事编号'}, 400)
            normalized_ids = []
            for event_id in event_ids:
                if type(event_id) is not int or event_id <= 0:
                    return self.send_json({'error': '赛事编号必须是正整数'}, 400)
                if event_id not in normalized_ids:
                    normalized_ids.append(event_id)
            bulk_handicap = body.get('lottery_handicap')
            if path == '/api/shortlist/bulk' and bulk_handicap is not None:
                if type(bulk_handicap) is not int or not -20 <= bulk_handicap <= 20:
                    return self.send_json({'error': '竞彩让球必须是 -20 到 20 的整数'}, 400)
            placeholders = ','.join('?' for _ in normalized_ids)
            with db() as conn:
                rows = conn.execute(
                    f'SELECT id, kickoff_at FROM events WHERE id IN ({placeholders})', normalized_ids
                ).fetchall()
                if path == '/api/shortlist/bulk':
                    upcoming_rows = [row for row in rows if is_upcoming_event(row['kickoff_at'])]
                    if not upcoming_rows:
                        return self.send_json({'error': '所选赛事均已过时或不存在'}, 400)
                    now = datetime.now(timezone.utc).isoformat()
                    for row in upcoming_rows:
                        if bulk_handicap is None:
                            conn.execute(
                                'INSERT OR IGNORE INTO shortlist(event_id, lottery_handicap, confirmed_at) VALUES(?,?,?)',
                                (row['id'], None, now),
                            )
                        else:
                            conn.execute(
                                """INSERT INTO shortlist(event_id, lottery_handicap, confirmed_at) VALUES(?,?,?)
                                   ON CONFLICT(event_id) DO UPDATE SET lottery_handicap=excluded.lottery_handicap""",
                                (row['id'], bulk_handicap, now),
                            )
                    accepted_ids = [row['id'] for row in upcoming_rows]
                    return self.send_json({'ok': True, 'count': len(accepted_ids), 'event_ids': accepted_ids, 'lottery_handicap': bulk_handicap})
                removed = conn.execute(
                    f'DELETE FROM shortlist WHERE event_id IN ({placeholders})', normalized_ids
                ).rowcount
            return self.send_json({'ok': True, 'count': removed, 'event_ids': normalized_ids})
        if path in ('/api/shortlist', '/api/market-quotes', '/api/shortlist/remove'):
            try:
                with db() as conn:
                    event = conn.execute('SELECT id FROM events WHERE id=?', (body.get('event_id'),)).fetchone()
                    if not event:
                        return self.send_json({'error': '赛事不存在'}, 404)
                    now = datetime.now(timezone.utc).isoformat()
                    if path == '/api/shortlist':
                        handicap = body.get('lottery_handicap')
                        if handicap is not None:
                            if type(handicap) is not int or not -20 <= handicap <= 20:
                                raise ValueError('竞彩让球必须是 -20 到 20 的整数')
                            lottery_result(0, 0, handicap)
                        conn.execute('INSERT OR REPLACE INTO shortlist VALUES(?,?,?)', (event['id'], handicap, now))
                    elif path == '/api/shortlist/remove':
                        conn.execute('DELETE FROM shortlist WHERE event_id=?', (event['id'],))
                    else:
                        quote = analyze_quote(body)
                        conn.execute('INSERT INTO market_quotes(event_id,payload,created_at) VALUES(?,?,?)', (event['id'], json.dumps(quote, ensure_ascii=False), now))
                return self.send_json({'ok': True})
            except ValueError as exc:
                return self.send_json({'error': str(exc)}, 400)
        if path == "/api/sync/context":
            conn = db()
            event = conn.execute("SELECT * FROM events WHERE id=?", (body.get("event_id"),)).fetchone()
            conn.close()
            if not event:
                return self.send_json({"error": "赛事不存在"}, 404)
            try:
                context = sync_event_context(dict(event), force=bool(body.get("force")))
            except DataSourceError as exc:
                return self.send_json({"error": str(exc)}, 400)
            injuries = context.get("injuries") or {}
            return self.send_json({
                "ok": True,
                "event_id": event["id"],
                "fixture_id": context.get("fixture_id"),
                "captured_at": context.get("captured_at"),
                "cached": context.get("cached", False),
                "request_count": context.get("request_count", 0),
                "quota_remaining": context.get("quota_remaining"),
                "stats_available": bool(context.get("home_stats") and context.get("away_stats")),
                "stats_fallback": bool(context.get("stats_fallback")),
                "stats_seasons": context.get("stats_seasons") or {},
                "lineups_available": bool((context.get("lineups") or {}).get("available")),
                "injuries_available": bool(injuries.get("available")),
                "home_injury_count": len(injuries.get("home") or []),
                "away_injury_count": len(injuries.get("away") or []),
                "warnings": context.get("warnings", []),
            })
        if path == "/api/sync/odds":
            conn = db()
            event = conn.execute("SELECT * FROM events WHERE id=?", (body.get("event_id"),)).fetchone()
            conn.close()
            if not event:
                return self.send_json({"error": "赛事不存在"}, 404)
            try:
                batch = fetch_api_football_odds(dict(event))
            except DataSourceError as exc:
                return self.send_json({"error": str(exc)}, 400)
            if not batch["quotes"]:
                return self.send_json({
                    "error": "未获取到可分析的国际胜平负赔率",
                    "warnings": batch.get("warnings", []),
                    "fixture_id": batch.get("fixture_id"),
                    "request_count": batch.get("request_count", 0),
                }, 400)
            conn = db()
            existing = [json.loads(row["payload"]) for row in conn.execute(
                "SELECT payload FROM market_quotes WHERE event_id=?", (event["id"],)
            )]
            imported = 0
            warnings = list(batch.get("warnings", []))
            bookmakers = []
            try:
                for quote in batch["quotes"]:
                    try:
                        payload = analyze_quote(quote)
                    except ValueError as exc:
                        warnings.append(f"{quote.get('bookmaker', '未知公司')}：{exc}")
                        continue
                    payload["bookmaker"] = quote.get("bookmaker")
                    payload["fixture_id"] = batch.get("fixture_id")
                    duplicate = any(
                        item.get("source") == payload["source"]
                        and item.get("captured_at") == payload["captured_at"]
                        for item in existing
                    )
                    if duplicate:
                        continue
                    conn.execute(
                        "INSERT INTO market_quotes(event_id,payload,created_at) VALUES(?,?,?)",
                        (event["id"], json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
                    )
                    existing.append(payload)
                    bookmakers.append(payload["bookmaker"])
                    imported += 1
                conn.commit()
            finally:
                conn.close()
            return self.send_json({
                "ok": True,
                "event_id": event["id"],
                "fixture_id": batch.get("fixture_id"),
                "imported": imported,
                "bookmakers": bookmakers,
                "warnings": warnings,
                "request_count": batch.get("request_count", 0),
                "quota_remaining": batch.get("quota_remaining"),
            })
        if path == "/api/sync/shortlist":
            kind = body.get("kind")
            if kind not in {"odds", "context"}:
                return self.send_json({"error": "批量同步类型必须是 odds 或 context"}, 400)
            event_ids = body.get("event_ids")
            if not isinstance(event_ids, list) or not event_ids or len(event_ids) > 50:
                return self.send_json({"error": "请提供 1–50 个候选赛事编号"}, 400)
            normalized_ids = []
            for event_id in event_ids:
                if type(event_id) is not int or event_id <= 0:
                    return self.send_json({"error": "赛事编号必须是正整数"}, 400)
                if event_id not in normalized_ids:
                    normalized_ids.append(event_id)
            placeholders = ",".join("?" for _ in normalized_ids)
            with db() as conn:
                events = conn.execute(
                    f"SELECT * FROM events WHERE id IN ({placeholders})", normalized_ids
                ).fetchall()
            updated = 0
            request_count = 0
            quota_remaining = None
            warnings = []
            for event in events:
                if not is_upcoming_event(event["kickoff_at"]):
                    warnings.append(f"{event['home_team']} vs {event['away_team']} 已过开赛时间")
                    continue
                if kind == "context":
                    try:
                        context = sync_event_context(dict(event), force=True)
                        request_count += context.get("request_count", 0)
                        quota_remaining = context.get("quota_remaining", quota_remaining)
                        warnings.extend(context.get("warnings", []))
                        updated += 1
                    except DataSourceError as exc:
                        warnings.append(f"{event['home_team']} vs {event['away_team']}：{exc}")
                    continue
                try:
                    batch = fetch_api_football_odds(dict(event))
                    request_count += batch.get("request_count", 0)
                    quota_remaining = batch.get("quota_remaining", quota_remaining)
                    warnings.extend(batch.get("warnings", []))
                except DataSourceError as exc:
                    warnings.append(f"{event['home_team']} vs {event['away_team']}：{exc}")
                    continue
                if not batch.get("quotes"):
                    warnings.append(f"{event['home_team']} vs {event['away_team']} 未返回完整赔率")
                    continue
                conn = db()
                try:
                    existing = [json.loads(row["payload"]) for row in conn.execute(
                        "SELECT payload FROM market_quotes WHERE event_id=?", (event["id"],)
                    )]
                    imported = 0
                    for quote in batch["quotes"]:
                        try:
                            payload = analyze_quote(quote)
                        except ValueError as exc:
                            warnings.append(f"{quote.get('bookmaker', '未知公司')}：{exc}")
                            continue
                        payload["bookmaker"] = quote.get("bookmaker")
                        payload["fixture_id"] = batch.get("fixture_id")
                        duplicate = any(
                            item.get("source") == payload["source"]
                            and item.get("captured_at") == payload["captured_at"]
                            for item in existing
                        )
                        if duplicate:
                            continue
                        conn.execute(
                            "INSERT INTO market_quotes(event_id,payload,created_at) VALUES(?,?,?)",
                            (event["id"], json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
                        )
                        existing.append(payload)
                        imported += 1
                    conn.commit()
                    updated += 1 if imported else 0
                finally:
                    conn.close()
            return self.send_json({
                "ok": True,
                "kind": kind,
                "requested": len(normalized_ids),
                "updated": updated,
                "request_count": request_count,
                "quota_remaining": quota_remaining,
                "warnings": warnings,
            })
        if path == "/api/parlay/recommend":
            parlay_type = body.get("parlay_type", "2串1")
            strategy = body.get("strategy", "safe")
            type_sizes = {
                "2串1": 2,
                "3串1": 3,
                "4串1": 4,
                "5串1": 5,
                "6串1": 6,
                "7串1": 7,
                "8串1": 8,
            }
            strategy_labels = {"safe": "稳健型", "balanced": "均衡型", "value": "高赔型"}
            if parlay_type not in type_sizes:
                return self.send_json({"error": "串关类型目前支持 2串1、3串1、4串1、5串1、6串1、7串1、8串1"}, 400)
            if strategy not in strategy_labels:
                return self.send_json({"error": "串关策略无效"}, 400)
            event_ids = body.get("event_ids")
            if event_ids is not None:
                if not isinstance(event_ids, list) or not event_ids or len(event_ids) > 50:
                    return self.send_json({"error": "候选赛事编号必须是 1–50 个"}, 400)
                event_ids = list(dict.fromkeys(event_ids))
                if any(type(event_id) is not int or event_id <= 0 for event_id in event_ids):
                    return self.send_json({"error": "赛事编号必须是正整数"}, 400)
            conn = db()
            try:
                if event_ids:
                    placeholders = ",".join("?" for _ in event_ids)
                    rows = conn.execute(
                        f"SELECT e.*, s.lottery_handicap FROM shortlist s JOIN events e ON e.id=s.event_id WHERE e.id IN ({placeholders})",
                        event_ids,
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT e.*, s.lottery_handicap FROM shortlist s JOIN events e ON e.id=s.event_id"
                    ).fetchall()
                quote_map = {}
                for row in rows:
                    quote_rows = conn.execute(
                        "SELECT payload FROM market_quotes WHERE event_id=? ORDER BY created_at DESC", (row["id"],)
                    ).fetchall()
                    if quote_rows:
                        try:
                            quote_map[row["id"]] = json.loads(quote_rows[0]["payload"])
                        except json.JSONDecodeError:
                            pass
            finally:
                conn.close()
            rows = [row for row in rows if is_upcoming_event(row["kickoff_at"])]
            if len(rows) < type_sizes[parlay_type]:
                return self.send_json({"error": f"候选池至少需要 {type_sizes[parlay_type]} 场未开赛比赛"}, 400)
            ranked = []
            for event in rows:
                prediction = predict(event, lottery_handicap=event["lottery_handicap"])
                probabilities = prediction.get("win_draw_loss") or {}
                quote = quote_map.get(event["id"]) or {}
                market_probabilities = quote.get("probabilities") or {}
                odds_map = quote.get("odds") or {}
                scores = {}
                for outcome, probability in probabilities.items():
                    market_probability = market_probabilities.get(outcome, probability)
                    odds = odds_map.get({"主胜": "home", "平局": "draw", "客胜": "away"}.get(outcome))
                    if strategy == "safe":
                        score = probability
                    elif strategy == "balanced":
                        score = probability * 0.65 + market_probability * 0.35
                    else:
                        score = probability * float(odds) if odds and float(odds) > 1 else probability
                    scores[outcome] = score
                selection = max(scores, key=scores.get)
                odds = odds_map.get({"主胜": "home", "平局": "draw", "客胜": "away"}.get(selection))
                ranked.append({
                    "event": localized_event(event),
                    "selection": selection,
                    "probability": probabilities.get(selection, 0),
                    "odds": odds,
                    "score": scores[selection],
                    "prediction": prediction,
                    "reason": strategy_labels[strategy],
                })
            ranked.sort(key=lambda item: item["score"], reverse=True)
            legs = ranked[:type_sizes[parlay_type]]
            combined_probability = 1.0
            combined_odds = 1.0
            has_all_odds = True
            for leg in legs:
                combined_probability *= leg["probability"]
                if leg["odds"] is None:
                    has_all_odds = False
                else:
                    combined_odds *= leg["odds"]
                leg.pop("prediction", None)
            return self.send_json({
                "ok": True,
                "type": parlay_type,
                "strategy": strategy,
                "strategy_label": strategy_labels[strategy],
                "legs": legs,
                "combined_probability": combined_probability,
                "combined_odds": combined_odds if has_all_odds else None,
            })
        if path == "/api/parlay/preview":
            legs = body.get("legs")
            if not isinstance(legs, list) or len(legs) < 2:
                return self.send_json({"error": "串关至少需要选择 2 场比赛"}, 400)
            allowed_selections = {"主胜", "平局", "客胜"}
            conn = db()
            output = []
            seen = set()
            try:
                for leg in legs:
                    event_id = leg.get("event_id") if isinstance(leg, dict) else None
                    selection = leg.get("selection") if isinstance(leg, dict) else None
                    if selection not in allowed_selections:
                        return self.send_json({"error": "串关目前只支持胜平负结果"}, 400)
                    if event_id in seen:
                        return self.send_json({"error": "同一场比赛不能重复加入串关"}, 400)
                    seen.add(event_id)
                    event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                    if not event:
                        return self.send_json({"error": "串关中包含不存在的赛事"}, 400)
                    if not is_upcoming_event(event["kickoff_at"]):
                        return self.send_json({"error": f"{event['home_team']} vs {event['away_team']} 已过开赛时间"}, 400)
                    prediction = predict(event)
                    probability = prediction["win_draw_loss"][selection]
                    odds = None
                    quotes = conn.execute("SELECT payload FROM market_quotes WHERE event_id=?", (event["id"],)).fetchall()
                    parsed_quotes = []
                    for quote_row in quotes:
                        try:
                            parsed_quotes.append(json.loads(quote_row["payload"]))
                        except json.JSONDecodeError:
                            continue
                    if parsed_quotes:
                        parsed_quotes.sort(key=lambda quote: datetime.fromisoformat(quote["captured_at"]))
                        odds = parsed_quotes[-1].get("odds", {}).get({"主胜": "home", "平局": "draw", "客胜": "away"}[selection])
                    output.append({
                        "event": localized_event(event),
                        "selection": selection,
                        "probability": probability,
                        "odds": odds,
                    })
            finally:
                conn.close()
            combined_probability = 1.0
            combined_odds = 1.0
            has_all_odds = True
            for item in output:
                combined_probability *= item["probability"]
                if item["odds"] is None:
                    has_all_odds = False
                else:
                    combined_odds *= item["odds"]
            return self.send_json({
                "ok": True,
                "type": f"{len(output)}串1",
                "legs": output,
                "combined_probability": combined_probability,
                "combined_odds": combined_odds if has_all_odds else None,
            })
        if path == '/api/lottery-settlement':
            try:
                for key in ('home_score', 'away_score'):
                    if type(body.get(key)) is not int or not 0 <= body[key] <= 50:
                        raise ValueError('比分必须是 0–50 的整数')
                result = lottery_result(body['home_score'], body['away_score'], body.get('lottery_handicap'))
                return self.send_json({'result': result})
            except ValueError as exc:
                return self.send_json({'error': str(exc)}, 400)
        if path == "/api/predict":
            conn = db()
            event = conn.execute("SELECT * FROM events WHERE id=?", (body.get("event_id"),)).fetchone()
            if not event:
                conn.close()
                return self.send_json({"error": "赛事不存在"}, 404)
            lottery_handicap = body.get("lottery_handicap")
            if lottery_handicap is not None:
                if type(lottery_handicap) is not int or not -20 <= lottery_handicap <= 20:
                    conn.close()
                    return self.send_json({"error": "竞彩让球必须是 -20 到 20 的整数"}, 400)
            conn.close()
            result = predict(
                event,
                lottery_handicap=lottery_handicap,
                refresh_context=bool(body.get("refresh_context")),
            )
            save_conn = db()
            try:
                save_prediction(save_conn, event, result)
                save_conn.commit()
            finally:
                save_conn.close()
            return self.send_json({"event": localized_event(event), "prediction": result})
        if path == "/api/shortlist/predict":
            allowed_markets = {"win_draw_loss", "handicap", "total_goals", "half_full", "score"}
            markets = body.get("markets")
            if not isinstance(markets, list) or not markets:
                return self.send_json({"error": "至少选择一个预测结果类型"}, 400)
            markets = [market for market in markets if market in allowed_markets]
            if not markets:
                return self.send_json({"error": "预测结果类型无效"}, 400)
            from_date = body.get("from_date") or ""
            to_date = body.get("to_date") or ""
            for value in (from_date, to_date):
                if not isinstance(value, str) or (value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
                    return self.send_json({"error": "日期格式必须是 YYYY-MM-DD"}, 400)
            if from_date and to_date and from_date > to_date:
                return self.send_json({"error": "开始日期不能晚于结束日期"}, 400)
            event_ids = body.get("event_ids")
            normalized_ids = []
            if event_ids is not None:
                if not isinstance(event_ids, list) or not event_ids or len(event_ids) > 50:
                    return self.send_json({"error": "请提供 1–50 个赛事编号"}, 400)
                for event_id in event_ids:
                    if type(event_id) is not int or event_id <= 0:
                        return self.send_json({"error": "赛事编号必须是正整数"}, 400)
                    if event_id not in normalized_ids:
                        normalized_ids.append(event_id)
            conn = db()
            sql = """SELECT e.*, s.lottery_handicap FROM shortlist s
                     JOIN events e ON e.id=s.event_id"""
            values = []
            filters = []
            if from_date:
                filters.append("substr(e.kickoff_at, 1, 10) >= ?")
                values.append(from_date)
            if to_date:
                filters.append("substr(e.kickoff_at, 1, 10) <= ?")
                values.append(to_date)
            if normalized_ids:
                placeholders = ','.join('?' for _ in normalized_ids)
                filters.append(f"e.id IN ({placeholders})")
                values.extend(normalized_ids)
            if filters:
                sql += " WHERE " + " AND ".join(filters)
            sql += " ORDER BY e.kickoff_at"
            events = [row for row in conn.execute(sql, values).fetchall() if is_upcoming_event(row["kickoff_at"])]
            conn.close()
            if not events:
                return self.send_json({"error": "当前日期范围内没有候选比赛" if from_date or to_date else "候选池为空，请先加入比赛"}, 400)
            output = []
            for event in events:
                result = predict(event, lottery_handicap=event["lottery_handicap"])
                save_conn = db()
                try:
                    save_prediction(save_conn, event, result)
                    save_conn.commit()
                finally:
                    save_conn.close()
                output.append({"event": localized_event(event), "prediction": result})
            return self.send_json({"ok": True, "markets": markets, "from_date": from_date, "to_date": to_date, "count": len(output), "predictions": output})
        if path == "/api/results":
            event_id = body.get("event_id")
            home, away = body.get("home_score"), body.get("away_score")
            if not isinstance(home, int) or not isinstance(away, int) or home < 0 or away < 0:
                return self.send_json({"error": "比分必须是非负整数"}, 400)
            conn = db()
            conn.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?)", (event_id, home, away, datetime.now(timezone.utc).isoformat()))
            prediction_rows = conn.execute("SELECT id FROM predictions WHERE event_id=?", (event_id,)).fetchall()
            for prediction_row in prediction_rows:
                record_evaluation(conn, prediction_row["id"], home, away)
            conn.commit()
            conn.close()
            return self.send_json({"ok": True})
        if path == "/api/sync/results":
            event_ids = body.get("event_ids")
            if event_ids is not None:
                if not isinstance(event_ids, list) or len(event_ids) > 100:
                    return self.send_json({"error": "赛事编号必须是 0–100 个的数组"}, 400)
                if any(type(event_id) is not int or event_id <= 0 for event_id in event_ids):
                    return self.send_json({"error": "赛事编号必须是正整数"}, 400)
                event_ids = list(dict.fromkeys(event_ids))
            with db() as conn:
                if event_ids:
                    placeholders = ",".join("?" for _ in event_ids)
                    rows = conn.execute(
                        f"""SELECT DISTINCT e.* FROM events e
                            JOIN predictions p ON p.event_id=e.id
                            LEFT JOIN results r ON r.event_id=e.id
                            WHERE e.id IN ({placeholders}) AND r.event_id IS NULL""",
                        event_ids,
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT DISTINCT e.* FROM events e
                           JOIN predictions p ON p.event_id=e.id
                           LEFT JOIN results r ON r.event_id=e.id
                           WHERE r.event_id IS NULL"""
                    ).fetchall()
            events = [dict(row) for row in rows if not is_upcoming_event(row["kickoff_at"])]
            if not events:
                return self.send_json({"ok": True, "updated": 0, "evaluated": 0, "request_count": 0, "warnings": ["暂时没有待同步的已结束预测"]})
            try:
                batch = fetch_api_football_results(events)
            except DataSourceError as exc:
                return self.send_json({"error": str(exc)}, 400)
            updated = 0
            evaluated = 0
            conn = db()
            try:
                for result in batch["results"]:
                    conn.execute(
                        "INSERT OR REPLACE INTO results VALUES(?,?,?,?)",
                        (result["event_id"], result["home_score"], result["away_score"], datetime.now(timezone.utc).isoformat()),
                    )
                    prediction_rows = conn.execute(
                        "SELECT id FROM predictions WHERE event_id=?", (result["event_id"],)
                    ).fetchall()
                    for prediction_row in prediction_rows:
                        evaluated += int(record_evaluation(conn, prediction_row["id"], result["home_score"], result["away_score"]))
                    updated += 1
                conn.commit()
            finally:
                conn.close()
            return self.send_json({
                "ok": True,
                "updated": updated,
                "evaluated": evaluated,
                "request_count": batch.get("request_count", 0),
                "quota_remaining": batch.get("quota_remaining"),
                "warnings": batch.get("warnings", []),
            })
        if path == "/api/optimize":
            conn = db()
            result = optimize_calibration(conn)
            conn.commit(); conn.close()
            return self.send_json(result)
        if path == "/api/import-events":
            rows = body.get("events")
            if not isinstance(rows, list) or not rows:
                return self.send_json({"error": "events 必须是非空数组"}, 400)
            conn = db()
            now = datetime.now(timezone.utc).isoformat()
            inserted = 0
            for row in rows:
                required = [row.get(k) for k in ("league", "home_team", "away_team", "kickoff_at")]
                if any(not isinstance(v, str) or not v.strip() for v in required):
                    continue
                conn.execute("""INSERT INTO events(league,home_team,away_team,kickoff_at,handicap,source,updated_at)
                    VALUES(?,?,?,?,?,?,?)""", (row["league"], row["home_team"], row["away_team"], row["kickoff_at"], float(row.get("handicap", 0)), row.get("source", "import"), now))
                inserted += 1
            conn.commit(); conn.close()
            return self.send_json({"ok": True, "inserted": inserted})
        if path == "/api/sync/public":
            try:
                days = min(max(int(body.get("days", 14)), 1), 30)
            except (TypeError, ValueError):
                return self.send_json({"error": "同步天数必须是 1–30 的整数"}, 400)
            batches = []
            warnings = []
            sources = []
            for provider, fetcher in (
                ("football-data.org", fetch_upcoming_matches),
                ("API-Football", fetch_api_football_matches),
            ):
                try:
                    batch = fetcher(days=days, with_meta=True)
                    batches.extend(batch["events"])
                    warnings.extend(batch.get("warnings", []))
                    sources.append({
                        "provider": provider,
                        "status": "ok",
                        "events": len(batch["events"]),
                        "requests": batch.get("request_count", 0),
                        "quota_remaining": batch.get("quota_remaining"),
                    })
                except DataSourceError as exc:
                    warnings.append(str(exc))
                    sources.append({"provider": provider, "status": "skipped", "events": 0, "requests": 0})
            if not any(source["status"] == "ok" for source in sources):
                return self.send_json({"error": "；".join(warnings), "sources": sources}, 400)
            changed = upsert_events(batches)
            return self.send_json({
                "ok": True,
                "imported": changed["inserted"],
                "updated": changed["updated"],
                "total": len(batches),
                "request_count": sum(source["requests"] for source in sources),
                "warnings": warnings,
                "sources": sources,
            })
        if path == "/api/sync/football-data":
            try:
                batch = fetch_upcoming_matches(
                    days=min(max(int(body.get("days", 14)), 1), 30),
                    with_meta=True,
                )
            except (DataSourceError, TypeError, ValueError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            changed = upsert_events(batch["events"])
            return self.send_json({
                "ok": True,
                "imported": changed["inserted"],
                "updated": changed["updated"],
                "total": len(batch["events"]),
                "request_count": batch["request_count"],
                "warnings": batch["warnings"],
            })
        return self.send_json({"error": "接口不存在"}, 404)

    def upload_image(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self.send_json({"error": "请使用 multipart/form-data 上传图片"}, 400)
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
            )
            part = next((p for p in message.walk() if p.get_filename()), None)
            if not part:
                return self.send_json({"error": "没有找到图片文件"}, 400)
            mime = part.get_content_type()
            data = part.get_payload(decode=True) or b""
            if mime not in {"image/png", "image/jpeg", "image/webp"} or len(data) > 10 * 1024 * 1024:
                return self.send_json({"error": "仅支持不超过 10MB 的 PNG/JPG/WEBP 图片"}, 400)
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
            image_id = str(uuid.uuid4())
            output_path = UPLOAD_DIR / f"{image_id}{suffix}"
            output_path.write_bytes(data)
            try:
                ocr = local_ocr(output_path)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                return self.send_json({"image_id": image_id, "status": "uploaded", "ocr_status": "unavailable", "message": f"图片已保存，但本地 OCR 暂不可用：{exc}"})
            text = "\n".join(item["text"] for item in ocr.get("lines", []))
            return self.send_json({
                "image_id": image_id,
                "status": "uploaded",
                "ocr_status": "completed",
                "text": text,
                "lines": ocr.get("lines", []),
                "candidates": match_events(text),
                "message": "图片已识别，请确认候选赛事。",
            })
        except Exception as exc:
            return self.send_json({"error": f"图片处理失败：{exc}"}, 400)

    def serve_file(self, path, content_type):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return self.send_json({"error": "资源不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", content_type or "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("球赛智析运行于 http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
