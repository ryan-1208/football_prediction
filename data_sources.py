"""External football data source adapters.

Tokens remain in the process environment, never in the browser or SQLite.
All providers are queried in batches by competition and date range.
"""

import json
import os
import re
import ssl
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
API_FOOTBALL_FREE_STATS_SEASONS = (2024, 2023, 2022)

# football-data.org free coverage includes the five major leagues, Champions
# League, FIFA World Cup and European Championship.
DEFAULT_COMPETITIONS = ("PL", "PD", "BL1", "SA", "FL1", "CL", "WC", "EC")

# These tournaments supplement football-data.org. World Cup and Euro are not
# repeated here, otherwise the same fixture would be imported from two sources.
DEFAULT_API_FOOTBALL_COMPETITIONS = (
    "Asian Cup",
    "Asian Games",
    "Copa America",
    "Africa Cup of Nations",
    "Olympics Men",
    "FIFA Club World Cup",
    "J1 League",
    "J2 League",
    "UEFA Europa League",
    "UEFA Conference League",
    "UEFA Europa Conference League",
    "CONMEBOL Libertadores",
)

API_FOOTBALL_LEAGUE_COMPETITIONS = frozenset({"j1 league", "j2 league"})
API_FOOTBALL_NATIONAL_COMPETITIONS = frozenset({
    "asian cup", "asian games", "copa america", "africa cup of nations", "olympics men",
})

# 赛前攻防、首发和伤停只对高关注赛事开放。低级别联赛不进入这条
# API-Football 逐场上下文链路，避免无谓调用和覆盖不稳定的数据。
API_FOOTBALL_CONTEXT_LEAGUES = frozenset({
    "premier league",
    "la liga",
    "primera division",
    "primera división",
    "prime division",
    "bundesliga",
    "serie a",
    "ligue 1",
    "uefa champions league",
    "uefa europa league",
    "uefa conference league",
    "uefa europa conference league",
    "fifa world cup",
    "world cup",
    "european championship",
    "euro championship",
    "asian cup",
    "asian games",
    "copa america",
    "africa cup of nations",
    "olympics men",
    "fifa club world cup",
    "j1 league",
    "j2 league",
    "conmebol libertadores",
})


class DataSourceError(RuntimeError):
    pass


def _error_message(payload):
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, dict):
        return "；".join(f"{key}: {value}" for key, value in errors.items() if value)
    if isinstance(errors, list):
        return "；".join(str(value) for value in errors if value)
    if errors:
        return str(errors)
    return ""


