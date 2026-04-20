"""
ibetcoin_reader.py
Scrapes open bets from https://reports.ibetcoin.win/Report/OpenBets.aspx
Returns a list of structured bet dicts.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)

OPENBETS_URL = "https://reports.ibetcoin.win/Report/OpenBets.aspx"
AMERICAN_ODDS_RE = re.compile(r'([+-]\d{3,4})(?=\D|$)')
LINE_RE = re.compile(
    r'(OVER|UNDER|O|U)\s*(\d+(?:\.\d+|[½?])?)(?:\s*[+-]\d{3,4})?',
    re.IGNORECASE,
)  # handles "u173", "u221½-105", and "OVER 216"
SPREAD_RE = re.compile(r'([+-]\d+\.?\d*)\s*$')
TICKET_RE = re.compile(r'Ticket\s*#?(\d+)', re.IGNORECASE)
RISK_WIN_RE = re.compile(r'(\d[\d,]*)\s*/\s*(\d[\d,]*)')


@dataclass
class OpenBet:
    ticket_id: str = ""
    placed: str = ""
    player: str = ""
    game_date: str = ""
    sport: str = ""
    event: str = ""
    market: str = ""
    selection: str = ""
    line: Optional[float] = None        # numeric total line e.g. 215.5
    bet_side: str = ""                  # "over" | "under" | "home" | "away" | ""
    odds_american: Optional[int] = None # e.g. -110
    odds_decimal: Optional[float] = None
    risk: float = 0.0
    win: float = 0.0
    raw_description: str = ""

    def to_dict(self) -> dict:
        return {
            "ticket_id":    self.ticket_id,
            "placed":       self.placed,
            "player":       self.player,
            "game_date":    self.game_date,
            "sport":        self.sport,
            "event":        self.event,
            "market":       self.market,
            "selection":    self.selection,
            "line":         self.line,
            "bet_side":     self.bet_side,
            "odds_american":self.odds_american,
            "odds":         self.odds_decimal,
            "risk":         self.risk,
            "win":          self.win,
            "raw":          self.raw_description,
        }


def _american_to_decimal(american: int) -> float:
    if american > 0:
        return round(american / 100 + 1.0, 4)
    return round(100 / abs(american) + 1.0, 4)


def _parse_risk_win(text: str) -> tuple[float, float]:
    m = RISK_WIN_RE.search(text.replace(",", ""))
    if m:
        return float(m.group(1)), float(m.group(2))
    return 0.0, 0.0


def parse_bet_row(row_text: str) -> Optional[OpenBet]:
    """Parse a single OpenBets table row into an OpenBet object."""
    if not row_text or "Ticket" not in row_text:
        return None

    bet = OpenBet()
    bet.raw_description = row_text

    lines = [l.strip() for l in row_text.replace("\t", "\n").split("\n") if l.strip()]

    # Ticket ID
    for part in lines:
        m = TICKET_RE.search(part)
        if m:
            bet.ticket_id = m.group(1)
            break

    # Risk / Win (last numbers)
    risk_win_line = lines[-1] if lines else ""
    bet.risk, bet.win = _parse_risk_win(risk_win_line)

    # Find columns: Placed | Player | GameDate | Sport | Description
    # After ticket line, data follows in order
    ticket_idx = next((i for i, l in enumerate(lines) if TICKET_RE.search(l)), -1)
    if ticket_idx >= 0:
        remaining = lines[ticket_idx + 1:]
        # placed date often is right after ticket line
        if remaining:
            # Check for date pattern like "April 17 7:06:16PM"
            date_pat = re.compile(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d+', re.I)
            for i, part in enumerate(remaining):
                if date_pat.search(part) and not bet.placed:
                    bet.placed = part
                elif re.match(r'^(Internet|Phone|Mobile)', part, re.I) and not bet.player:
                    bet.player = part
                elif date_pat.search(part) and bet.placed and not bet.game_date:
                    bet.game_date = part
                elif re.match(r'^(NFL|NBA|MLB|NHL|NCAA|PROP|SOCCER|TENNIS|BASKETBALL|FOOTBALL|BASEBALL|HOCKEY|MMA|GOLF|BOXING)', part, re.I) and not bet.sport:
                    bet.sport = part.split()[0]
                    # rest of line may have description start
                    rest = part[len(bet.sport):].strip()
                    if rest:
                        bet.event = rest

    # Description parsing — look for lines with [ ] bracket selections
    bracket_lines = [l for l in lines if re.search(r'\[\d+\]', l)]
    if bracket_lines:
        desc_line = bracket_lines[0]
        # Normalize fractional markers before ASCII cleanup.
        desc_line = (
            desc_line
            .replace("½", ".5")
            .replace("Â½", ".5")
            .replace("?", ".5")
        )
        # Strip remaining non-ASCII / corrupt unicode chars that can appear between odds parts
        desc_clean_raw = re.sub(r'[^\x00-\x7F]+', ' ', desc_line).strip()
        bet.raw_description = desc_clean_raw

        # Extract American odds — look for -115, +150 etc
        # Must search AFTER stripping non-ASCII so patterns like u173???-115 → u173 -115
        odds_m = AMERICAN_ODDS_RE.search(desc_clean_raw)
        if odds_m:
            bet.odds_american = int(odds_m.group(1))
            bet.odds_decimal = _american_to_decimal(bet.odds_american)

        # Strip the bracket ID and odds from description for clean text
        clean = re.sub(r'\[\d+\]', '', desc_clean_raw)
        # Remove American odds even when glued to text, e.g. "u221½-105 (....)"
        clean = re.sub(r'[+-]\d{3,4}(?=\D|$)', '', clean).strip()
        bet.selection = clean

        # Detect Over/Under — handles both "OVER 216" and "u173" (no space)
        line_m = LINE_RE.search(clean)
        if line_m:
            side = line_m.group(1).upper()
            bet.bet_side = "over" if side in ("OVER", "O") else "under"
            try:
                line_txt = line_m.group(2).replace("½", ".5").replace("?", ".5")
                bet.line = float(line_txt)
            except ValueError:
                pass
            bet.market = f"Total {side.title()} {bet.line}"
        else:
            spread_m = SPREAD_RE.search(clean)
            if spread_m:
                bet.market = "Spread"
            else:
                bet.market = "Moneyline"

    # Event: extract team names from the bracket description (handles "vrs", "vs", "@")
    if not bet.event and bet.selection:
        # Look for team match pattern inside parentheses: (TEAM1 vrs/vs/@ TEAM2) (League)
        paren_m = re.search(r'\(([^)]+(?:vrs?|@|at)[^)]+)\)', bet.selection, re.I)
        if paren_m:
            event_raw = paren_m.group(1).strip()
            # Normalize "vrs" -> "vs"
            event_raw = re.sub(r'\bvrs?\b', 'vs', event_raw, flags=re.I)
            # Also grab league if present: (Spain Liga ACB)
            league_m = re.search(r'\(([^)]+(?:Liga|League|ACB|NBA|NFL|MLB|NHL)[^)]*)\)', bet.selection, re.I)
            league = f" ({league_m.group(1)})" if league_m else ""
            bet.event = event_raw + league
    
    if not bet.event:
        for l in lines:
            if re.search(r'\s+(vrs?|vs\.?|@|at)\s+', l, re.I) or re.search(r'[A-Z]{2,}\s+[A-Z]{2,}', l):
                bet.event = re.sub(r'\bvrs?\b', 'vs', l, flags=re.I)
                break

    # Fallback: use raw description as event
    if not bet.event and bet.raw_description:
        bet.event = re.sub(r'\[\d+\].*', '', bet.raw_description).strip()

    return bet if bet.ticket_id else None


class IbetcoinReader:
    """Logs into ibetcoin.win and returns the current list of open bets."""

    def __init__(self, username: str, password: str, headless: bool = True):
        self.username = username
        self.password = password
        self.headless = headless
        self._known_tickets: set[str] = set()

    async def fetch_open_bets(self) -> list[dict]:
        """Returns list of bet dicts. Only returns NEW bets not seen before."""
        bets = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
                page = await ctx.new_page()

                # Login
                await page.goto(OPENBETS_URL, timeout=30000)
                await asyncio.sleep(3)

                title = await page.title()
                if "Login" in title or "login" in title:
                    await page.fill("#Account", self.username, timeout=10000)
                    await page.fill("#Password", self.password)
                    await page.click("input[type=submit]")
                    await asyncio.sleep(5)
                    # Navigate back to OpenBets
                    await page.goto(OPENBETS_URL, timeout=20000)
                    await asyncio.sleep(4)

                bets = await self._scrape_bets(page)
                await browser.close()

        except Exception as e:
            logger.error(f"[IbetcoinReader] Error: {e}")

        return bets

    async def fetch_new_bets(self) -> list[dict]:
        """Same as fetch_open_bets but filters out already-seen tickets."""
        all_bets = await self.fetch_open_bets()
        new_bets = []
        for b in all_bets:
            tid = b.get("ticket_id", "")
            if tid and tid not in self._known_tickets:
                self._known_tickets.add(tid)
                new_bets.append(b)
        return new_bets

    async def _scrape_bets(self, page: Page) -> list[dict]:
        bets = []
        rows = await page.query_selector_all("table tr")
        for row in rows:
            try:
                text = (await row.inner_text()).strip()
                bet = parse_bet_row(text)
                if bet:
                    bets.append(bet.to_dict())
            except Exception:
                pass
        logger.info(f"[IbetcoinReader] Scraped {len(bets)} open bets")
        return bets
