"""External football data source adapters.

Tokens remain in the process environment, never in the browser or SQLite.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://api.football-data.org/v4"
DEFAULT_COMPETITIONS = ("PL", "PD", "BL1", "SA", "FL1")


class DataSourceError(RuntimeError):
    pass


def fetch_upcoming_matches(token=None, competitions=DEFAULT_COMPETITIONS, days=14):
    """Fetch upcoming matches from football-data.org's documented v4 endpoint."""
    token = token or os.getenv("FOOTBALL_DATA_TOKEN")
    if not token:
        raise DataSourceError("未设置 FOOTBALL_DATA_TOKEN，无法同步 football-data.org 数据。")
    start = datetime.now(timezone.utc).date()
    end = start + timedelta(days=days)
    imported = []
    for code in competitions:
        query = urlencode({"dateFrom": start.isoformat(), "dateTo": end.isoformat()})
        url = f"{BASE_URL}/competitions/{code}/matches?{query}"
        request = Request(url, headers={"X-Auth-Token": token, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except Exception as exc:
            raise DataSourceError(f"同步 {code} 失败：{exc}") from exc
        for match in payload.get("matches", []):
            if match.get("status") not in {"SCHEDULED", "TIMED"}:
                continue
            utc_time = match.get("utcDate")
            if not utc_time:
                continue
            imported.append({
                "source_id": str(match.get("id")),
                "league": match.get("competition", {}).get("name", code),
                "home_team": match.get("homeTeam", {}).get("name"),
                "away_team": match.get("awayTeam", {}).get("name"),
                "kickoff_at": utc_time.replace("T", " ").replace("Z", " UTC"),
                "source": "football-data.org",
            })
    return [row for row in imported if row["home_team"] and row["away_team"]]
