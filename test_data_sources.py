import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from data_sources import (
    DataSourceError,
    fetch_api_football_context,
    fetch_api_football_matches,
    fetch_api_football_odds,
    fetch_api_football_results,
    fetch_upcoming_matches,
)


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class DataSourceTests(unittest.TestCase):
    def test_football_data_includes_national_tournament_metadata(self):
        payload = {
            "matches": [{
                "id": 123,
                "status": "TIMED",
                "utcDate": "2026-06-11T19:00:00Z",
                "stage": "GROUP_STAGE",
                "competition": {"name": "FIFA World Cup"},
                "homeTeam": {"name": "Team A"},
                "awayTeam": {"name": "Team B"},
            }]
        }
        with patch("data_sources.urlopen", return_value=FakeResponse(payload)):
            result = fetch_upcoming_matches(
                token="test",
                competitions=("WC",),
                days=14,
                now=datetime(2026, 6, 1, tzinfo=timezone.utc),
                with_meta=True,
            )
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["events"][0]["competition_type"], "national_team")
        self.assertEqual(result["events"][0]["stage"], "GROUP_STAGE")

    def test_api_football_discovers_then_fetches_only_target_tournament(self):
        catalogue = {
            "errors": {},
            "response": [
                {
                    "league": {"id": 7, "name": "Asian Cup"},
                    "seasons": [{"year": 2027, "start": "2027-01-01", "end": "2027-02-15"}],
                },
                {
                    "league": {"id": 999, "name": "Unrelated Cup"},
                    "seasons": [{"year": 2027, "start": "2027-01-01", "end": "2027-02-15"}],
                },
            ],
        }
        fixtures = {
            "errors": {},
            "response": [{
                "fixture": {
                    "id": 456,
                    "date": "2027-01-08T12:00:00+00:00",
                    "status": {"short": "NS"},
                    "venue": {"name": "Neutral Stadium"},
                },
                "league": {"name": "Asian Cup", "round": "Group Stage"},
                "teams": {"home": {"name": "Team C"}, "away": {"name": "Team D"}},
            }],
        }
        responses = [
            FakeResponse(catalogue, {"x-ratelimit-requests-remaining": "99"}),
            FakeResponse(fixtures, {"x-ratelimit-requests-remaining": "98"}),
        ]
        with patch("data_sources.urlopen", side_effect=responses):
            result = fetch_api_football_matches(
                token="test",
                competitions=("Asian Cup",),
                days=14,
                now=datetime(2027, 1, 1, tzinfo=timezone.utc),
                with_meta=True,
            )
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["quota_remaining"], 98)
        self.assertEqual(result["matched_competitions"], ["Asian Cup"])
        self.assertEqual(result["events"][0]["source"], "api-football.com")
        self.assertEqual(result["events"][0]["is_neutral"], 1)

    def test_api_football_falls_back_to_date_query_when_current_season_is_restricted(self):
        catalogue_error = {
            "errors": {"plan": "Free plans do not have access to this season, try from 2022 to 2024."},
            "response": [],
        }
        date_fixtures = {
            "errors": {},
            "response": [{
                "fixture": {
                    "id": 789,
                    "date": "2027-01-08T12:00:00+00:00",
                    "status": {"short": "NS"},
                    "venue": {"name": "Europa Stadium"},
                },
                "league": {"name": "UEFA Europa League", "round": "League Stage"},
                "teams": {"home": {"name": "Team C"}, "away": {"name": "Team D"}},
            }],
        }
        with patch("data_sources.urlopen", side_effect=[
            FakeResponse(catalogue_error, {"x-ratelimit-requests-remaining": "99"}),
            FakeResponse(date_fixtures, {"x-ratelimit-requests-remaining": "98"}),
        ]):
            result = fetch_api_football_matches(
                token="test",
                competitions=("UEFA Europa League",),
                days=0,
                now=datetime(2027, 1, 8, tzinfo=timezone.utc),
                with_meta=True,
            )
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["quota_remaining"], 98)
        self.assertEqual(result["events"][0]["league"], "UEFA Europa League")

    def test_api_football_requires_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(DataSourceError):
                fetch_api_football_matches()

    def test_api_football_odds_parses_match_winner(self):
        payload = {
            "errors": {},
            "response": [{
                "bookmakers": [{
                    "id": 6,
                    "name": "Bwin",
                    "last_update": "2027-01-08T10:30:00+00:00",
                    "bets": [{
                        "id": 1,
                        "name": "Match Winner",
                        "values": [
                            {"value": "Home", "odd": "2.00"},
                            {"value": "Draw", "odd": "3.20"},
                            {"value": "Away", "odd": "4.00"},
                        ],
                    }],
                }],
            }],
        }
        with patch("data_sources.urlopen", return_value=FakeResponse(
            payload, {"x-ratelimit-requests-remaining": "97"}
        )):
            result = fetch_api_football_odds({
                "source": "api-football.com",
                "source_id": "456",
                "home_team": "Team C",
                "away_team": "Team D",
                "kickoff_at": "2027-01-08 12:00:00 UTC",
            }, token="test")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["quota_remaining"], 97)
        self.assertEqual(result["fixture_id"], "456")
        self.assertEqual(result["quotes"][0]["bookmaker"], "Bwin")
        self.assertEqual(result["quotes"][0]["home"], 2.0)

    def test_api_football_odds_matches_football_data_fixture(self):
        fixture_lookup = {
            "errors": {},
            "response": [{
                "fixture": {"id": 456, "date": "2027-01-08T12:00:00+00:00"},
                "teams": {"home": {"name": "Team C FC"}, "away": {"name": "Team D"}},
            }],
        }
        odds = {
            "errors": {},
            "response": [{
                "bookmakers": [{
                    "name": "Bookmaker X",
                    "bets": [{
                        "name": "1X2",
                        "values": [
                            {"value": "1", "odd": "1.80"},
                            {"value": "X", "odd": "3.50"},
                            {"value": "2", "odd": "4.50"},
                        ],
                    }],
                }],
            }],
        }
        responses = [
            FakeResponse(fixture_lookup, {"x-ratelimit-requests-remaining": "99"}),
            FakeResponse(odds, {"x-ratelimit-requests-remaining": "98"}),
        ]
        with patch("data_sources.urlopen", side_effect=responses):
            result = fetch_api_football_odds({
                "source": "football-data.org",
                "source_id": "123",
                "home_team": "Team C",
                "away_team": "Team D",
                "kickoff_at": "2027-01-08 12:00:00 UTC",
            }, token="test")
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["fixture_id"], "456")
        self.assertEqual(result["quota_remaining"], 98)
        self.assertEqual(result["quotes"][0]["away"], 4.5)

    def test_api_football_results_batches_by_date_and_parses_fulltime_score(self):
        payload = {
            "errors": {},
            "response": [
                {
                    "fixture": {"id": 456, "date": "2027-01-08T12:00:00+00:00", "status": {"short": "FT"}},
                    "teams": {"home": {"name": "Team C FC"}, "away": {"name": "Team D"}},
                    "score": {"fulltime": {"home": 2, "away": 1}},
                },
                {
                    "fixture": {"id": 457, "date": "2027-01-08T15:00:00+00:00", "status": {"short": "NS"}},
                    "teams": {"home": {"name": "Team E"}, "away": {"name": "Team F"}},
                    "score": {"fulltime": {"home": None, "away": None}},
                },
            ],
        }
        with patch("data_sources.urlopen", return_value=FakeResponse(
            payload, {"x-ratelimit-requests-remaining": "96"}
        )):
            result = fetch_api_football_results([{
                "id": 10,
                "source": "football-data.org",
                "source_id": "123",
                "home_team": "Team C",
                "away_team": "Team D",
                "kickoff_at": "2027-01-08 12:00:00 UTC",
            }], token="test")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["quota_remaining"], 96)
        self.assertEqual(result["results"][0]["event_id"], 10)
        self.assertEqual(result["results"][0]["home_score"], 2)
        self.assertEqual(result["results"][0]["away_score"], 1)

    def test_api_football_context_parses_stats_injuries_and_lineups(self):
        fixture = {
            "errors": {},
            "response": [{
                "fixture": {"id": 456, "date": "2027-01-08T12:00:00+00:00"},
                "league": {"id": 39, "name": "Premier League", "season": 2026},
                "teams": {
                    "home": {"id": 10, "name": "Team C"},
                    "away": {"id": 20, "name": "Team D"},
                },
            }],
        }
        stats = lambda home, away: {
            "errors": {},
            "response": {
                "goals": {
                    "for": {"average": {"home": str(home), "away": str(away)}},
                    "against": {"average": {"home": "0.8", "away": "1.1"}},
                },
                "form": "WWDLW",
            },
        }
        injuries = {
            "errors": {},
            "response": [{
                "player": {"id": 99, "name": "Unavailable Player"},
                "team": {"id": 10, "name": "Team C"},
                "type": "Missing Fixture",
                "reason": "Suspended",
            }],
        }
        lineups = {
            "errors": {},
            "response": [
                {"team": {"id": 10, "name": "Team C"}, "formation": "4-3-3", "startXI": [{"player": {"id": 1, "name": "Starter C", "number": 9, "pos": "F"}}], "substitutes": []},
                {"team": {"id": 20, "name": "Team D"}, "formation": "4-4-2", "startXI": [{"player": {"id": 2, "name": "Starter D", "number": 10, "pos": "M"}}], "substitutes": []},
            ],
        }
        responses = [
            FakeResponse(fixture),
            FakeResponse(stats(1.8, 1.2)),
            FakeResponse(stats(1.4, 1.0)),
            FakeResponse(injuries),
            FakeResponse(lineups),
        ]
        with patch("data_sources.urlopen", side_effect=responses):
            result = fetch_api_football_context({
                "source": "api-football.com",
                "source_id": "456",
                "league": "Premier League",
                "home_team": "Team C",
                "away_team": "Team D",
                "kickoff_at": "2027-01-08 12:00:00 UTC",
            }, token="test")
        self.assertEqual(result["request_count"], 5)
        self.assertEqual(result["home_stats"]["home_attack"], 1.8)
        self.assertEqual(result["away_stats"]["away_attack"], 1.0)
        self.assertEqual(result["availability"]["home_injury_count"], 1)
        self.assertTrue(result["lineups"]["home"]["startXI"])
        self.assertEqual(len(result["team_cache"]), 2)


if __name__ == "__main__":
    unittest.main()
