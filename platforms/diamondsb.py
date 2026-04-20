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
import time
from .base import BasePlatformScraper

logger = logging.getLogger(__name__)


class DiamondSBScraper(BasePlatformScraper):
    PLATFORM_NAME = "DiamondSB"

    def _origin(self):
        from urllib.parse import urlparse
        p = urlparse(self.url)
        return f"{p.scheme}://{p.netloc}"

    def _has_odds_tokens_from_text(self, text: str) -> bool:
        t = text or ""
        if re.search(r"\b(?:Spread|MoneyLine|Total Over|Total Under)\b", t, re.I):
            return True
        return bool(re.search(r"[+-]\d{3,4}\b", t))

    async def _has_odds_tokens(self) -> bool:
        try:
            return await self.page.evaluate(
                "() => /(?:\\bSpread\\b|\\bMoneyLine\\b|\\bTotal\\s+(?:Over|Under)\\b|[+-]\\d{3,4}\\b)/i.test(document.body?.innerText || '')"
            )
        except Exception:
            return False

    async def _wait_for_odds_board(self, timeout_ms: int = 7000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            try:
                if await self._has_odds_tokens():
                    return True
                # Most successful navigations land on this route before lines paint.
                if "p=lines" in (self.page.url or "").lower():
                    await asyncio.sleep(0.15)
                    if await self._has_odds_tokens():
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

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
                if await loc.count() and await loc.is_visible(timeout=250):
                    try:
                        await loc.click(timeout=600)
                    except Exception:
                        await loc.click(timeout=600, force=True)
                    await asyncio.sleep(0.05)
            except Exception:
                pass

    async def _verify_login(self) -> bool:
        try:
            url = self.page.url or ""
            if "expired=true" in url.lower():
                return False

            title = await self.page.title()
            body = await self.page.locator("body").inner_text(timeout=2500)
            if self._is_login_wall(self.page.url, title, body):
                return False

            body_l = body.lower()
            if ("players:" in body_l and "got it" in body_l) or ("balance" in body_l and "sports" in body_l):
                return True

            for sel in [
                'text=Sports',
                'text=Pending Wagers',
                'text=Messages',
                'text=Transactions',
                'a:has-text("Log Out")',
            ]:
                loc = self.page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=450):
                    return True

            sign_in = self.page.locator('.signin-form, button:has-text("Sign In")').first
            if await sign_in.count() and await sign_in.is_visible(timeout=350):
                return False
            return True
        except Exception:
            return False

    async def login(self) -> bool:
        if not self.username or not self.password:
            logger.warning(f"[{self.PLATFORM_NAME}] No credentials configured")
            return False

        logger.info(f"[{self.PLATFORM_NAME}] Logging in...")
        t0 = time.monotonic()
        try:
            await self.safe_goto(self._origin() + "/pla/#/msg", timeout=22000)
            await asyncio.sleep(0.35)
            await self._dismiss_overlays()

            # Already signed in (restored session / notice screen).
            if await self._verify_login():
                self.is_logged_in = True
                logger.info(f"[{self.PLATFORM_NAME}] Login already active ({time.monotonic() - t0:.2f}s)")
                return True

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
                if await self.safe_fill(sel, self.username, timeout=2500):
                    user_filled = True
                    break

            pass_filled = False
            for sel in pass_selectors:
                if await self.safe_fill(sel, self.password, timeout=2500):
                    pass_filled = True
                    break

            if user_filled and pass_filled:
                submitted = await self.safe_click(
                    '.signin-form button[type="submit"], .signin-form .btn-primary, button[type="submit"]',
                    timeout=4500,
                )
                if not submitted:
                    try:
                        await self.page.keyboard.press("Enter")
                    except Exception:
                        pass
                # Wait up to ~8s for shell to appear; no hard fixed 5s delay.
                for _ in range(24):
                    await asyncio.sleep(0.35)
                    await self._dismiss_overlays()
                    if await self._verify_login():
                        break

            # If fields weren't found we may already be logged in on notice/shell.
            self.is_logged_in = await self._verify_login()

            if self.is_logged_in:
                logger.info(f"[{self.PLATFORM_NAME}] Login successful ({time.monotonic() - t0:.2f}s)")
            else:
                # One fast recovery attempt for transient SPA routing.
                await self.safe_goto(self._origin() + "/pla/#/msg", timeout=16000)
                await asyncio.sleep(0.25)
                await self._dismiss_overlays()
                self.is_logged_in = await self._verify_login()
            if not self.is_logged_in:
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

    def _is_prop_bet(self, bet: dict) -> bool:
        m = (bet.get("market") or "").lower()
        s = (bet.get("selection") or "").lower()
        e = (bet.get("event") or "").lower()
        hints = ("get ", "pts", "points", "reb", "assist", "threes", "first", "race to", "player")
        return "prop" in m or any(h in s for h in hints) or any(h in e for h in hints)

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
            f'span:has-text("{text}")',
            f'div:has-text("{text}")',
            f'li:has-text("{text}")',
            f'label:has-text("{text}")',
        ]
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=450):
                    try:
                        await loc.click(timeout=timeout_ms)
                    except Exception:
                        await loc.click(timeout=timeout_ms, force=True)
                    return True
            except Exception:
                pass

        try:
            loc = self.page.get_by_text(text, exact=False).first
            if await loc.count() and await loc.is_visible(timeout=350):
                try:
                    await loc.click(timeout=timeout_ms)
                except Exception:
                    await loc.click(timeout=timeout_ms, force=True)
                return True
        except Exception:
            pass

        # Last fallback for icon cards and nested non-button blocks.
        try:
            clicked = await self.page.evaluate(
                """
                (needle) => {
                  const norm = (s) => (s || "").toLowerCase().replace(/\\s+/g, " ").trim();
                  const target = norm(needle);
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = getComputedStyle(el);
                    if (!st || st.display === "none" || st.visibility === "hidden") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 4 && r.height > 4;
                  };
                  const nodes = Array.from(document.querySelectorAll("button,a,div,span,li,label"));
                  for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    const txt = norm(el.textContent || "");
                    if (!txt) continue;
                    if (txt === target || txt.includes(target)) {
                      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
                      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
                      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                      return true;
                    }
                  }
                  return false;
                }
                """,
                text,
            )
            return bool(clicked)
        except Exception:
            pass
        return False

    async def _prepare_board(self, bet: dict) -> bool:
        mode = self._bet_mode(bet)
        target_url = self._origin() + f"/pla/#/bet?bm={mode}"

        # Fast path: if already on lines and odds are visible, skip navigation work.
        cur = (self.page.url or "").lower()
        if "pla/#/bet" in cur and "p=lines" in cur and await self._has_odds_tokens():
            return True

        last_sport_clicked = False
        last_league_clicked = False
        for attempt in range(2):
            if attempt == 0:
                await self.safe_goto(target_url, timeout=18000)
            else:
                await self.safe_goto(target_url, timeout=14000)
            await asyncio.sleep(0.35)
            await self._dismiss_overlays()

            # Some sessions already restore directly to populated lines.
            if await self._has_odds_tokens():
                return True

            sport_clicked = False
            for label in self._sport_labels(bet):
                if await self._click_text(label):
                    sport_clicked = True
                    await asyncio.sleep(0.25)
                    break

            league_clicked = False
            for label in self._league_candidates(bet):
                if await self._click_text(label):
                    league_clicked = True
                    await asyncio.sleep(0.25)
                    break

            _ = await self._click_text("CONTINUE") or await self._click_text("Continue")
            has_odds = await self._wait_for_odds_board(timeout_ms=7000 if attempt == 0 else 4000)

            if not has_odds:
                # Quick refresh try before next attempt/fail.
                _ = await self._click_text("Refresh")
                has_odds = await self._wait_for_odds_board(timeout_ms=2200)

            last_sport_clicked = sport_clicked
            last_league_clicked = league_clicked
            if has_odds:
                return True

        body = ""
        try:
            body = await self.page.locator("body").inner_text(timeout=1800)
        except Exception:
            pass

        has_odds = self._has_odds_tokens_from_text(body)
        logger.info(
            f"[{self.PLATFORM_NAME}] board prep: sport_clicked={last_sport_clicked} "
            f"league_clicked={last_league_clicked} has_odds={has_odds}"
        )
        return has_odds

    async def _click_board_pick(self, event_text: str, tokens: list[str]) -> dict:
        team_tokens = self._event_team_tokens(event_text)
        try:
            return await self.page.evaluate(
                """
                ({tokens, teams}) => {
                  const norm = (s) => (s || "").toLowerCase().replace(/\\s+/g, " ").trim();
                  const toks = (tokens || []).map(norm).filter(Boolean);
                  const tms = (teams || []).map(norm).filter(Boolean);
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = getComputedStyle(el);
                    if (!st || st.display === "none" || st.visibility === "hidden") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 4 && r.height > 4;
                  };
                  const clickEl = (el) => {
                    el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
                    el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
                    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                  };
                  const nodes = Array.from(document.querySelectorAll("button,a,span,div,.ng-value,.ng-value-label"))
                    .filter((el) => isVisible(el));
                  let best = null;
                  for (const el of nodes) {
                    if (el.children && el.children.length > 4) continue;
                    const txt = norm(el.textContent || "");
                    if (!txt || txt.length > 30) continue;
                    if (!toks.some((t) => txt === t || txt.includes(t))) continue;

                    let score = 10;
                    if (toks.some((t) => txt === t)) score += 18;
                    if (txt.length <= 10) score += 8;

                    let teamHits = 0;
                    let p = el;
                    for (let depth = 0; depth < 8 && p; depth++) {
                      const block = norm(p.textContent || "");
                      if (block.length > 2500) {
                        p = p.parentElement;
                        continue;
                      }
                      for (const tm of tms) {
                        if (tm && block.includes(tm)) teamHits += 1;
                      }
                      p = p.parentElement;
                    }
                    score += teamHits * 9;
                    if (!best || score > best.score) best = { el, text: txt, score };
                  }
                  if (!best) return { ok: false, reason: "no_pick_cell" };
                  clickEl(best.el);
                  return { ok: true, text: best.text, score: best.score };
                }
                """,
                {"tokens": tokens, "teams": team_tokens},
            )
        except Exception as e:
            return {"ok": False, "reason": f"js_error:{type(e).__name__}"}

    async def _betslip_visible(self) -> bool:
        try:
            body = (await self.page.locator("body").inner_text(timeout=1800)).lower()
            if "total bets" in body and "total at risk" in body:
                return True
            if "enter password" in body and "confirm password" in body:
                return True
        except Exception:
            pass
        for sel in ['input[name^="risk-"]', '#enter-password', 'button:has-text("Confirm Password")']:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=350):
                    return True
            except Exception:
                pass
        return False

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
        pre_event_rows: list[dict] = []
        team_a = ""
        team_b = ""
        current_team = ""
        current_event = ""
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
                    pre_event_rows = []
                    team_a = current_team
                    current_event = ""
                elif not team_b and current_team != team_a:
                    team_b = current_team
                    current_event = f"{team_a} @ {team_b}"
                    if pre_event_rows:
                        for row in pre_event_rows:
                            row["event"] = current_event
                            candidates.append(row)
                        pre_event_rows = []
                continue

            if low == "vs":
                team_a = ""
                team_b = ""
                current_team = ""
                current_event = ""
                pre_event_rows = []
                pending = ""
                continue

            if low in skip_words:
                if low in ("spread", "moneyline", "total over", "total under"):
                    pending = low
                continue

            if pending == "spread":
                m = spread_line_re.match(line)
                if m:
                    if not current_team:
                        pending = ""
                        continue
                    row = {
                        "event": current_event,
                        "market": "Spread",
                        "selection": f"{current_team} {m.group(1).replace('½', '.5')}".strip(),
                        "odds_american": int(m.group(2)),
                    }
                    if current_event:
                        candidates.append(row)
                    elif team_a and not team_b and current_team == team_a:
                        pre_event_rows.append(row)
                    pending = ""
                    continue

            if pending == "moneyline":
                if ml_line_re.match(line):
                    if not current_team:
                        pending = ""
                        continue
                    row = {
                        "event": current_event,
                        "market": "Moneyline",
                        "selection": current_team,
                        "odds_american": int(line),
                    }
                    if current_event:
                        candidates.append(row)
                    elif team_a and not team_b and current_team == team_a:
                        pre_event_rows.append(row)
                    pending = ""
                    continue

            if pending in ("total over", "total under"):
                m = total_line_re.match(line)
                if m:
                    if not current_team and not team_a:
                        pending = ""
                        continue
                    side = "Over" if pending.endswith("over") else "Under"
                    row = {
                        "event": current_event,
                        "market": "Total",
                        "selection": f"{side} {m.group(1).replace('½', '.5')}",
                        "odds_american": int(m.group(2)),
                    }
                    if current_event:
                        candidates.append(row)
                    elif team_a and not team_b:
                        pre_event_rows.append(row)
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

    async def _search_props(self, bet: dict) -> list[dict]:
        payload = {"event": bet.get("event", ""), "selection": bet.get("selection", "")}
        raw = await self.page.evaluate(
            """
            async (input) => {
              const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const tokens = norm(`${input.event} ${input.selection}`).split(" ").filter(w => w.length > 2);
              const out = [];
              const links = Array.from(document.querySelectorAll("a,button,span,div"))
                .filter(el => /\+\d+\s*props|\\bprops\\b|\\bmore\\b/i.test((el.textContent || "").trim()))
                .slice(0, 6);

              for (const el of links) {
                try {
                  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                  await new Promise(r => setTimeout(r, 900));
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

        try:
            if self._url_suggests_login_wall(self.page.url or ""):
                logger.warning(f"[{self.PLATFORM_NAME}] Session expired, relogging...")
                self.is_logged_in = False
                if not await self.login_with_retry(max_attempts=2):
                    return []
            elif not await self._verify_login():
                logger.warning(f"[{self.PLATFORM_NAME}] Session check failed, relogging...")
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
            if self._is_prop_bet(bet):
                scraped_props = await self._search_props(bet)
            else:
                scraped_props = []

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
            results.extend(scraped_props)

            logger.info(f"[{self.PLATFORM_NAME}] Scraped {len(results)} odds candidates")

        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Search error: {e}")

        return results

    async def submit_bet(
        self,
        bet: dict,
        matched: dict | None = None,
        stake: float = 25.0,
        max_risk: float = 50.0,
        confirm_password: str = "",
        dry_run: bool = False,
    ) -> dict:
        if not self.is_logged_in:
            return {"success": False, "status": "not_logged_in", "platform": self.PLATFORM_NAME}

        try:
            ready = await self._prepare_board(bet)
            if not ready:
                return {"success": False, "status": "board_not_ready", "platform": self.PLATFORM_NAME}

            event_text = (matched or {}).get("event") or bet.get("event", "")
            tokens = self._submit_tokens(bet, matched or {})
            clicked = await self._click_board_pick(event_text, tokens)
            if not clicked.get("ok"):
                clicked = await self._click_market_candidate(event_text, tokens, timeout_ms=2500)
            if not clicked.get("ok"):
                odds_am = (matched or {}).get("odds_american")
                if odds_am is not None:
                    odds_token = f"{int(odds_am):+d}"
                    if await self._click_text(odds_token):
                        clicked = {"ok": True, "text": odds_token, "score": 0}
            if not clicked.get("ok"):
                return {
                    "success": False,
                    "status": "odds_click_failed",
                    "platform": self.PLATFORM_NAME,
                    "tokens": tokens[:8],
                }

            # Diamond uses explicit "VIEW BET SLIP" flow.
            _ = await self._click_text("VIEW BET SLIP") or await self._click_text("View Bet Slip") or await self._click_text("BET SLIP")
            await asyncio.sleep(0.25)
            if not await self._betslip_visible():
                odds_am = (matched or {}).get("odds_american")
                if odds_am is not None:
                    _ = await self._click_text(f"{int(odds_am):+d}")
                    _ = await self._click_text("VIEW BET SLIP") or await self._click_text("View Bet Slip") or await self._click_text("BET SLIP")
                    await asyncio.sleep(0.25)

            filled = await self._fill_stake_input(stake)
            if not filled.get("ok"):
                # Fallback: click exact odds token directly, reopen slip, retry stake fill.
                odds_am = (matched or {}).get("odds_american")
                if odds_am is not None:
                    try:
                        odds_token = f"{int(odds_am):+d}"
                        _ = await self._click_text(odds_token)
                        _ = await self._click_text("VIEW BET SLIP") or await self._click_text("View Bet Slip") or await self._click_text("BET SLIP")
                        await asyncio.sleep(0.2)
                        filled = await self._fill_stake_input(stake)
                    except Exception:
                        pass
            if not filled.get("ok"):
                return {
                    "success": False,
                    "status": "stake_fill_failed",
                    "platform": self.PLATFORM_NAME,
                    "details": filled,
                }

            await asyncio.sleep(0.3)
            total_risk = await self._read_total_risk()
            if total_risk is not None:
                if total_risk > max_risk + 0.01:
                    return {
                        "success": False,
                        "status": "risk_above_limit",
                        "platform": self.PLATFORM_NAME,
                        "total_risk": total_risk,
                    }
                if total_risk < float(stake) - 0.01:
                    return {
                        "success": False,
                        "status": "risk_below_target",
                        "platform": self.PLATFORM_NAME,
                        "total_risk": total_risk,
                    }

            effective_pw = confirm_password or self.password or ""
            if effective_pw:
                _ = await self._fill_confirm_password(effective_pw)

            if dry_run:
                return {
                    "success": True,
                    "status": "dry_run_ready",
                    "platform": self.PLATFORM_NAME,
                    "clicked": clicked,
                    "total_risk": total_risk,
                }

            submitted = await self._click_submit_button()
            if not submitted:
                return {
                    "success": False,
                    "status": "submit_button_not_found",
                    "platform": self.PLATFORM_NAME,
                    "total_risk": total_risk,
                }

            outcome = await self._await_submit_outcome(timeout_ms=15000)
            return {
                **outcome,
                "platform": self.PLATFORM_NAME,
                "clicked": clicked,
                "total_risk": total_risk,
            }
        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Submit error: {e}")
            return {
                "success": False,
                "status": "submit_exception",
                "platform": self.PLATFORM_NAME,
                "error": f"{type(e).__name__}: {e}",
            }
