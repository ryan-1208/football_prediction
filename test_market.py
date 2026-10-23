import unittest
from market import analyze_quote, lottery_result


class MarketTests(unittest.TestCase):
    def quote(self, **changes):
        return dict(source='test', captured_at='2025-01-01T00:00:00+00:00', home=2, draw=3.5, away=4, **changes)

    def test_probabilities(self):
        q = analyze_quote(self.quote())
        self.assertAlmostEqual(sum(q['probabilities'].values()), 1)
        self.assertAlmostEqual(q['probabilities']['主胜'], 14/29)
        self.assertEqual(q['selection'], '主胜')

    def test_invalid(self):
        for field, value in [('home', float('nan')), ('draw', 1), ('away', True), ('captured_at', '2025-01-01'), ('source', '')]:
            q = self.quote(); q[field] = value
            with self.assertRaises(ValueError): analyze_quote(q)

    def test_lottery_is_separate(self):
        self.assertEqual(lottery_result(2, 1, -1), '让平')
        self.assertEqual(lottery_result(0, 0, 1), '让胜')
        with self.assertRaises(ValueError): lottery_result(2, 1, -0.5)


if __name__ == '__main__':
    unittest.main()
