import unittest

from bet_matcher import BetMatcher
from ibetcoin_reader import parse_bet_row


class ParseBetRowTests(unittest.TestCase):
    def test_parse_total_compact_line(self):
        raw = "STRAIGHT BET [590] TOTAL u221½-105 (ORL MAGIC vrs DET PISTONS)"
        bet = parse_bet_row(raw)
        self.assertIsNotNone(bet)
        self.assertEqual(bet.ticket_id, "B590")
        self.assertEqual(bet.bet_side, "under")
        self.assertAlmostEqual(bet.line, 221.5)
        self.assertEqual(bet.odds_american, -105)
        self.assertIn("ORL MAGIC", bet.event)
        self.assertIn("DET PISTONS", bet.event)

    def test_parse_spread_without_ticket_text(self):
        raw = "STRAIGHT BET [589] ORL MAGIC +8½-105 (NBA PLAYOFFS)"
        bet = parse_bet_row(raw)
        self.assertIsNotNone(bet)
        self.assertEqual(bet.ticket_id, "B589")
        self.assertEqual(bet.market, "Spread")
        self.assertEqual(bet.event, "ORL MAGIC")
        self.assertEqual(bet.odds_american, -105)

    def test_parse_compact_prop_text(self):
        raw = (
            "STRAIGHT BETApr 19 9:12 PMMU [94591] "
            "POR TRAIL BLAZERS GET 30PTS 1ST +200 "
            "(POR TRAIL BLAZERS GET 30PTS 1ST vrs SA SPURS GET 30PTS 1ST)"
        )
        bet = parse_bet_row(raw)
        self.assertIsNotNone(bet)
        self.assertEqual(bet.ticket_id, "B94591")
        self.assertEqual(bet.market, "Prop")
        self.assertEqual(bet.odds_american, 200)
        self.assertIn("POR TRAIL BLAZERS", bet.event)
        self.assertIn("SA SPURS", bet.event)


class BetMatcherSlippageTests(unittest.TestCase):
    def setUp(self):
        self.matcher = BetMatcher(similarity_threshold=75, line_slippage=1.0, juice_slippage=20)
        self.target = {
            "event": "POR TRAIL BLAZERS vs SA SPURS",
            "market": "Total",
            "selection": "Under 223.5",
            "bet_side": "under",
            "line": 223.5,
            "odds_american": -105,
        }

    def test_under_accepts_within_line_and_juice_slippage(self):
        candidate = {
            "event": "POR TRAIL BLAZERS vs SA SPURS",
            "market": "Total",
            "selection": "Under 223",
            "odds_american": -115,
        }
        is_exact, is_similar, _score = self.matcher.match(self.target, candidate)
        self.assertFalse(is_exact)
        self.assertTrue(is_similar)

    def test_under_rejects_when_line_too_low(self):
        candidate = {
            "event": "POR TRAIL BLAZERS vs SA SPURS",
            "market": "Total",
            "selection": "Under 222",
            "odds_american": -115,
        }
        is_exact, is_similar, _score = self.matcher.match(self.target, candidate)
        self.assertFalse(is_exact)
        self.assertFalse(is_similar)

    def test_under_rejects_when_juice_slips_too_far(self):
        candidate = {
            "event": "POR TRAIL BLAZERS vs SA SPURS",
            "market": "Total",
            "selection": "Under 223.5",
            "odds_american": -140,
        }
        is_exact, is_similar, _score = self.matcher.match(self.target, candidate)
        self.assertFalse(is_exact)
        self.assertFalse(is_similar)


if __name__ == "__main__":
    unittest.main()
