"""
Sports411 platform scraper.

Flow:
1) Login at /en/sports/
2) Select sport/league from left sidebar when available
3) Read center odds board (Away/Home with Spread/Total/Money Line)
"""

import asyncio
import logging
import re
from .base import BasePlatformScraper

logger = logging.getLogger(__name__)

# Bright Data proxy credentials for Sports411
_S411_PROXY = {
    "server": "http://brd.superproxy.io:33335",
    "username": "brd-customer-hl_5c133191-zone-sports411",
    "password": "uuszpgqk1e3m",
}


class Sports411Scraper(BasePlatformScraper):
    PLATFORM_NAME = "Sports411"

    def __init__(self, config: dict, headless: bool = True):
        if "proxy" not in config:
            config = {**config, "proxy": _S411_PROXY}
        super().__init__(config, headless)

    async def _dismiss_overlays(self):
        for sel in [
            "button.outdated-button",
            'button:has-text("Continue Anyway")',
            'button:has-text("Accept")',
            '[aria-label="Close"]',
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=600):
                    await loc.click(timeout=1200)
                    await asyncio.sleep(0.2)
            except Exception:
                pass

    async def _click_text(self, text: str, timeout_ms: int = 2500) -> bool:
        selectors = [
            f'text="{text}"',
            f'button:has-text("{text}")',
            f'a:has-text("{text}")',
            f'li:has-text("{text}")',
            f'label:has-text("{text}")',
            f'span:has-text("{text}")',
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

    async def _verify_login(self) -> bool:
        for sel in [
            'text=Inbox',
            'text=Open Bets',
            'text=History',
            'button:has-text("Logout")',
            'a:has-text("Logout")',
        ]:
            loc = self.page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=1200):
                    return True
            except Exception:
                pass

        # Fallback: if login form elements are gone, assume logged in
        login_field = self.page.locator('input[name="account"], button.login-enter').first
        try:
            if await login_field.count() and await login_field.is_visible(timeout=800):
                return False
        except Exception:
            pass
        return True

    async def login(self) -> bool:
        if not self.username or not self.password:
            logger.warning(f"[{self.PLATFORM_NAME}] No credentials configured")
            return False

        logger.info(f"[{self.PLATFORM_NAME}] Logging in to Sports411 (via proxy)...")
        try:
            await self.safe_goto("https://be.sports411.ag/en/sports/")
            try:
                await self.page.wait_for_selector("button.login-enter", timeout=15000)
            except Exception:
                await asyncio.sleep(10)
            await self._dismiss_overlays()
            await asyncio.sleep(0.5)

            clicked = await self.safe_click("button.login-enter", timeout=8000)
            if not clicked:
                for sel in ['button:has-text("Log In")', 'button:has-text("Login")']:
                    if await self.safe_click(sel, timeout=3000):
                        clicked = True
                        break
            if not clicked:
                logger.error(f"[{self.PLATFORM_NAME}] Login button not found")
                return False

            await asyncio.sleep(2.0)

            user_filled = False
            for sel in [
                'input[name="account"]',
                'input[formcontrolname="username"]',
                'input[type="text"]',
            ]:
                if await self.safe_fill(sel, self.username, timeout=5000):
                    user_filled = True
                    break
            if not user_filled:
                logger.error(f"[{self.PLATFORM_NAME}] Could not fill username")
                return False

            pass_filled = False
            for sel in ['input[name="password"]', 'input[type="password"]']:
                if await self.safe_fill(sel, self.password, timeout=5000):
                    pass_filled = True
                    break
            if not pass_filled:
                logger.error(f"[{self.PLATFORM_NAME}] Could not fill password")
                return False

            submitted = False
            for sel in ['button[type="submit"]', 'button:has-text("Log In")', 'button:has-text("Sign In")']:
                if await self.safe_click(sel, timeout=5000):
                    submitted = True
                    break
            if not submitted:
                try:
                    await self.page.keyboard.press("Enter")
                except Exception:
                    pass

            await asyncio.sleep(6)
            await self._dismiss_overlays()

            self.is_logged_in = await self._verify_login()
            if self.is_logged_in:
                logger.info(f"[{self.PLATFORM_NAME}] Login successful")
            else:
                logger.error(f"[{self.PLATFORM_NAME}] Login failed (URL: {self.page.url}, title: {await self.page.title()})")
            return self.is_logged_in

        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Login error: {e}")
            return False

    def _sport_label(self, sport: str, event: str) -> str:
        s = (sport or "").lower()
        e = (event or "").lower()
        if "basket" in s or any(x in e for x in ["nba", "wnba", "bbl"]):
            return "BASKETBALL"
        if s in ("football", "nfl", "ncaa"):
            return "FOOTBALL"
        if s in ("baseball", "mlb"):
            return "BASEBALL"
        if s in ("hockey", "nhl"):
            return "HOCKEY"
        if "soccer" in s:
            return "SOCCER"
        return s.upper() if s else "BASKETBALL"

    def _is_prop_bet(self, bet: dict) -> bool:
        m = (bet.get("market") or "").lower()
        s = (bet.get("selection") or "").lower()
        e = (bet.get("event") or "").lower()
        hints = ("get ", "pts", "points", "reb", "assist", "threes", "first", "race to", "player")
        return "prop" in m or any(h in s for h in hints) or any(h in e for h in hints)

    def _league_labels(self, bet: dict) -> list[str]:
        sport = (bet.get("sport") or "").lower()
        event = (bet.get("event") or "").lower()
        labels = []
        if "basket" in sport or "nba" in event:
            labels += ["NBA", "GAME LINES", "NBA PLAYOFFS"]
        if "baseball" in sport or "mlb" in event:
            labels += ["MLB", "GAME LINES"]
        if "football" in sport or "nfl" in event:
            labels += ["NFL", "GAME LINES"]
        if "hockey" in sport or "nhl" in event:
            labels += ["NHL", "GAME LINES"]
        if "soccer" in sport:
            labels += ["SOCCER", "GAME LINES"]
        if not labels:
            labels = ["GAME LINES"]
        return list(dict.fromkeys(labels))

    async def _prepare_board(self, bet: dict):
        await self.safe_goto("https://be.sports411.ag/en/sports/")
        await asyncio.sleep(2.0)
        await self._dismiss_overlays()

        sport_ok = await self._click_text(self._sport_label(bet.get("sport", ""), bet.get("event", "")))
        if sport_ok:
            await asyncio.sleep(1.0)

        clicked_labels = []
        for label in self._league_labels(bet):
            if await self._click_text(label):
                clicked_labels.append(label)
                await asyncio.sleep(0.8)

        # If league submenu loaded but board is still empty, try GAME LINES again.
        try:
            body = await self.page.locator("body").inner_text()
            has_board = bool(re.search(r"Away\s+Home\s+Spread\s+Total\s+Money\s+Line", body, re.I))
            if not has_board and "GAME LINES" not in clicked_labels:
                if await self._click_text("GAME LINES"):
                    await asyncio.sleep(1.0)
        except Exception:
            pass

        # Sports411 can intermittently open league route without loading rows.
        # Force the known-good click path once more when board is empty.
        try:
            body = await self.page.locator("body").inner_text()
            has_rows = bool(re.search(r"GAME LINES\s*-\s*APR", body, re.I))
            if not has_rows:
                for label in [self._sport_label(bet.get("sport", ""), bet.get("event", "")), "NBA", "GAME LINES"]:
                    await self._click_text(label)
                    await asyncio.sleep(0.9)
                await self._dismiss_overlays()
        except Exception:
            pass

        await asyncio.sleep(1.5)

    def _parse_board_candidates(self, body_text: str, bet: dict) -> list[dict]:
        lines = [x.strip() for x in (body_text or "").splitlines() if x.strip()]
        target_event = (bet.get("event") or "")
        target_words = [
            w for w in re.sub(r"[^a-z0-9 ]+", " ", target_event.lower()).split() if len(w) > 2
        ]

        def event_score(s: str) -> int:
            if not target_words:
                return 1
            low = s.lower()
            return sum(1 for w in target_words if w in low)

        def is_team_name(s: str) -> bool:
            if len(s) < 3:
                return False
            low = s.lower()
            if low in {
                "live", "away", "home", "spread", "total", "money line", "more",
                "betslip", "my open bets", "straight", "empty", "select any pick to start betting",
            }:
                return False
            if re.search(r"\bapr\b|\butc\b|\bprops\b|\bview\b", low):
                return False
            if re.search(r"^[+\-]?\d", s):
                return False
            return bool(re.search(r"[A-Za-z]", s))

        def compact(s: str) -> str:
            return re.sub(r"\s+", "", s).replace("½", ".5")

        candidates = []
        i = 0
        while i < len(lines) - 2:
            # Detect a matchup by two adjacent team-like lines.
            away = lines[i]
            home = lines[i + 1]
            if not (is_team_name(away) and is_team_name(home)):
                i += 1
                continue

            event = f"{away} @ {home}"
            j = i + 2
            block = []
            while j < len(lines):
                lj = lines[j]
                lowj = lj.lower()
                if "props" in lowj or lowj == "view":
                    block.append(lj)
                    j += 1
                    break
                if j + 1 < len(lines) and is_team_name(lines[j]) and is_team_name(lines[j + 1]):
                    break
                if re.search(r" - APR |GAME LINES|LIVE$", lj):
                    if block:
                        break
                block.append(lj)
                j += 1

            # Parse known order tokens from block.
            toks = [compact(x) for x in block if x]
            # Spread pairs
            for k in range(len(toks) - 1):
                if re.fullmatch(r"[+-]\d+(?:\.\d+)?", toks[k]) and re.fullmatch(r"[+-]\d{3,4}", toks[k + 1]):
                    # choose side by sign for readability only; still keep both
                    sel = f"{away} {toks[k]}" if toks[k].startswith("+") else f"{home} {toks[k]}"
                    candidates.append(
                        {
                            "event": event,
                            "market": "Spread",
                            "selection": sel,
                            "odds_american": int(toks[k + 1]),
                        }
                    )
            # Totals pairs
            for k in range(len(toks) - 1):
                if re.fullmatch(r"[ou]\d+(?:\.\d+)?", toks[k], re.I) and re.fullmatch(r"[+-]\d{3,4}", toks[k + 1]):
                    side = "Over" if toks[k][0].lower() == "o" else "Under"
                    candidates.append(
                        {
                            "event": event,
                            "market": "Total",
                            "selection": f"{side} {toks[k][1:]}",
                            "odds_american": int(toks[k + 1]),
                        }
                    )
            # Moneyline: last pair of signed 3-4 digit odds in block typically away/home ML
            ml_vals = [int(t) for t in toks if re.fullmatch(r"[+-]\d{3,4}", t)]
            if len(ml_vals) >= 2:
                candidates.append({"event": event, "market": "Moneyline", "selection": away, "odds_american": ml_vals[-2]})
                candidates.append({"event": event, "market": "Moneyline", "selection": home, "odds_american": ml_vals[-1]})

            i = j

        # Score/filter target event and market
        scored = []
        for c in candidates:
            c2 = dict(c)
            c2["_score"] = event_score(c2.get("event", ""))
            scored.append(c2)

        if target_words:
            max_score = max([x["_score"] for x in scored], default=0)
            scored = [x for x in scored if x["_score"] >= max(1, max_score - 1)]

        need = (bet.get("market") or "").lower()
        if "total" in need:
            scored = [x for x in scored if x.get("market") == "Total"]
        elif "spread" in need:
            scored = [x for x in scored if x.get("market") == "Spread"]
        elif "money" in need:
            scored = [x for x in scored if x.get("market") == "Moneyline"]

        out = []
        seen = set()
        for x in scored:
            key = (x.get("event", ""), x.get("market", ""), x.get("selection", ""), x.get("odds_american"))
            if key in seen:
                continue
            seen.add(key)
            x.pop("_score", None)
            out.append(x)
        return out

    async def _search_props(self, bet: dict) -> list[dict]:
        payload = {"event": bet.get("event", ""), "selection": bet.get("selection", "")}
        raw = await self.page.evaluate(
            """
            async (input) => {
              const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const tokens = norm(`${input.event} ${input.selection}`).split(" ").filter(w => w.length > 2);
              const out = [];

              const propEls = Array.from(document.querySelectorAll("a,button,span,div"))
                .filter(el => /\+\d+\s*props|\\bprops\\b|\\bmore\\b/i.test((el.textContent || "").trim()))
                .slice(0, 8);

              for (const el of propEls) {
                try {
                  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                  await new Promise(r => setTimeout(r, 800));
                } catch {}
                const lines = (document.body?.innerText || "").split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);
                for (const line of lines) {
                  const low = norm(line);
                  const score = tokens.length ? tokens.filter(t => low.includes(t)).length : 0;
                  if (score < Math.max(1, Math.floor(tokens.length * 0.35))) continue;
                  const am = line.match(/([+-]\\d{3,4})/);
                  if (!am) continue;
                  out.push({ event: input.event || "", market: "Prop", selection: line.slice(0, 160), odds_american: Number.parseInt(am[1], 10) });
                }
                document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
                await new Promise(r => setTimeout(r, 100));
              }

              const dedup = new Set();
              const uniq = [];
              for (const x of out) {
                const k = `${x.selection}|${x.odds_american}`;
                if (dedup.has(k)) continue;
                dedup.add(k);
                uniq.push(x);
              }
              return uniq;
            }
            """,
            payload,
        )
        rows = []
        for c in raw or []:
            am = c.get("odds_american")
            dec = self.normalize_odds(str(am)) if am is not None else None
            if dec is None:
                continue
            rows.append(
                {
                    "event": (c.get("event") or "")[:120],
                    "sport": bet.get("sport", ""),
                    "market": "Prop",
                    "selection": c.get("selection", ""),
                    "odds_american": am,
                    "odds": dec,
                    "url": self.page.url,
                }
            )
        return rows

    async def search_bets(self, bet: dict) -> list[dict]:
        if not self.is_logged_in:
            logger.warning(f"[{self.PLATFORM_NAME}] Not logged in")
            return []

        results: list[dict] = []
        try:
            await self._prepare_board(bet)
            body = await self.page.locator("body").inner_text()
            scraped = self._parse_board_candidates(body, bet)

            # Final fallback: parse directly from sportsbook landing board.
            if not scraped:
                await self.safe_goto("https://be.sports411.ag/en/sports/")
                await asyncio.sleep(2.0)
                await self._dismiss_overlays()
                body = await self.page.locator("body").inner_text()
                scraped = self._parse_board_candidates(body, bet)
            if self._is_prop_bet(bet):
                scraped.extend(await self._search_props(bet))

            for c in scraped:
                am = c.get("odds_american")
                dec = self.normalize_odds(str(am)) if am is not None else None
                if dec is None:
                    continue
                results.append(
                    {
                        "event": (c.get("event") or "")[:120],
                        "sport": bet.get("sport", ""),
                        "market": c.get("market", ""),
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
