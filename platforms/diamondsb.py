"""
DiamondSB platform scraper.

Flow:
1) Login at /pla/#/msg
2) Dismiss post-login notice ("Got It!")
3) Go to /pla/#/bet?bm=straight (or parlay)
4) Pick sport + league, click CONTINUE
5) Scrape odds board text
"""

import asyncio
import logging
import re
from .base import BasePlatformScraper

logger = logging.getLogger(__name__)


class DiamondSBScraper(BasePlatformScraper):
    PLATFORM_NAME = "DiamondSB"

    def _origin(self):
        from urllib.parse import urlparse
        p = urlparse(self.url)
        return f"{p.scheme}://{p.netloc}"

    def _is_login_wall(self, url: str, title: str, body_text: str) -> bool:
        u = (url or "").lower()
        t = (title or "").lower()
        b = (body_text or "").lower()
        return (
            "expired=true" in u
            or "diamondsb login" in t
            or ("welcome!" in b and "sign in" in b and "username" in b)
        )

    async def _dismiss_overlays(self):
        for sel in [
            'button:has-text("Accept")',
            'button:has-text("Got It")',
            'button:has-text("Got It!")',
            '.close',
            '[aria-label="Close"]',
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=600):
                    await loc.click(timeout=1200)
                    await asyncio.sleep(0.2)
            except Exception:
                pass

    async def _verify_login(self) -> bool:
        try:
            title = await self.page.title()
            body = await self.page.locator("body").inner_text()
            if self._is_login_wall(self.page.url, title, body):
                return False

            body_l = body.lower()
            if "got it!" in body_l and "players:" in body_l:
                return True

            for sel in [
                'text=Sports',
                'text=Pending Wagers',
                'text=Messages',
                'text=Transactions',
                'a:has-text("Log Out")',
            ]:
                loc = self.page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1200):
                    return True

            sign_in = self.page.locator('.signin-form, button:has-text("Sign In")').first
            if await sign_in.count() and await sign_in.is_visible(timeout=800):
                return False
            return True
        except Exception:
            return False

    async def login(self) -> bool:
        if not self.username or not self.password:
            logger.warning(f"[{self.PLATFORM_NAME}] No credentials configured")
            return False

        logger.info(f"[{self.PLATFORM_NAME}] Logging in...")
        try:
            await self.safe_goto(self._origin() + "/pla/#/msg")
            await asyncio.sleep(2.0)
            await self._dismiss_overlays()

            user_selectors = [
                '.signin-form input[type="text"]',
                'input[name="username"]',
                'input[type="text"]',
            ]
            pass_selectors = [
                '#password-field',
                '.signin-form input[type="password"]',
                'input[type="password"]',
            ]

            user_filled = False
            for sel in user_selectors:
                if await self.safe_fill(sel, self.username, timeout=5000):
                    user_filled = True
                    break

            pass_filled = False
            for sel in pass_selectors:
                if await self.safe_fill(sel, self.password, timeout=5000):
                    pass_filled = True
                    break

            if user_filled and pass_filled:
                submitted = await self.safe_click(
                    '.signin-form button[type="submit"], .signin-form .btn-primary, button[type="submit"]',
                    timeout=8000,
                )
                if not submitted:
                    try:
                        await self.page.keyboard.press("Enter")
                    except Exception:
                        pass
                await asyncio.sleep(5)
                await self._dismiss_overlays()

            # If fields weren't found we may already be logged in on notice/shell.
            self.is_logged_in = await self._verify_login()

            if self.is_logged_in:
                logger.info(f"[{self.PLATFORM_NAME}] Login successful")
            else:
                logger.error(
                    f"[{self.PLATFORM_NAME}] Login failed (URL: {self.page.url}, title: {await self.page.title()})"
                )
            return self.is_logged_in

        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Login error: {e}")
            return False

    def _bet_mode(self, bet: dict) -> str:
        mode = (bet.get("bet_mode") or "").lower().strip()
        if mode in ("straight", "parlay", "teaser", "if-bet", "ifbet", "reverse"):
            return "if-bet" if mode == "ifbet" else mode
        market = (bet.get("market") or "").lower()
        if "parlay" in market:
            return "parlay"
        return "straight"

    def _sport_labels(self, bet: dict) -> list[str]:
        sport = (bet.get("sport") or "").lower()
        event = (bet.get("event") or "").lower()
        if "basket" in sport or any(x in event for x in ["nba", "wnba", "bbl", "euroleague"]):
            return ["Basketball"]
        if sport in ("football", "nfl", "ncaa"):
            return ["Football"]
        if sport in ("baseball", "mlb"):
            return ["Baseball"]
        if sport in ("hockey", "nhl"):
            return ["Hockey"]
        if "soccer" in sport:
            return ["Major Soccer Categories", "Minor Soccer Categories"]
        return [sport.title()] if sport else ["Basketball", "Football", "Baseball", "Hockey"]

    def _league_candidates(self, bet: dict) -> list[str]:
        sport = (bet.get("sport") or "").lower()
        event = (bet.get("event") or "").lower()
        out: list[str] = []

        if "basket" in sport or "nba" in event:
            out += ["NBA Full Game", "NBA 1st Half", "NBA 1st Quarter", "NBA"]
        if "wnba" in event:
            out += ["WNBA Futures", "WNBA"]
        if any(x in event for x in ["germany", "bbl"]):
            out += ["Germany BBL Full Game"]
        if "football" in sport or "nfl" in event:
            out += ["NFL Full Game", "NCAAF Full Game", "Football"]
        if "baseball" in sport or "mlb" in event:
            out += ["MLB Full Game", "Baseball"]
        if "hockey" in sport or "nhl" in event:
            out += ["NHL Full Game", "Hockey"]
        if "soccer" in sport:
            out += ["Major Soccer Categories", "Minor Soccer Categories"]

        if not out:
            out = ["NBA Full Game", "Football", "Baseball", "Hockey"]

        seen = set()
        final = []
        for x in out:
            if x not in seen:
                seen.add(x)
                final.append(x)
        return final

    async def _click_text(self, text: str, timeout_ms: int = 2500) -> bool:
        selectors = [
            f'text="{text}"',
            f'button:has-text("{text}")',
            f'a:has-text("{text}")',
            f'li:has-text("{text}")',
            f'label:has-text("{text}")',
        ]
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=900):
                    await loc.click(timeout=timeout_ms)
                    return True
            except Exception:
                pass
        return False

    async def _prepare_board(self, bet: dict) -> bool:
        mode = self._bet_mode(bet)
        await self.safe_goto(self._origin() + f"/pla/#/bet?bm={mode}")
        await asyncio.sleep(2.0)
        await self._dismiss_overlays()

        sport_clicked = False
        for label in self._sport_labels(bet):
            if await self._click_text(label):
                sport_clicked = True
                await asyncio.sleep(1.0)
                break

        league_clicked = False
        for label in self._league_candidates(bet):
            if await self._click_text(label):
                league_clicked = True
                await asyncio.sleep(1.0)
                break

        _ = await self._click_text("CONTINUE") or await self._click_text("Continue")
        await asyncio.sleep(2.0)

        body = ""
        try:
            body = await self.page.locator("body").inner_text()
        except Exception:
            pass

        has_odds = bool(re.search(r"\bTotal\s+(?:Over|Under)\b|\bSpread\b|\bMoneyLine\b", body, re.I))
        logger.info(
            f"[{self.PLATFORM_NAME}] board prep: sport_clicked={sport_clicked} "
            f"league_clicked={league_clicked} has_odds={has_odds}"
        )
        return has_odds

    def _parse_board_candidates(self, body_text: str, target_event: str, target_market: str) -> list[dict]:
        lines = [x.strip() for x in (body_text or "").splitlines() if x.strip()]
        target_words = [
            w for w in re.sub(r"[^a-z0-9 ]+", " ", (target_event or "").lower()).split() if len(w) > 2
        ]

        def score_event(s: str) -> int:
            low = (s or "").lower()
            if not target_words:
                return 1
            return sum(1 for w in target_words if w in low)

        candidates: list[dict] = []
        team_a = ""
        team_b = ""
        current_team = ""
        current_event = target_event or ""
        pending = ""

        skip_words = {
            "sports", "live", "props", "casino", "horses", "straight", "parlay", "teaser",
            "if-bet", "reverse", "spread", "moneyline", "total over", "total under", "vs",
            "refresh", "upcoming games",
        }

        team_line_re = re.compile(r"^[A-Za-z][A-Za-z .'-]{2,}(?:\s+NBC)?$")
        rotation_re = re.compile(r"^\d{3}(?:\s+TT)?$")
        spread_line_re = re.compile(r"^([+-]\d+(?:\.\d+|½)?)\s+([+-]\d{3,4})$")
        total_line_re = re.compile(r"^(\d+(?:\.\d+|½)?)\s+([+-]\d{3,4})$")
        ml_line_re = re.compile(r"^[+-]\d{3,4}$")

        for i, line in enumerate(lines):
            low = line.lower()
            nxt = lines[i + 1] if i + 1 < len(lines) else ""

            if team_line_re.match(line) and rotation_re.match(nxt):
                current_team = re.sub(r"\s+NBC$", "", line).strip()
                if not team_a:
                    team_a = current_team
                elif not team_b and current_team != team_a:
                    team_b = current_team
                    current_event = f"{team_a} @ {team_b}"
                continue

            if low == "vs":
                team_a = ""
                team_b = ""
                current_team = ""
                pending = ""
                continue

            if low in skip_words:
                if low in ("spread", "moneyline", "total over", "total under"):
                    pending = low
                continue

            if pending == "spread":
                m = spread_line_re.match(line)
                if m:
                    candidates.append(
                        {
                            "event": current_event,
                            "market": "Spread",
                            "selection": f"{current_team} {m.group(1).replace('½', '.5')}".strip(),
                            "odds_american": int(m.group(2)),
                        }
                    )
                    pending = ""
                    continue

            if pending == "moneyline":
                if ml_line_re.match(line):
                    candidates.append(
                        {
                            "event": current_event,
                            "market": "Moneyline",
                            "selection": current_team,
                            "odds_american": int(line),
                        }
                    )
                    pending = ""
                    continue

            if pending in ("total over", "total under"):
                m = total_line_re.match(line)
                if m:
                    side = "Over" if pending.endswith("over") else "Under"
                    candidates.append(
                        {
                            "event": current_event,
                            "market": "Total",
                            "selection": f"{side} {m.group(1).replace('½', '.5')}",
                            "odds_american": int(m.group(2)),
                        }
                    )
                    pending = ""
                    continue

        scored = []
        for c in candidates:
            c2 = dict(c)
            c2["_score"] = score_event(c2.get("event", ""))
            scored.append(c2)

        if target_words:
            max_score = max([x["_score"] for x in scored], default=0)
            scored = [x for x in scored if x["_score"] >= max(1, max_score - 1)]

        need = (target_market or "").lower()
        if "total" in need:
            scored = [x for x in scored if x.get("market") == "Total"]
        elif "spread" in need:
            scored = [x for x in scored if x.get("market") == "Spread"]
        elif "moneyline" in need or need == "ml":
            scored = [x for x in scored if x.get("market") == "Moneyline"]

        uniq = []
        seen = set()
        for c in scored:
            key = (c.get("event", ""), c.get("market", ""), c.get("selection", ""), c.get("odds_american"))
            if key in seen:
                continue
            seen.add(key)
            c.pop("_score", None)
            uniq.append(c)
        return uniq

    async def search_bets(self, bet: dict) -> list[dict]:
        if not self.is_logged_in:
            logger.warning(f"[{self.PLATFORM_NAME}] Not logged in")
            return []

        try:
            title = await self.page.title()
            body = await self.page.locator("body").inner_text()
            if self._is_login_wall(self.page.url, title, body):
                logger.warning(f"[{self.PLATFORM_NAME}] Session expired, relogging...")
                self.is_logged_in = False
                if not await self.login_with_retry(max_attempts=2):
                    return []
        except Exception:
            pass

        results: list[dict] = []
        try:
            ready = await self._prepare_board(bet)
            if not ready:
                return []

            body_text = await self.page.locator("body").inner_text()
            scraped = self._parse_board_candidates(
                body_text=body_text,
                target_event=bet.get("event", ""),
                target_market=(bet.get("market") or ""),
            )

            for c in scraped:
                am = c.get("odds_american")
                dec = self.normalize_odds(str(am)) if am is not None else None
                if dec is None:
                    continue
                results.append(
                    {
                        "event": (c.get("event") or "")[:120],
                        "sport": bet.get("sport", ""),
                        "market": c.get("market", bet.get("market", "")),
                        "selection": c.get("selection", ""),
                        "odds_american": am,
                        "odds": dec,
                        "url": self.page.url,
                    }
                )

            logger.info(f"[{self.PLATFORM_NAME}] Scraped {len(results)} odds candidates")

        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Search error: {e}")

        return results
