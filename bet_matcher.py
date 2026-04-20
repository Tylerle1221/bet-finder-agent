"""
bet_matcher.py - Slippage-aware bet matching.
Implements the Over/Under line slippage rules from user specification.

Rule:
  OVER  bet: accept if platform_line <= detected_line + max_slippage
  UNDER bet: accept if platform_line >= detected_line - max_slippage
  Also checks juice/odds slippage.
"""

import logging
import re
from typing import Optional
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

LINE_RE = re.compile(
    r'(OVER|UNDER|O|U)\s*(\d+(?:\.\d+|[½?])?)(?:\s*[+-]\d{3,4})?',
    re.IGNORECASE,
)
AMERICAN_ODDS_RE = re.compile(r'([+-]\d{3,4})')
SPREAD_RE = re.compile(r'([+-]\d+\.?\d*)\s+[(-]?\d{2,3}')


def _extract_line(text: str) -> tuple[Optional[str], Optional[float]]:
    """Extract bet_side ('over'/'under') and line value from text."""
    m = LINE_RE.search(text)
    if m:
        side = m.group(1).upper()
        side_str = "over" if side in ("OVER", "O") else "under"
        line_txt = m.group(2).replace("½", ".5").replace("?", ".5")
        return side_str, float(line_txt)
    return None, None


def _decimal_to_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int((decimal - 1) * 100)
    return int(-100 / (decimal - 1))


