import unittest

from model import build_prediction


class PredictionModelTests(unittest.TestCase):
    def stats(self):
        return {
            "home_attack": 1.45,
            "away_attack": 1.05,
            "home_defense": 0.9,
            "away_defense": 1.1,
            "form": 1.0,
        }

    def test_markets_are_probability_distributions(self):
        result = build_prediction(self.stats(), self.stats(), lottery_handicap=-1)
        self.assertAlmostEqual(sum(result["win_draw_loss"].values()), 1.0)
        self.assertAlmostEqual(sum(result["total_goals"].values()), 1.0)
        self.assertAlmostEqual(sum(result["half_full"].values()), 1.0)
        self.assertAlmostEqual(sum(result["handicap"]["probabilities"].values()), 1.0)
        self.assertTrue(all("/" in key for key in result["half_full"]))
        self.assertTrue(any(key.startswith("胜/") for key in result["half_full"]))

    def test_score_candidates_are_ranked_and_bounded(self):
        result = build_prediction(self.stats(), self.stats())
        candidates = result["score_distribution"]
        self.assertEqual(len(candidates), 5)
        self.assertGreaterEqual(candidates[0]["probability"], candidates[-1]["probability"])
        self.assertLessEqual(sum(item["probability"] for item in candidates), 1.0)
        self.assertGreaterEqual(result["score_other_probability"], 0.0)

    def test_missing_team_data_is_visible(self):
        result = build_prediction(self.stats(), None)
        self.assertFalse(result["data_quality"]["complete"])
        self.assertIn("客队历史攻防参数", result["data_quality"]["missing"])
        self.assertIn("至少一方缺少历史参数", " ".join(result["factors"]))

    def test_context_availability_is_used_and_reported(self):
        context = {
            "availability": {
                "home_factor": 0.97,
                "away_factor": 1.0,
                "home_injury_count": 2,
                "away_injury_count": 0,
            },
            "lineups": {"available": True},
            "injuries": {"available": True},
        }
        result = build_prediction(self.stats(), self.stats(), context=context)
        self.assertTrue(result["data_quality"]["complete"])
        self.assertNotIn("实时首发与伤停", result["data_quality"]["missing"])
        self.assertEqual(result["features"]["home_availability"], 0.97)
        self.assertIn("已使用 API-Football 赛前首发与伤停快照", result["factors"])


if __name__ == "__main__":
    unittest.main()