def _request_json(url, headers, source):
    request = Request(url, headers={**headers, "Accept": "application/json", "User-Agent": "football-predict/0.2"})
    try:
        with urlopen(request, timeout=25, context=_https_context()) as response:
            payload = json.load(response)
            response_headers = {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = _error_message(payload) or payload.get("message")
        except Exception:
            detail = None
        raise DataSourceError(f"{source} 请求失败（HTTP {exc.code}）{f'：{detail}' if detail else ''}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise DataSourceError(f"{source} 请求失败：{exc}") from exc
    detail = _error_message(payload)
    if detail:
        raise DataSourceError(f"{source} 返回错误：{detail}")
    return payload, response_headers


def _https_context():
    """Use a local CA bundle when the macOS Python build has none configured."""
    for cafile in (
        os.getenv("SSL_CERT_FILE"),
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
    ):
        if cafile and os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _date_range(days, now=None):
    start = (now or datetime.now(timezone.utc)).date()
    return start, start + timedelta(days=days)


def _quota_remaining(headers):
    value = headers.get("x-ratelimit-requests-remaining")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fetch_upcoming_matches(token=None, competitions=DEFAULT_COMPETITIONS, days=14, now=None, with_meta=False):
    """Fetch upcoming club and major international fixtures from football-data.org."""
    token = token or os.getenv("FOOTBALL_DATA_TOKEN")
    if not token:
        raise DataSourceError("未设置 FOOTBALL_DATA_TOKEN，无法同步 football-data.org 数据。")
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DataSourceError("FOOTBALL_DATA_TOKEN 含有非 ASCII 字符，已跳过 football-data.org。") from exc
    start, end = _date_range(days, now)
    imported = []
    warnings = []
    request_count = 0
    for code in competitions:
        query = urlencode({"dateFrom": start.isoformat(), "dateTo": end.isoformat()})
        url = f"{FOOTBALL_DATA_BASE_URL}/competitions/{code}/matches?{query}"
        request_count += 1
        try:
            payload, _ = _request_json(url, {"X-Auth-Token": token}, f"football-data.org/{code}")
        except DataSourceError as exc:
            warnings.append(str(exc))
            continue
        for match in payload.get("matches", []):
            if match.get("status") not in {"SCHEDULED", "TIMED"}:
                continue
            utc_time = match.get("utcDate")
            if not utc_time:
                continue
            competition = match.get("competition") or {}
            imported.append({
                "source_id": str(match.get("id")),
                "league": competition.get("name", code),
                "home_team": (match.get("homeTeam") or {}).get("name"),
                "away_team": (match.get("awayTeam") or {}).get("name"),
                "kickoff_at": utc_time.replace("T", " ").replace("Z", " UTC"),
                "status": "scheduled",
                "source": "football-data.org",
                "competition_type": "national_team" if code in {"WC", "EC"} else "club",
                "stage": match.get("stage"),
                "venue": None,
                "is_neutral": 1 if code in {"WC", "EC"} else 0,
            })
    imported = [row for row in imported if row["home_team"] and row["away_team"]]
    if not imported and warnings and len(warnings) == request_count:
        raise DataSourceError("；".join(warnings))
    result = {
        "events": imported,
        "warnings": warnings,
        "request_count": request_count,
        "quota_remaining": None,
        "provider": "football-data.org",
    }
    return result if with_meta else imported


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _season_overlaps(season, start, end):
    season_start = _parse_date(season.get("start"))
    season_end = _parse_date(season.get("end"))
    if season_start and season_start > end:
        return False
    if season_end and season_end < start:
        return False
    return True


def _scheduled_api_fixture_event(item, fallback_competition=None):
    fixture = item.get("fixture") or {}
    status = (fixture.get("status") or {}).get("short")
    if status not in {"TBD", "NS", "PST"}:
        return None
    teams = item.get("teams") or {}
    league = item.get("league") or {}
    venue = fixture.get("venue") or {}
    utc_time = fixture.get("date")
    home_team = (teams.get("home") or {}).get("name")
    away_team = (teams.get("away") or {}).get("name")
    if not home_team or not away_team or not utc_time:
        return None
    competition_name = league.get("name") or fallback_competition
    if not competition_name:
        return None
    return {
        "source_id": str(fixture.get("id")),
        "league": competition_name,
        "home_team": home_team,
        "away_team": away_team,
        "kickoff_at": utc_time.replace("T", " ").replace("+00:00", " UTC"),
        "status": "postponed" if status == "PST" else "scheduled",
        "source": "api-football.com",
        "competition_type": "club",
        "stage": league.get("round"),
        "venue": venue.get("name"),
        "is_neutral": 1,
    }


def fetch_api_football_matches(
    token=None,
    competitions=DEFAULT_API_FOOTBALL_COMPETITIONS,
    days=14,
    now=None,
    with_meta=False,
):
    """Fetch selected international tournaments from API-Football.

    One league-catalogue request is made per calendar year in the date range,
    followed by one fixtures request only for each matching active tournament.
    """
    token = token or os.getenv("API_FOOTBALL_KEY")
    if not token:
        raise DataSourceError("未设置 API_FOOTBALL_KEY，已跳过 API-Football 赛事。")
    start, end = _date_range(days, now)
    wanted = {name.casefold(): name for name in competitions}
    request_count = 0
    warnings = []
    quota_remaining = None
    selected = {}

    catalogue_types = {"Cup"}
    if any(name.casefold() in API_FOOTBALL_LEAGUE_COMPETITIONS for name in competitions):
        catalogue_types.add("League")
    for season_year in range(start.year, end.year + 1):
        for competition_type in sorted(catalogue_types):
            query = urlencode({"season": season_year, "type": competition_type})
            request_count += 1
            try:
                payload, headers = _request_json(
                    f"{API_FOOTBALL_BASE_URL}/leagues?{query}",
                    {"x-apisports-key": token},
                    "API-Football/赛事目录",
                )
            except DataSourceError as exc:
                warnings.append(str(exc))
                continue
            remaining = _quota_remaining(headers)
            quota_remaining = remaining if remaining is not None else quota_remaining
            for item in payload.get("response", []):
                league = item.get("league") or {}
                canonical_name = wanted.get(str(league.get("name", "")).casefold())
                if not canonical_name:
                    continue
                season = next((value for value in item.get("seasons", []) if value.get("year") == season_year), None)
                if not season or not _season_overlaps(season, start, end):
                    continue
                selected[(league.get("id"), season_year)] = {
                    "id": league.get("id"),
                    "name": canonical_name,
                    "season": season_year,
                }

    imported = []
    for competition in selected.values():
        if competition["id"] is None:
            continue
        query = urlencode({
            "league": competition["id"],
            "season": competition["season"],
            "from": start.isoformat(),
            "to": end.isoformat(),
            "timezone": "UTC",
        })
        request_count += 1
        try:
            payload, headers = _request_json(
                f"{API_FOOTBALL_BASE_URL}/fixtures?{query}",
                {"x-apisports-key": token},
                f"API-Football/{competition['name']}",
            )
        except DataSourceError as exc:
            warnings.append(str(exc))
            continue
        remaining = _quota_remaining(headers)
        quota_remaining = remaining if remaining is not None else quota_remaining
        for item in payload.get("response", []):
            event = _scheduled_api_fixture_event(item, competition["name"])
            if event:
                if competition["name"].casefold() in API_FOOTBALL_NATIONAL_COMPETITIONS:
                    event["competition_type"] = "national_team"
                imported.append(event)

    if not selected and any("Free plans do not have access to this season" in warning for warning in warnings):
        warnings.append("API-Football 当前赛季目录受免费套餐限制，已切换为按日期查询。")
        cursor = start
        while cursor <= end:
            query = urlencode({"date": cursor.isoformat(), "timezone": "UTC"})
            request_count += 1
            try:
                payload, headers = _request_json(
                    f"{API_FOOTBALL_BASE_URL}/fixtures?{query}",
                    {"x-apisports-key": token},
                    "API-Football/按日期赛事",
                )
            except DataSourceError as exc:
                warnings.append(str(exc))
                cursor += timedelta(days=1)
                continue
            remaining = _quota_remaining(headers)
            quota_remaining = remaining if remaining is not None else quota_remaining
            for item in payload.get("response", []):
                league_name = str((item.get("league") or {}).get("name") or "")
                if league_name.casefold() not in wanted:
                    continue
                event = _scheduled_api_fixture_event(item)
                if event:
                    imported.append(event)
            cursor += timedelta(days=1)
    imported = [row for row in imported if row["home_team"] and row["away_team"] and row["kickoff_at"]]
    result = {
        "events": imported,
        "warnings": warnings,
        "request_count": request_count,
        "quota_remaining": quota_remaining,
        "provider": "API-Football",
        "matched_competitions": [item["name"] for item in selected.values()],
    }
    return result if with_meta else imported


def _normalize_team_name(value):
    """Normalize common provider naming differences before fixture matching."""
    value = str(value or "").casefold()
    value = re.sub(r"\b(football club|fc|cf|afc|sc)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _team_similarity(left, right):
    left = _normalize_team_name(left)
    right = _normalize_team_name(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.9
    return SequenceMatcher(None, left, right).ratio()


def _find_api_football_fixture(event, payload):
    item = _find_api_football_fixture_item(event, payload)
    fixture = item.get("fixture") or {} if item else {}
    return str(fixture.get("id")) if fixture.get("id") is not None else None


def _find_api_football_fixture_item(event, payload):
    expected_date = str(event.get("kickoff_at") or "")[:10]
    response = payload.get("response", []) if isinstance(payload, dict) else []
    if isinstance(response, dict):
        response = [response]
    best = None
    for item in response:
        fixture = item.get("fixture") or {}
        fixture_date = str(fixture.get("date") or "")[:10]
        if expected_date and fixture_date and fixture_date != expected_date:
            continue
        teams = item.get("teams") or {}
        home = (teams.get("home") or {}).get("name")
        away = (teams.get("away") or {}).get("name")
        home_score = _team_similarity(event.get("home_team"), home)
        away_score = _team_similarity(event.get("away_team"), away)
        if home_score < 0.84 or away_score < 0.84:
            continue
        score = min(home_score, away_score)
        if best is None or score > best[0]:
            best = (score, item)
    return best[1] if best else None


def fetch_api_football_results(events, token=None):
    """Fetch completed scores for predicted events with one request per date."""
    token = token or os.getenv("API_FOOTBALL_KEY")
    if not token:
        raise DataSourceError("未设置 API_FOOTBALL_KEY，无法自动同步赛果。")

    headers = {"x-apisports-key": token}
    by_date = {}
    for event in events or []:
        kickoff_date = str(event.get("kickoff_at") or "")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", kickoff_date):
            by_date.setdefault(kickoff_date, []).append(event)

    results = []
    warnings = []
    request_count = 0
    quota_remaining = None
    finished_statuses = {"FT", "AET", "PEN", "AWD", "WO"}
    for kickoff_date, date_events in by_date.items():
        query = urlencode({"date": kickoff_date, "timezone": "UTC"})
        request_count += 1
        try:
            payload, response_headers = _request_json(
                f"{API_FOOTBALL_BASE_URL}/fixtures?{query}",
                headers,
                "API-Football/完赛赛果",
            )
        except DataSourceError as exc:
            warnings.append(str(exc))
            continue
        remaining = _quota_remaining(response_headers)
        quota_remaining = remaining if remaining is not None else quota_remaining
        response = payload.get("response", []) if isinstance(payload, dict) else []
        if isinstance(response, dict):
            response = [response]
        for event in date_events:
            item = None
            if event.get("source") == "api-football.com" and event.get("source_id"):
                item = next(
                    (
                        candidate for candidate in response
                        if str((candidate.get("fixture") or {}).get("id")) == str(event["source_id"])
                    ),
                    None,
                )
            if item is None:
                item = _find_api_football_fixture_item(event, {"response": response})
            if item is None:
                warnings.append(f"{event.get('home_team', '主队')} vs {event.get('away_team', '客队')} 未匹配到 API-Football 比赛")
                continue
            fixture = item.get("fixture") or {}
            status = (fixture.get("status") or {}).get("short")
            if status not in finished_statuses:
                continue
            fulltime = (item.get("score") or {}).get("fulltime") or {}
            home_score = fulltime.get("home")
            away_score = fulltime.get("away")
            if type(home_score) is not int or type(away_score) is not int:
                warnings.append(f"{event.get('home_team', '主队')} vs {event.get('away_team', '客队')} 已结束但没有完整比分")
                continue
            results.append({
                "event_id": event.get("id"),
                "fixture_id": fixture.get("id"),
                "home_score": home_score,
                "away_score": away_score,
                "status": status,
            })
    return {
        "results": results,
        "warnings": warnings,
        "request_count": request_count,
        "quota_remaining": quota_remaining,
        "provider": "API-Football",
    }


def _odds_timestamp(value):
    if value:
        timestamp = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(timestamp)
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def _parse_match_winner_values(values):
    parsed = {}
    for item in values or []:
        name = str(item.get("value") or "").casefold()
        key = "home" if name in {"home", "1"} else "draw" if name in {"draw", "x"} else "away" if name in {"away", "2"} else None
        if key is None:
            continue
        try:
            odd = float(item.get("odd"))
        except (TypeError, ValueError):
            continue
        if odd > 1:
            parsed[key] = odd
    return parsed


def fetch_api_football_odds(event, token=None):
    """Fetch pre-match 1X2 bookmaker odds for one candidate-pool event.

    API-Football fixtures already carry their fixture ID. For fixtures from
    football-data.org, one same-day fixture lookup matches both team names
    before the odds request, avoiding a full competition-wide scan.
    """
    token = token or os.getenv("API_FOOTBALL_KEY")
    if not token:
        raise DataSourceError("未设置 API_FOOTBALL_KEY，无法获取 API-Football 国际赔率。")

    headers = {"x-apisports-key": token}
    request_count = 0
    quota_remaining = None
    warnings = []
    fixture_id = None
    if event.get("source") == "api-football.com" and event.get("source_id"):
        fixture_id = str(event["source_id"])
    else:
        kickoff_date = str(event.get("kickoff_at") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", kickoff_date):
            raise DataSourceError("赛事缺少有效日期，无法匹配 API-Football fixture。")
        query = urlencode({"date": kickoff_date, "timezone": "UTC"})
        request_count += 1
        payload, response_headers = _request_json(
            f"{API_FOOTBALL_BASE_URL}/fixtures?{query}",
            headers,
            "API-Football/赛事匹配",
        )
        quota_remaining = _quota_remaining(response_headers)
        fixture_id = _find_api_football_fixture(event, payload)
        if not fixture_id:
            raise DataSourceError("未在 API-Football 找到同日期、同主客队的比赛，无法获取国际赔率。")

    query = urlencode({"fixture": fixture_id})
    request_count += 1
    payload, response_headers = _request_json(
        f"{API_FOOTBALL_BASE_URL}/odds?{query}",
        headers,
        "API-Football/国际赔率",
    )
    remaining = _quota_remaining(response_headers)
    quota_remaining = remaining if remaining is not None else quota_remaining

    response = payload.get("response", []) if isinstance(payload, dict) else []
    if isinstance(response, dict):
        response = [response]
    quotes = []
    for item in response:
        for bookmaker in item.get("bookmakers") or []:
            bookmaker_name = bookmaker.get("name") or f"ID {bookmaker.get('id')}"
            for bet in bookmaker.get("bets") or []:
                bet_name = str(bet.get("name") or "").casefold()
                if bet_name not in {"match winner", "1x2", "fulltime result"}:
                    continue
                odds = _parse_match_winner_values(bet.get("values"))
                if not all(key in odds for key in ("home", "draw", "away")):
                    continue
                quotes.append({
                    "source": f"API-Football / {bookmaker_name}",
                    "bookmaker": bookmaker_name,
                    "captured_at": _odds_timestamp(bookmaker.get("last_update") or item.get("last_update")),
                    "home": odds["home"],
                    "draw": odds["draw"],
                    "away": odds["away"],
                    "fixture_id": fixture_id,
                })
                break
    if not quotes:
        warnings.append("API-Football 未返回完整的赛前胜平负赔率。")
    return {
        "fixture_id": fixture_id,
        "quotes": quotes,
        "warnings": warnings,
        "request_count": request_count,
        "quota_remaining": quota_remaining,
    }


def supports_api_football_context(event):
    """Return whether an event is in the supported top-level competition set."""
    if event.get("source") not in {"football-data.org", "api-football.com"}:
        return False
    return str(event.get("league") or "").strip().casefold() in API_FOOTBALL_CONTEXT_LEAGUES


def _as_float(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _form_factor(form):
    values = [str(value).upper() for value in str(form or "") if str(value).upper() in {"W", "D", "L"}]
    if not values:
        return 1.0
    points = sum(3 if value == "W" else 1 if value == "D" else 0 for value in values)
    return round(0.92 + 0.16 * (points / (3 * len(values))), 4)


def _parse_team_statistics(payload):
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        return None
    goals = response.get("goals") or {}
    goals_for = goals.get("for") or {}
    goals_against = goals.get("against") or {}
    averages_for = goals_for.get("average") or {}
    averages_against = goals_against.get("average") or {}
    parsed = {
        "home_attack": _as_float(averages_for.get("home")),
        "away_attack": _as_float(averages_for.get("away")),
        "home_defense": _as_float(averages_against.get("home")),
        "away_defense": _as_float(averages_against.get("away")),
        "form": _form_factor(response.get("form")),
    }
    if not all(parsed[key] is not None for key in ("home_attack", "away_attack", "home_defense", "away_defense")):
        return None
    return {key: round(value, 4) for key, value in parsed.items()}


def _side_stats(team_stats, side):
    if not isinstance(team_stats, dict):
        return None
    keys = (f"{side}_attack", f"{side}_defense", "form")
    if not all(_as_float(team_stats.get(key)) is not None for key in keys):
        return None
    return {key: float(team_stats[key]) for key in keys}


def _stats_season_candidates(season):
    try:
        current_season = int(season)
    except (TypeError, ValueError):
        return [season]
    return [current_season] + [value for value in API_FOOTBALL_FREE_STATS_SEASONS if value != current_season]


def _parse_injuries(response, home_team_id, away_team_id):
    home = []
    away = []
    for item in response or []:
        team = item.get("team") or {}
        side = home if team.get("id") == home_team_id else away if team.get("id") == away_team_id else None
        if side is None:
            continue
        player = item.get("player") or {}
        side.append({
            "player_id": player.get("id"),
            "player": player.get("name") or "未知球员",
            "type": item.get("type"),
            "reason": item.get("reason"),
        })
    return {"home": home, "away": away, "available": True}


def _parse_lineups(response, home_team_id, away_team_id):
    parsed = {"home": None, "away": None, "available": bool(response)}
    for item in response or []:
        team = item.get("team") or {}
        side = "home" if team.get("id") == home_team_id else "away" if team.get("id") == away_team_id else None
        if side is None:
            continue
        def players(key):
            values = []
            for row in item.get(key) or []:
                player = row.get("player") or {}
                values.append({
                    "id": player.get("id"),
                    "name": player.get("name") or "未知球员",
                    "number": player.get("number"),
                    "position": player.get("pos"),
                })
            return values
        parsed[side] = {
            "team": team.get("name"),
            "formation": item.get("formation"),
            "coach": (item.get("coach") or {}).get("name"),
            "startXI": players("startXI"),
            "substitutes": players("substitutes"),
        }
    return parsed


def fetch_api_football_context(event, token=None, cached_team_stats=None):
    """Fetch one supported fixture's season stats, injuries and lineups.

    The returned payload is intentionally provider-shaped but normalized enough
    for the local model and UI. Team season statistics may be supplied from the
    caller's cache to avoid repeating the same API request for nearby fixtures.
    """
    token = token or os.getenv("API_FOOTBALL_KEY")
    if not token:
        raise DataSourceError("未设置 API_FOOTBALL_KEY，无法获取赛季攻防、首发和伤停数据。")
    if not supports_api_football_context(event):
        raise DataSourceError("当前赛事不在接入范围，仅支持五大联赛、欧冠和指定国际赛事。")

    headers = {"x-apisports-key": token}
    request_count = 0
    quota_remaining = None
    warnings = []
    if event.get("source") == "api-football.com" and event.get("source_id"):
        query = urlencode({"id": str(event["source_id"])})
    else:
        kickoff_date = str(event.get("kickoff_at") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", kickoff_date):
            raise DataSourceError("赛事缺少有效日期，无法匹配 API-Football fixture。")
        query = urlencode({"date": kickoff_date, "timezone": "UTC"})
    request_count += 1
    payload, response_headers = _request_json(
        f"{API_FOOTBALL_BASE_URL}/fixtures?{query}", headers, "API-Football/赛事匹配"
    )
    quota_remaining = _quota_remaining(response_headers)
    fixture_item = _find_api_football_fixture_item(event, payload)
    if event.get("source") == "api-football.com" and event.get("source_id"):
        response = payload.get("response", []) if isinstance(payload, dict) else []
        if isinstance(response, dict):
            response = [response]
        fixture_item = next((item for item in response if str((item.get("fixture") or {}).get("id")) == str(event["source_id"])), None)
    if not fixture_item:
        raise DataSourceError("未在 API-Football 找到对应比赛，无法获取赛前数据。")

    fixture = fixture_item.get("fixture") or {}
    league = fixture_item.get("league") or {}
    teams = fixture_item.get("teams") or {}
    home_team = teams.get("home") or {}
    away_team = teams.get("away") or {}
    fixture_id = str(fixture.get("id")) if fixture.get("id") is not None else None
    league_id = league.get("id")
    season = league.get("season")
    home_team_id = home_team.get("id")
    away_team_id = away_team.get("id")
    if not all(value is not None for value in (fixture_id, league_id, season, home_team_id, away_team_id)):
        raise DataSourceError("API-Football 比赛缺少联赛、赛季或球队 ID，无法获取赛前数据。")

    cached_team_stats = cached_team_stats or {}
    team_cache = {}
    all_stats = {}
    stats_cache_keys = {}
    stats_seasons = {}
    for side, team_id, team_name in (
        ("home", home_team_id, home_team.get("name")),
        ("away", away_team_id, away_team.get("name")),
    ):
        selected_cache_key = None
        selected_season = None
        stats = None
        errors = []
        for stats_season in _stats_season_candidates(season):
            cache_key = f"{league_id}:{stats_season}:{team_id}"
            stats = cached_team_stats.get(cache_key)
            if isinstance(stats, dict) and isinstance(stats.get("stats"), dict):
                stats = stats["stats"]
            if stats is None:
                query = urlencode({"league": league_id, "season": stats_season, "team": team_id})
                request_count += 1
                try:
                    stats_payload, response_headers = _request_json(
                        f"{API_FOOTBALL_BASE_URL}/teams/statistics?{query}",
                        headers,
                        f"API-Football/{team_name or team_id}赛季攻防",
                    )
                    remaining = _quota_remaining(response_headers)
                    quota_remaining = remaining if remaining is not None else quota_remaining
                    stats = _parse_team_statistics(stats_payload)
                except DataSourceError as exc:
                    errors.append(str(exc))
            if stats is not None:
                selected_cache_key = cache_key
                selected_season = stats_season
                if cache_key not in cached_team_stats:
                    team_cache[cache_key] = {
                        "league_id": league_id,
                        "season": stats_season,
                        "team_id": team_id,
                        "team_name": team_name,
                        "stats": stats,
                    }
                break
        if selected_cache_key is None:
            warnings.extend(errors)
            warnings.append(f"{team_name or team_id} 缺少可用赛季攻防参数")
            all_stats[f"{league_id}:{season}:{team_id}"] = None
        else:
            stats_cache_keys[side] = selected_cache_key
            stats_seasons[side] = selected_season
            all_stats[selected_cache_key] = stats
            if selected_season != season:
                warnings.append(
                    f"{team_name or team_id} 当前赛季攻防不可用，已回退到 {selected_season} 赛季历史参数"
                )

    injuries = {"home": [], "away": [], "available": False}
    request_count += 1
    try:
        injuries_payload, response_headers = _request_json(
            f"{API_FOOTBALL_BASE_URL}/injuries?{urlencode({'fixture': fixture_id})}",
            headers,
            "API-Football/伤停",
        )
        remaining = _quota_remaining(response_headers)
        quota_remaining = remaining if remaining is not None else quota_remaining
        injuries_response = injuries_payload.get("response", []) if isinstance(injuries_payload, dict) else []
        injuries = _parse_injuries(injuries_response, home_team_id, away_team_id)
    except DataSourceError as exc:
        warnings.append(str(exc))

    lineups = {"home": None, "away": None, "available": False}
    request_count += 1
    try:
        lineups_payload, response_headers = _request_json(
            f"{API_FOOTBALL_BASE_URL}/fixtures/lineups?{urlencode({'fixture': fixture_id})}",
            headers,
            "API-Football/首发",
        )
        remaining = _quota_remaining(response_headers)
        quota_remaining = remaining if remaining is not None else quota_remaining
        lineups_response = lineups_payload.get("response", []) if isinstance(lineups_payload, dict) else []
        lineups = _parse_lineups(lineups_response, home_team_id, away_team_id)
        if not lineups["home"] or not lineups["away"]:
            warnings.append("API-Football 尚未返回双方完整首发")
    except DataSourceError as exc:
        warnings.append(str(exc))

    home_stats = _side_stats(all_stats.get(stats_cache_keys.get("home")), "home")
    away_stats = _side_stats(all_stats.get(stats_cache_keys.get("away")), "away")
    home_injuries = len(injuries["home"])
    away_injuries = len(injuries["away"])
    return {
        "fixture_id": fixture_id,
        "league_id": league_id,
        "season": season,
        "team_ids": {"home": home_team_id, "away": away_team_id},
        "home_stats": home_stats,
        "away_stats": away_stats,
        "stats_seasons": stats_seasons,
        "stats_fallback": any(value != season for value in stats_seasons.values()),
        "team_cache": team_cache,
        "injuries": injuries,
        "lineups": lineups,
        "availability": {
            "home_factor": round(max(0.90, 1 - 0.015 * min(home_injuries, 6)), 4),
            "away_factor": round(max(0.90, 1 - 0.015 * min(away_injuries, 6)), 4),
            "home_injury_count": home_injuries,
            "away_injury_count": away_injuries,
        },
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "request_count": request_count,
        "quota_remaining": quota_remaining,
        "warnings": warnings,
    }