class BetMatcher:
    def __init__(
        self,
        similarity_threshold: int = 75,
        odds_tolerance: float = 0.05,
        line_slippage: float = 1.0,
        juice_slippage: int = 20,
    ):
        """
        similarity_threshold : min fuzzy score (0-100) for text match
        odds_tolerance       : fractional tolerance for decimal odds comparison
        line_slippage        : max points line can move and still be accepted (e.g. 1.0)
        juice_slippage       : max juice change in American odds units (e.g. 20 means -110 -> -130 is rejected)
        """
        self.similarity_threshold = similarity_threshold
        self.odds_tolerance = odds_tolerance
        self.line_slippage = line_slippage
        self.juice_slippage = juice_slippage

    # ── public interface ──────────────────────────────────────────────────────

    def match(self, target: dict, candidate: dict) -> tuple[bool, bool, float]:
        """
        Returns (is_exact, is_similar, similarity_score 0-100).
        """
        # Line-based matching for Over/Under bets
        if target.get("bet_side") in ("over", "under"):
            return self._match_total(target, candidate)

        # Text-based matching for other markets
        return self._match_text(target, candidate)

    def filter_results(self, target: dict, candidates: list[dict]) -> list[dict]:
        """Annotate and filter candidates, sorted by similarity desc."""
        annotated = []
        for c in candidates:
            is_exact, is_similar, score = self.match(target, c)
            if is_exact or is_similar:
                annotated.append({
                    **c,
                    "is_exact": is_exact,
                    "is_similar": is_similar,
                    "similarity_score": score,
                })
        annotated.sort(key=lambda x: x["similarity_score"], reverse=True)
        return annotated

    # ── Over/Under line matching ──────────────────────────────────────────────

    def _match_total(self, target: dict, candidate: dict) -> tuple[bool, bool, float]:
        target_side = target.get("bet_side", "").lower()
        target_line = target.get("line")
        if target_line is None:
            return self._match_text(target, candidate)

        # Extract side+line from candidate text
        cand_text = " ".join([
            str(candidate.get("event", "")),
            str(candidate.get("selection", "")),
            str(candidate.get("market", "")),
        ])
        cand_side, cand_line = _extract_line(cand_text)
        if candidate.get("bet_side") in ("over", "under"):
            cand_side = str(candidate.get("bet_side")).lower()
        if candidate.get("line") is not None:
            try:
                cand_line = float(candidate.get("line"))
            except Exception:
                pass

        # Event text similarity
        event_score = self._score_text(target.get("event", ""), candidate.get("event", ""))
        if event_score < 40:
            return False, False, event_score

        if cand_line is None or cand_side is None:
            # Candidate has no parseable line; fall back to pure text
            return self._match_text(target, candidate)

        if cand_side != target_side:
            return False, False, 0.0

        # Slippage check
        line_ok = self._line_within_slippage(target_side, target_line, cand_line)
        juice_ok = self._juice_ok(target, candidate)

        # A moved line or moved juice outside threshold is treated as non-actionable.
        if not line_ok or not juice_ok:
            return False, False, 0.0

        # Calculate similarity score
        line_diff = abs(cand_line - target_line)
        line_score = max(0, 100 - line_diff * 50)  # lose 50pts per point of slip
        combined = event_score * 0.5 + line_score * 0.5

        is_exact = (cand_line == target_line and event_score >= 90)
        is_similar = combined >= self.similarity_threshold

        return is_exact, is_similar, round(combined, 1)

    def _line_within_slippage(self, side: str, target_line: float, actual_line: float) -> bool:
        """
        OVER : actual_line <= target_line + slippage
        UNDER: actual_line >= target_line - slippage
        """
        if side == "over":
            return actual_line <= target_line + self.line_slippage
        elif side == "under":
            return actual_line >= target_line - self.line_slippage
        return True

    def _juice_ok(self, target: dict, candidate: dict) -> bool:
        """Check that the juice/odds haven't moved too much."""
        t_odds = target.get("odds_american") or target.get("odds")
        c_odds = candidate.get("odds_american") or candidate.get("odds")
        if t_odds is None or c_odds is None:
            return True
        # Convert to American if decimal given
        if isinstance(t_odds, float) and t_odds < 20:
            t_odds = _decimal_to_american(t_odds)
        if isinstance(c_odds, float) and c_odds < 20:
            c_odds = _decimal_to_american(c_odds)
        return abs(int(t_odds) - int(c_odds)) <= self.juice_slippage

    # ── Text-based matching ───────────────────────────────────────────────────

    def _match_text(self, target: dict, candidate: dict) -> tuple[bool, bool, float]:
        event_score = self._score_text(target.get("event", ""), candidate.get("event", ""))
        market_score = self._score_text(target.get("market", ""), candidate.get("market", ""))
        sel_score = self._score_text(target.get("selection", ""), candidate.get("selection", ""))

        if target.get("market") and target.get("selection"):
            overall = event_score * 0.5 + market_score * 0.25 + sel_score * 0.25
        elif target.get("market"):
            overall = event_score * 0.6 + market_score * 0.4
        elif target.get("selection"):
            overall = event_score * 0.6 + sel_score * 0.4
        else:
            overall = float(event_score)

        odds_match = self._odds_match(target.get("odds"), candidate.get("odds"))
        is_similar = overall >= self.similarity_threshold
        is_exact = overall >= 95 and (odds_match or target.get("odds") is None)

        return is_exact, is_similar, round(overall, 1)

    def _score_text(self, a: str, b: str) -> float:
        if not a or not b:
            return 50.0
        a, b = a.lower().strip(), b.lower().strip()
        return float(max(fuzz.token_set_ratio(a, b), fuzz.partial_ratio(a, b)))

    def _odds_match(self, target: Optional[float], candidate: Optional[float]) -> bool:
        if target is None or candidate is None:
            return True
        lower = target * (1 - self.odds_tolerance)
        upper = target * (1 + self.odds_tolerance)
        return lower <= candidate <= upper


def is_hedge(existing_bets: list[dict], new_bet: dict, same_account: bool = True) -> bool:
    """
    Detect if new_bet is a hedge against an existing bet on the same game.
    same_account: True if checking within the same account/player.
    """
    if not same_account:
        return False
    new_event = new_bet.get("event", "").lower()
    new_side = new_bet.get("bet_side", "").lower()

    for existing in existing_bets:
        ex_event = existing.get("event", "").lower()
        ex_side = existing.get("bet_side", "").lower()

        event_score = fuzz.token_set_ratio(new_event, ex_event)
        if event_score < 75:
            continue

        # Opposite sides on same total = hedge
        if new_side and ex_side:
            if (new_side == "over" and ex_side == "under") or \
               (new_side == "under" and ex_side == "over"):
                return True

    return False
