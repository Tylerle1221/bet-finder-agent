"""
V2Sports platform scraper - DGS pay-per-head platform.
Used by Smash66 and Leftcoast797.

Sports page: /v2/#/sports  (shows event list with evId links)
Game odds page: /v2/#/schedule?evId=XXXXXX  (shows full spread/ML/total board)
"""
import asyncio
import logging
import re
from .base import BasePlatformScraper

logger = logging.getLogger(__name__)


class V2SportsScraper(BasePlatformScraper):
    """Base scraper for any site running the /v2/#/sports DGS platform."""
    PLATFORM_NAME = "V2Sports"

    def _base_origin(self):
        from urllib.parse import urlparse
        p = urlparse(self.url)
        return f"{p.scheme}://{p.netloc}"

    async def login(self) -> bool:
        if not self.username or not self.password:
            logger.warning(f"[{self.PLATFORM_NAME}] No credentials configured")
            return False

        logger.info(f"[{self.PLATFORM_NAME}] Attempting login to {self._base_origin()}...")
        try:
            await self.safe_goto(self._base_origin() + "/")
            await asyncio.sleep(3)
            await self._dismiss_overlays()

            if not await self.safe_fill('#customerid', self.username, timeout=12000):
                if not await self.safe_fill('input[name="customerid"]', self.username, timeout=5000):
                    logger.error(f"[{self.PLATFORM_NAME}] Username field not found")
                    return False

            await self.safe_fill('#password', self.password)
            await asyncio.sleep(0.5)

            clicked = await self.safe_click('input#submit, button[name="button"], .login__submit, .login_btn', timeout=6000)
            if not clicked:
                await self.page.keyboard.press("Enter")

            await asyncio.sleep(5)
            self.is_logged_in = await self._verify_login()
            if self.is_logged_in:
                logger.info(f"[{self.PLATFORM_NAME}] Login successful")
            else:
                logger.error(f"[{self.PLATFORM_NAME}] Login failed (URL: {self.page.url})")
            return self.is_logged_in

        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Login error: {e}")
            return False

    async def _dismiss_overlays(self):
        for sel in ['button:has-text("Accept")', '.cookie-btn', '[class*="close"]']:
            try:
                await self.page.click(sel, timeout=1500)
                await asyncio.sleep(0.3)
            except Exception:
                pass

    async def _verify_login(self) -> bool:
        cur = self.page.url
        if "sports" in cur or "v2" in cur:
            return True
        for sel in ['[class*="balance"]', '[class*="Balance"]', '[class*="account"]', '#balance']:
            if await self.wait_for_selector(sel, timeout=2000):
                return True
        return not await self.wait_for_selector('#customerid', timeout=1500)

    def _market_key(self, bet: dict) -> str:
        market = (bet.get("market") or "").lower()
        if "team total" in market:
            return "team_total"
        if "total" in market or bet.get("bet_side") in ("over", "under"):
            return "total"
        if "spread" in market:
            return "spread"
        if "moneyline" in market or market == "ml":
            return "moneyline"
        return ""

    def _is_prop_bet(self, bet: dict) -> bool:
        m = (bet.get("market") or "").lower()
        s = (bet.get("selection") or "").lower()
        e = (bet.get("event") or "").lower()
        hints = ("get ", "pts", "points", "reb", "assist", "threes", "first", "race to", "player")
        return "prop" in m or any(h in s for h in hints) or any(h in e for h in hints)

    async def _search_props(self, bet: dict) -> list[dict]:
        """Best-effort prop extraction by opening +Props links and scanning text+odds."""
        payload = {"event": bet.get("event", ""), "selection": bet.get("selection", "")}
        raw = await self.page.evaluate(
            """
            async (input) => {
              const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const tokens = norm(`${input.event} ${input.selection}`).split(" ").filter(w => w.length > 2);
              const out = [];

              const propButtons = Array.from(document.querySelectorAll("a,button,span,div"))
                .filter(el => /\+\d+\s*props/i.test((el.textContent || "").trim()))
                .slice(0, 4);

              for (const btn of propButtons) {
                try {
                  btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                  await new Promise(r => setTimeout(r, 900));
                } catch {}

                const lines = (document.body?.innerText || "").split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);
                for (const line of lines) {
                  const low = norm(line);
                  const score = tokens.length ? tokens.filter(t => low.includes(t)).length : 0;
                  if (score < Math.max(1, Math.floor(tokens.length * 0.35))) continue;
                  const am = line.match(/([+-]\\d{3,4})/);
                  if (!am) continue;
                  out.push({
                    event: input.event || "",
                    market: "Prop",
                    selection: line.slice(0, 140),
                    odds_american: Number.parseInt(am[1], 10),
                  });
                }
                document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
                await new Promise(r => setTimeout(r, 120));
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

    async def _has_schedule_lines(self) -> bool:
        try:
            return await self.page.evaluate(
                "() => /\\b(?:o|u)\\d+|[+-]\\d{3,4}/i.test(document.body?.innerText || '')"
            )
        except Exception:
            return False

    async def _ensure_schedule_ready(self, bet: dict):
        await self.safe_goto(self._base_origin() + "/v2/#/schedule")
        await asyncio.sleep(1.5)
        if await self._has_schedule_lines():
            return

        # Fresh sessions may need league selection before lines render.
        await self.safe_goto(self._base_origin() + "/v2/#/sports")
        await asyncio.sleep(1.5)
        await self._dismiss_overlays()

        sport = (bet.get("sport") or "").lower()
        labels = []
        if "basket" in sport:
            labels = ["NBA - Playoffs", "NBA"]
        elif "football" in sport:
            labels = ["NFL", "NCAAF", "Football"]
        elif "baseball" in sport:
            labels = ["MLB", "Baseball"]
        elif "hockey" in sport:
            labels = ["NHL", "Hockey"]
        elif "soccer" in sport:
            labels = ["Soccer"]

        for label in labels:
            try:
                item = self.page.get_by_text(label, exact=False).first
                if await item.is_visible(timeout=1000):
                    await item.click()
                    await asyncio.sleep(0.2)
            except Exception:
                pass
            try:
                chk = self.page.get_by_role("checkbox", name=label).first
                if await chk.is_visible(timeout=1000):
                    await chk.check(force=True)
            except Exception:
                pass

        try:
            cont = self.page.get_by_role("button", name=re.compile("CONTINUE", re.I)).first
            if await cont.is_visible(timeout=2000):
                await cont.click()
                await asyncio.sleep(1.2)
        except Exception:
            pass

        await self.safe_goto(self._base_origin() + "/v2/#/schedule")
        try:
            upd = self.page.get_by_role("button", name=re.compile("UPDATE", re.I)).first
            if await upd.is_visible(timeout=1500):
                await upd.click()
        except Exception:
            pass
        await asyncio.sleep(1.2)

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
            await self._ensure_schedule_ready(bet)
            await self._dismiss_overlays()
            if not await self._has_schedule_lines():
                return {"success": False, "status": "no_schedule_lines", "platform": self.PLATFORM_NAME}

            event_text = (matched or {}).get("event") or bet.get("event", "")
            tokens = self._submit_tokens(bet, matched or {})
            clicked = await self._click_market_candidate(event_text, tokens, timeout_ms=4500)
            if not clicked.get("ok"):
                return {
                    "success": False,
                    "status": "odds_click_failed",
                    "platform": self.PLATFORM_NAME,
                    "tokens": tokens[:8],
                }

            # Some skins require opening bet slip first.
            for text in ("VIEW BET SLIP", "View Bet Slip", "BET SLIP"):
                try:
                    loc = self.page.get_by_text(text, exact=False).first
                    if await loc.count() and await loc.is_visible(timeout=350):
                        try:
                            await loc.click(timeout=700)
                        except Exception:
                            await loc.click(timeout=700, force=True)
                        await asyncio.sleep(0.2)
                        break
                except Exception:
                    pass

            filled = await self._fill_stake_input(stake)
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

            # Some books ask confirm password, some don't.
            _ = await self._fill_confirm_password(confirm_password or "")
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

            outcome = await self._await_submit_outcome(timeout_ms=12000)
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

    async def search_bets(self, bet: dict) -> list[dict]:
        if not self.is_logged_in:
            logger.warning(f"[{self.PLATFORM_NAME}] Not logged in")
            return []

        results: list[dict] = []
        try:
            await self._ensure_schedule_ready(bet)
            if not await self._has_schedule_lines():
                logger.warning(f"[{self.PLATFORM_NAME}] Schedule has no visible odds lines")
                return []
            await self._dismiss_overlays()

            payload = {
                "event": bet.get("event", ""),
                "marketKey": self._market_key(bet),
                "includeDropdown": True,
            }

            scraped = await self.page.evaluate(
                """
                async (input) => {
                  const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
                  const toDecimal = (am) => {
                    const n = Number.parseInt(am, 10);
                    if (!Number.isFinite(n) || n === 0) return null;
                    return n > 0 ? Number((n / 100 + 1).toFixed(3)) : Number((100 / Math.abs(n) + 1).toFixed(3));
                  };
                  const americanFromJuice = (s) => {
                    const m = (s || "").match(/-?\\d{3,4}/);
                    return m ? Number.parseInt(m[0], 10) : null;
                  };
                  const parseEntry = (token, juiceToken, gameTitle, teamName) => {
                    const t = (token || "").replace(/\\s+/g, "").trim();
                    if (!t) return null;
                    const juice = americanFromJuice(juiceToken);

                    if (/^[ou]\\d+(?:\\.\\d+|\\u00bd)?$/i.test(t)) {
                      const side = t[0].toLowerCase() === "o" ? "Over" : "Under";
                      const line = t.slice(1).replace(/\\u00bd/g, ".5");
                      const selection = `${side} ${line}`;
                      return {
                        event: gameTitle,
                        market: "Total",
                        selection,
                        odds_american: juice,
                        odds: toDecimal(juice),
                      };
                    }
                    if (/^[+-]\\d+(?:\\.\\d+|\\u00bd)?$/.test(t)) {
                      if (Math.abs(Number.parseInt(t, 10)) >= 100) {
                        const am = Number.parseInt(t, 10);
                        return {
                          event: gameTitle,
                          market: "Moneyline",
                          selection: teamName,
                          odds_american: am,
                          odds: toDecimal(am),
                        };
                      }
                      return {
                        event: gameTitle,
                        market: "Spread",
                        selection: `${teamName} ${t.replace(/\\u00bd/g, ".5")}`,
                        odds_american: juice,
                        odds: toDecimal(juice),
                      };
                    }
                    return null;
                  };

                  const linesRaw = (document.body?.innerText || "").split(/\\r?\\n/).map((s) => s.trim()).filter(Boolean);
                  const lines = [];
                  for (let i = 0; i < linesRaw.length; i++) {
                    const cur = linesRaw[i];
                    const nxt = linesRaw[i + 1];
                    if (nxt && /^\\d{1,2}:\\d{2}[ap](?:m)?\\s*ET$/i.test(cur) && /^Playoffs\\b/i.test(nxt)) {
                      lines.push(`${cur} ${nxt}`.replace(/\\s+/g, " ").trim());
                      i += 1;
                      continue;
                    }
                    lines.push(cur);
                  }

                  const dayHeader = /^(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)\\b.+\\bPlayoffs\\s*$/i;
                  const gameStart = (line) => line.includes("@") && /Playoffs/i.test(line) && /\\d{1,2}:\\d{2}[ap](?:m)?/i.test(line);
                  const games = [];
                  let day = "";
                  for (let i = 0; i < lines.length; i++) {
                    const line = lines[i];
                    if (dayHeader.test(line)) {
                      day = line;
                      continue;
                    }
                    if (!gameStart(line)) continue;
                    const teams = [];
                    let j = i + 1;
                    while (j < lines.length) {
                      const p = lines[j];
                      if (dayHeader.test(p) || gameStart(p)) break;
                      if (/^\\d{3}$/.test(p) && j + 1 < lines.length) {
                        const row = { rotation: p, name: lines[j + 1], prices: [] };
                        let k = j + 2;
                        while (k < lines.length) {
                          const x = lines[k];
                          if (
                            /^\\d{3}$/.test(x) ||
                            dayHeader.test(x) ||
                            gameStart(x) ||
                            x === "UPDATE" ||
                            x === "LOGOUT" ||
                            x === "IF BET" ||
                            x.startsWith("ID:") ||
                            /^ACTION\\s+REVERSE$/i.test(x)
                          ) break;
                          row.prices.push(x);
                          k += 1;
                        }
                        teams.push(row);
                        j = k;
                        continue;
                      }
                      j += 1;
                    }
                    games.push({ day, title: line, teams });
                  }

                  const words = norm(input.event).split(" ").filter((w) => w.length > 2);
                  const scored = games.map((g) => {
                    const hay = norm(`${g.title} ${g.teams.map((t) => t.name).join(" ")}`);
                    const score = words.length ? words.filter((w) => hay.includes(w)).length : 1;
                    return { game: g, score };
                  });
                  scored.sort((a, b) => b.score - a.score);
                  const selectedGames = scored.filter((x) => x.score > 0).slice(0, 2).map((x) => x.game);

                  const out = [];
                  for (const g of selectedGames) {
                    for (const t of g.teams) {
                      for (let i = 0; i < t.prices.length; i++) {
                        const token = t.prices[i];
                        const juice = i + 1 < t.prices.length ? t.prices[i + 1] : "";
                        const parsed = parseEntry(token, juice, g.title, t.name);
                        if (!parsed) continue;
                        if (input.marketKey) {
                          const mk = parsed.market.toLowerCase().replace(" ", "_");
                          if (mk !== input.marketKey) continue;
                        }
                        out.push(parsed);
                      }
                    }
                  }

                  if (input.includeDropdown && selectedGames.length) {
                    const target = selectedGames[0];
                    for (const team of target.teams) {
                      const teamNorm = norm(team.name);
                      const leaf = Array.from(document.querySelectorAll("*")).find((el) => {
                        if (!el || el.children.length !== 0) return false;
                        return norm(el.textContent || "") === teamNorm;
                      });
                      if (!leaf) continue;
                      let row = leaf;
                      for (let depth = 0; depth < 8 && row; depth++) {
                        if (row.querySelectorAll && row.querySelectorAll("ng-select").length) break;
                        row = row.parentElement;
                      }
                      if (!row || !row.querySelectorAll) continue;

                      const selects = Array.from(row.querySelectorAll("ng-select"));
                      for (const sel of selects) {
                        const current = (sel.textContent || "").replace(/\\s+/g, "").trim();
                        const isTotal = /^[x\\u00d7]?[ou]\\d/i.test(current);
                        const isSpread = /^[x\\u00d7]?[+-]\\d/.test(current);
                        if (!isTotal && !isSpread) continue;
                        if (input.marketKey === "total" && !isTotal) continue;
                        if (input.marketKey === "spread" && !isSpread) continue;

                        const clickTarget = sel.querySelector(".ng-arrow-wrapper,.ng-select-container,.ng-arrow") || sel;
                        clickTarget.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
                        clickTarget.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        await new Promise((r) => setTimeout(r, 250));

                        const panel = document.querySelector(".ng-dropdown-panel,[role='listbox']");
                        if (!panel) continue;
                        const options = Array.from(panel.querySelectorAll(".ng-option,[role='option']")).map((o) =>
                          (o.textContent || "").replace(/\\s+/g, "").trim()
                        );

                        for (const opt of options) {
                          const m = opt.match(/^([ou]\\d+(?:\\.\\d+|\\u00bd)?|[+-]\\d+(?:\\.\\d+|\\u00bd)?)\\((-?\\d{3,4})\\)$/i);
                          if (!m) continue;
                          const parsed = parseEntry(m[1], m[2], target.title, team.name);
                          if (!parsed) continue;
                          if (input.marketKey) {
                            const mk = parsed.market.toLowerCase().replace(" ", "_");
                            if (mk !== input.marketKey) continue;
                          }
                          out.push(parsed);
                        }

                        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
                        await new Promise((r) => setTimeout(r, 100));
                      }
                    }
                  }

                  const dedup = new Set();
                  const unique = [];
                  for (const item of out) {
                    const key = [
                      item.event || "",
                      item.market || "",
                      item.selection || "",
                      item.odds_american ?? "",
                    ].join("|");
                    if (dedup.has(key)) continue;
                    dedup.add(key);
                    unique.push(item);
                  }

                  return {
                    selectedCount: selectedGames.length,
                    candidateCount: unique.length,
                    candidates: unique,
                  };
                }
                """,
                payload,
            )

            for c in (scraped or {}).get("candidates", []):
                odds_american = c.get("odds_american")
                odds_decimal = c.get("odds")
                if odds_decimal is None and odds_american is not None:
                    odds_decimal = self.normalize_odds(str(odds_american))
                if odds_decimal is None and odds_american is None:
                    continue
                results.append(
                    {
                        "event": c.get("event", "")[:120],
                        "sport": bet.get("sport", ""),
                        "market": c.get("market", ""),
                        "selection": c.get("selection", ""),
                        "odds_american": odds_american,
                        "odds": odds_decimal,
                        "url": self.page.url,
                    }
                )

            if self._is_prop_bet(bet):
                results.extend(await self._search_props(bet))

            logger.info(
                f"[{self.PLATFORM_NAME}] Schedule scrape returned "
                f"{(scraped or {}).get('candidateCount', 0)} candidate odds"
            )

        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Search error: {e}")

        return results


class Smash66Scraper(V2SportsScraper):
    PLATFORM_NAME = "Smash66"


class Leftcoast797Scraper(V2SportsScraper):
    PLATFORM_NAME = "Leftcoast797"
