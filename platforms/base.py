"""
Base platform scraper class.
All platform-specific scrapers inherit from this.
"""

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)


class BasePlatformScraper(ABC):
    """Abstract base class for all betting platform scrapers."""

    PLATFORM_NAME = "base"

    def __init__(self, config: dict, headless: bool = True):
        self.config = config
        self.headless = headless
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.url = config.get("url", "")
        # Optional proxy: {"server": "http://host:port", "username": "...", "password": "..."}
        self.proxy: Optional[dict] = config.get("proxy")
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None
        self.is_logged_in = False

    async def start(self) -> bool:
        """Start the browser and context."""
        try:
            self._playwright = await async_playwright().start()
            launch_kwargs = dict(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ]
            )
            if self.proxy:
                launch_kwargs["proxy"] = self.proxy
            self.browser = await self._playwright.chromium.launch(**launch_kwargs)
            self.context = await self.browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
                timezone_id="Europe/London",
            )
            self.page = await self.context.new_page()
            logger.info(f"[{self.PLATFORM_NAME}] Browser started")
            return True
        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Failed to start browser: {e}")
            return False

    async def stop(self):
        """Shutdown the browser cleanly."""
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self._playwright:
                await self._playwright.stop()
            logger.info(f"[{self.PLATFORM_NAME}] Browser stopped")
        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Error stopping browser: {e}")

    @abstractmethod
    async def login(self) -> bool:
        """Log into the platform. Must be implemented per platform."""
        pass

    async def login_with_retry(self, max_attempts: int = 2) -> bool:
        """Calls login() up to max_attempts times. Returns True on first success."""
        for attempt in range(1, max_attempts + 1):
            try:
                if await self.login():
                    return True
                if attempt < max_attempts:
                    logger.warning(f"[{self.PLATFORM_NAME}] Login attempt {attempt} failed, retrying in 5s...")
                    # Reload the page before retrying
                    await asyncio.sleep(5)
                    if self.page:
                        try:
                            await self.page.reload(wait_until="domcontentloaded", timeout=20000)
                            await asyncio.sleep(3)
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"[{self.PLATFORM_NAME}] Login attempt {attempt} error: {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(5)
        logger.error(f"[{self.PLATFORM_NAME}] All {max_attempts} login attempts failed")
        return False

    @abstractmethod
    async def search_bets(self, bet: dict) -> list[dict]:
        """
        Search for bets matching the given bet details.
        Returns a list of found bets, each as a dict with keys:
          - event: str
          - sport: str
          - market: str
          - selection: str
          - odds: float
          - url: str
        """
        pass

    async def submit_bet(
        self,
        bet: dict,
        matched: Optional[dict] = None,
        stake: float = 25.0,
        max_risk: float = 50.0,
        confirm_password: str = "",
        dry_run: bool = False,
    ) -> dict:
        """
        Platform-specific submit operation.
        Override this in concrete scrapers.
        """
        return {
            "success": False,
            "status": "not_implemented",
            "message": f"{self.PLATFORM_NAME} submit_bet not implemented",
            "platform": self.PLATFORM_NAME,
        }

    async def safe_goto(self, url: str, timeout: int = 30000) -> bool:
        """Navigate to URL with error handling."""
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Navigation failed to {url}: {e}")
            return False

    async def safe_click(self, selector: str, timeout: int = 10000) -> bool:
        """Click element with error handling."""
        try:
            await self.page.click(selector, timeout=timeout)
            return True
        except Exception as e:
            logger.warning(f"[{self.PLATFORM_NAME}] Click failed on '{selector}': {e}")
            return False

    async def safe_fill(self, selector: str, value: str, timeout: int = 10000) -> bool:
        """Fill input with error handling."""
        try:
            await self.page.fill(selector, value, timeout=timeout)
            return True
        except Exception as e:
            logger.warning(f"[{self.PLATFORM_NAME}] Fill failed on '{selector}': {e}")
            return False

    async def wait_for_selector(self, selector: str, timeout: int = 10000) -> bool:
        """Wait for a selector to appear."""
        try:
            await self.page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception:
            return False

    async def screenshot(self, path: str):
        """Capture screenshot for debugging."""
        try:
            await self.page.screenshot(path=path, full_page=True)
        except Exception as e:
            logger.warning(f"[{self.PLATFORM_NAME}] Screenshot failed: {e}")

    def browser_tab_open(self) -> bool:
        """True if Playwright browser + page exist and the tab is not closed (no JS eval)."""
        try:
            return bool(
                self.browser
                and self.context
                and self.page
                and not self.page.is_closed()
            )
        except Exception:
            return False

    def _url_suggests_login_wall(self, url: str) -> bool:
        """Narrow checks only — avoid substrings like 'expired' inside 'unexpired' / 'expires_in'."""
        u = url.lower()
        if "#/login" in u or u.rstrip("/").endswith("/login"):
            return True
        if "expired=true" in u or "session=expired" in u or "sessionexpired" in u:
            return True
        if "signout=true" in u or "/signout" in u or "/logout" in u:
            return True
        return False

    async def is_session_alive(self) -> bool:
        """
        Quick check: browser usable and we still believe we're logged in.
        Avoids broad URL substring heuristics that false-negative SPA URLs.
        """
        if not self.is_logged_in:
            return False
        if not self.browser or not self.page:
            return False
        try:
            if self.page.is_closed():
                return False
        except Exception:
            return False
        try:
            if self._url_suggests_login_wall(self.page.url):
                return False
        except Exception:
            pass
        try:
            await self.page.evaluate("() => document.readyState", timeout=8000)
            return True
        except Exception:
            # Often fails while search_bets is navigating — page is not closed, session likely fine
            try:
                return not self.page.is_closed()
            except Exception:
                return False

    def normalize_odds(self, raw: str) -> Optional[float]:
        """Convert odds string (decimal, fractional, american) to decimal float.

        Handles:
          American: +150, -110, +700  → decimal equivalent
          Decimal:  1.85, 2.10        → returned as-is
          Fractional: 5/2             → decimal equivalent
        """
        import re as _re
        if not raw:
            return None
        raw = raw.strip().replace(",", ".")

        # American odds must be detected FIRST (before float() swallows them).
        # Pattern: optional +/-, exactly 3-4 digits, nothing else.
        if _re.fullmatch(r'[+-]?\d{3,4}', raw):
            try:
                american = int(raw)
                if american == 0:
                    return None
                if american > 0:
                    return round(american / 100 + 1.0, 3)
                else:
                    return round(100 / abs(american) + 1.0, 3)
            except Exception:
                pass

        # Decimal odds (e.g. 1.85, 2.10)
        try:
            val = float(raw)
            if 1.01 < val < 100:
                return round(val, 3)
        except ValueError:
            pass

        # Fractional e.g. "5/2"
        if "/" in raw:
            try:
                num, den = raw.split("/")
                return round(float(num) / float(den) + 1.0, 3)
            except Exception:
                pass

        return None

    # ── Submit helpers (shared by platform submit flows) ─────────────────────

    def _event_team_tokens(self, event: str) -> list[str]:
        if not event:
            return []
        norm = re.sub(r"\bvrs?\b", "@", event, flags=re.I)
        norm = re.sub(r"\bvs\.?\b", "@", norm, flags=re.I)
        norm = re.sub(r"\bat\b", "@", norm, flags=re.I)
        parts = [p.strip() for p in re.split(r"@", norm) if p.strip()]
        if not parts:
            parts = [event]

        tokens: list[str] = []
        for p in parts[:2]:
            p2 = re.sub(r"[^a-zA-Z0-9 ]+", " ", p).strip()
            words = [w for w in p2.split() if len(w) > 2]
            if words:
                tokens.append(" ".join(words[:3]))
        return list(dict.fromkeys(tokens))

    def _line_token_variants(self, line: Optional[float]) -> list[str]:
        if line is None:
            return []
        try:
            f = float(line)
        except Exception:
            return []
        if abs(f - int(f)) < 1e-9:
            return [str(int(f))]
        whole = int(f)
        frac = round(f - whole, 2)
        out = [f"{f:g}"]
        if abs(frac - 0.5) < 1e-9:
            out.extend([f"{whole}.5", f"{whole}½"])
        return list(dict.fromkeys(out))

    def _submit_tokens(self, bet: dict, matched: Optional[dict]) -> list[str]:
        market = (bet.get("market") or matched.get("market") if matched else bet.get("market") or "").lower()
        side = (bet.get("bet_side") or "").lower()
        line = bet.get("line")
        odds = None
        if matched:
            odds = matched.get("odds_american")
        if odds is None:
            odds = bet.get("odds_american")

        tokens: list[str] = []
        odds_token = ""
        try:
            if odds is not None:
                o = int(odds)
                odds_token = f"{o:+d}"
        except Exception:
            pass

        line_tokens = self._line_token_variants(line)
        if "total" in market or side in ("over", "under"):
            prefix = "o" if side == "over" else "u" if side == "under" else ""
            for lt in line_tokens:
                if prefix:
                    tokens.append(f"{prefix}{lt}")
                    if odds_token:
                        tokens.append(f"{prefix}{lt} ({odds_token})")
                        tokens.append(f"{prefix}{lt}({odds_token})")
        elif "spread" in market:
            sel = (matched.get("selection") if matched else bet.get("selection") or "")
            if sel:
                sel_clean = sel.replace(" .5", "½").replace(".5", "½")
                tokens.append(sel_clean)
            for lt in line_tokens:
                if lt and not lt.startswith(("+", "-")):
                    continue
                tokens.append(lt)
                if odds_token:
                    tokens.append(f"{lt} ({odds_token})")
                    tokens.append(f"{lt}({odds_token})")
        elif "money" in market or market == "ml":
            sel = (matched.get("selection") if matched else bet.get("selection") or "")
            if odds_token:
                tokens.append(odds_token)
            if sel:
                tokens.append(sel)

        add_plain_odds = ("money" in market or market == "ml") and odds_token not in tokens
        if add_plain_odds and odds_token and odds_token not in tokens:
            tokens.append(odds_token)
        return [t for t in list(dict.fromkeys(tokens)) if t]

    async def _click_market_candidate(self, event_text: str, tokens: list[str], timeout_ms: int = 5000) -> dict:
        if not tokens:
            return {"ok": False, "reason": "no_tokens"}

        team_tokens = self._event_team_tokens(event_text)
        try:
            clicked = await self.page.evaluate(
                """
                ({tokens, teamTokens}) => {
                  const norm = (s) => (s || "").toLowerCase().replace(/\\s+/g, " ").trim();
                  const toks = (tokens || []).map(norm).filter(Boolean);
                  const teams = (teamTokens || []).map(norm).filter(Boolean);
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

                  const nodes = Array.from(document.querySelectorAll("button,a,div,span,label,li,ng-select,.ng-value,.ng-value-label"));
                  let best = null;

                  for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    const txt = norm(el.textContent || "");
                    if (!txt) continue;
                    // Avoid giant container blocks; target compact odds cells/buttons.
                    if (txt.length > 90) continue;
                    if (el.children && el.children.length > 8) continue;
                    const tokenHit = toks.some((t) => txt === t || txt.includes(t));
                    if (!tokenHit) continue;

                    let score = 10;
                    for (const t of toks) {
                      if (txt === t) score += 10;
                      else if (txt.includes(t)) score += 4;
                    }
                    if (txt.length <= 14) score += 10;
                    if (/^[ou][0-9]/i.test(txt) || /^[+-][0-9]{3,4}$/.test(txt)) score += 12;

                    let p = el;
                    for (let depth = 0; depth < 8 && p; depth++) {
                      const block = norm(p.textContent || "");
                      if (block.length > 3500) {
                        p = p.parentElement;
                        continue;
                      }
                      for (const team of teams) {
                        if (team && block.includes(team)) score += 5;
                      }
                      p = p.parentElement;
                    }
                    if (!best || score > best.score) best = { el, score, text: txt };
                  }

                  if (!best) return { ok: false, reason: "no_candidate" };
                  clickEl(best.el);
                  return { ok: true, text: best.text, score: best.score };
                }
                """,
                {"tokens": tokens, "teamTokens": team_tokens},
            )
            if clicked and clicked.get("ok"):
                await asyncio.sleep(min(1.0, timeout_ms / 1000.0))
                return clicked
        except Exception:
            pass

        # Fallback text-click loop.
        for t in tokens:
            for sel in [
                f'text="{t}"',
                f'button:has-text("{t}")',
                f'a:has-text("{t}")',
                f'div:has-text("{t}")',
                f'span:has-text("{t}")',
            ]:
                try:
                    loc = self.page.locator(sel).first
                    if await loc.count() and await loc.is_visible(timeout=500):
                        try:
                            await loc.click(timeout=900)
                        except Exception:
                            await loc.click(timeout=900, force=True)
                        await asyncio.sleep(0.2)
                        return {"ok": True, "text": t, "score": 0}
                except Exception:
                    pass
        return {"ok": False, "reason": "click_failed"}

    async def _fill_stake_input(self, stake: float) -> dict:
        try:
            val = str(int(stake) if float(stake).is_integer() else stake)
        except Exception:
            val = str(stake)
        try:
            filled = await self.page.evaluate(
                """
                (stakeValue) => {
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = getComputedStyle(el);
                    if (!st || st.display === "none" || st.visibility === "hidden") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 30 && r.height > 14;
                  };
                  const bad = /(search|username|login|password|dummy|win\\b)/i;
                  const good = /(risk|wager|stake|amount|betslip|at risk|to risk)/i;
                  const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="password"])'));
                  let best = null;
                  for (const el of inputs) {
                    if (!isVisible(el)) continue;
                    const id = (el.id || "");
                    const name = (el.name || "");
                    const cls = (el.className || "");
                    const ph = (el.placeholder || "");
                    const parent = (el.parentElement?.innerText || "");
                    const all = `${id} ${name} ${cls} ${ph} ${parent}`.toLowerCase();
                    if (bad.test(all)) continue;
                    let score = 0;
                    if (good.test(all)) score += 20;
                    if (/risk[-_]/i.test(name)) score += 20;
                    if (/betslip/i.test(cls)) score += 12;
                    if (/base\\b/i.test(all) && !/risk|stake|wager|amount/.test(all)) score -= 15;
                    const r = el.getBoundingClientRect();
                    score += Math.max(0, 8 - Math.floor(r.top / 140));
                    if (!best || score > best.score) best = { el, score };
                  }
                  if (!best) return { ok: false, reason: "input_not_found" };
                  const input = best.el;
                  input.focus();
                  input.value = "";
                  input.dispatchEvent(new Event("input", { bubbles: true }));
                  input.value = String(stakeValue);
                  input.dispatchEvent(new Event("input", { bubbles: true }));
                  input.dispatchEvent(new Event("change", { bubbles: true }));
                  input.dispatchEvent(new KeyboardEvent("keyup", { key: "5", bubbles: true }));
                  input.blur();
                  return { ok: true, name: input.name || "", id: input.id || "", className: input.className || "" };
                }
                """,
                val,
            )
            await asyncio.sleep(0.25)
            return filled or {"ok": False, "reason": "fill_failed"}
        except Exception as e:
            return {"ok": False, "reason": f"fill_exception:{type(e).__name__}"}

    async def _read_total_risk(self) -> Optional[float]:
        try:
            body = await self.page.locator("body").inner_text(timeout=2000)
        except Exception:
            return None
        pats = [
            r"Total\s*At\s*Risk\s*\$?\s*([0-9]+(?:\.[0-9]{1,2})?)",
            r"At\s*Risk\s*\$?\s*([0-9]+(?:\.[0-9]{1,2})?)",
            r"Risk\s*[:$]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
        ]
        for p in pats:
            m = re.search(p, body, re.I)
            if m:
                try:
                    return float(m.group(1))
                except Exception:
                    pass
        return None

    async def _fill_confirm_password(self, confirm_password: str) -> bool:
        if not confirm_password:
            return False
        for sel in [
            "#enter-password",
            'input[placeholder*="Password"]',
            'input[name*="password"]',
            'input[type="password"]',
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=600):
                    await loc.fill(confirm_password, timeout=1200)
                    return True
            except Exception:
                pass
        return False

    async def _click_submit_button(self) -> bool:
        submit_texts = [
            "Confirm Password",
            "Place Bet",
            "Submit",
            "Wager",
            "Confirm",
            "Continue",
            "CONTINUE",
        ]
        for text in submit_texts:
            for sel in [
                f'button:has-text("{text}")',
                f'a:has-text("{text}")',
                f'div:has-text("{text}")',
                f'span:has-text("{text}")',
                f'text="{text}"',
            ]:
                try:
                    loc = self.page.locator(sel).first
                    if await loc.count() and await loc.is_visible(timeout=500):
                        try:
                            await loc.click(timeout=1000)
                        except Exception:
                            await loc.click(timeout=1000, force=True)
                        await asyncio.sleep(0.25)
                        return True
                except Exception:
                    pass
        return False

    async def _await_submit_outcome(self, timeout_ms: int = 12000) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
        last = ""
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            try:
                body = await self.page.locator("body").inner_text(timeout=1800)
            except Exception:
                continue
            low = (body or "").lower()
            last = low[:2000]
            if "invalid password" in low:
                return {"success": False, "status": "invalid_password"}
            if "minimum" in low and "risk" in low:
                return {"success": False, "status": "min_risk_not_met"}
            if "insufficient" in low and "balance" in low:
                return {"success": False, "status": "insufficient_balance"}
            if ("oops" in low and "error" in low) or "unable to process" in low:
                return {"success": False, "status": "submit_error"}
            if (
                "ticket #" in low
                or "wagered" in low
                or "wager accepted" in low
                or "pending wagers" in low
                or "open bets" in low
            ):
                return {"success": True, "status": "submitted_confirmed"}
        return {"success": False, "status": "submit_unverified", "sample": last[:500]}
