"""
NY Urban open-play watcher (alert-only by default).

Each run:
  1. Loads every watched session page (LaGuardia; Beacon "Advanced Intermediate" only).
  2. Logs each row's status (+ spots left when the site shows a number).
  3. Appends changes to sightings.csv and pushes a phone alert when spots open
     or the count goes up. One line per court.
     LaGuardia is listed first.

NYU_MODE=buy enables the old single-ticket auto-buy for the first open match.
It buys ONE ticket; alert mode is the default.

Never prints personal info, so public Actions logs are safe.

Env:
  NYU_NTFY_TOPIC     phone push via ntfy.sh (secret)
  NYU_TARGETS        "any" (default) | "today" | "11/20 12/04"
  NYU_LGA_URL        LaGuardia page (default below)
  NYU_BEACON_URL     Beacon page; Beacon is skipped until this is set
  NYU_MODE           "alert" (default) | "buy"
  buy mode only:  NYU_FIRST NYU_LAST NYU_EMAIL NYU_CARD_NUMBER NYU_CARD_EXP NYU_CARD_CVV
                  NYU_CARD_ZIP NYU_USERNAME NYU_PASSWORD, DRY_RUN=1, SCREENSHOTS=1
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

ET = ZoneInfo("America/New_York")
DEFAULT_LGA_URL = "https://www.nyurban.com/open-play-volleyball-no/?id=6&gametypeid=1&filter_id=1"
ROOT = Path(os.environ.get("NYU_DATA_DIR") or Path(__file__).parent)
STATE_FILE = ROOT / "state.json"
SIGHTINGS_FILE = ROOT / "sightings.csv"
SHOTS_DIR = ROOT / "shots"

BLOCKING = {"pending", "purchased", "unknown", "failed"}
SUCCESS_RX = re.compile(r"thank you|successfully|receipt|transaction (id|approved)|order (number|#)|you are registered|payment (complete|received)", re.I)
DECLINE_RX = re.compile(r"declined|card (was )?(rejected|invalid)|payment failed|transaction failed|could not be processed", re.I)


# ---------- utils ----------

def now() -> datetime:
    return datetime.now(ET)


def log(msg: str) -> None:
    print(f"{now():%Y-%m-%d %H:%M:%S} ET  {msg}", flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def notify(msg: str, topic: str | None = None, who: str = "legacy") -> None:
    log(f"NOTIFY[{who}]: " + msg.replace("\n", " / "))
    topic = topic if topic is not None else env("NYU_NTFY_TOPIC")
    if not topic or env("NYU_NOTIFY_DRY") == "1":
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=msg.encode(),
            headers={"Title": "NY Urban volleyball", "Priority": "high",
                     "Tags": "volleyball"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # never fail a run over a push
        log(f"ntfy failed: {type(e).__name__}")


def iso_date(mmdd: str) -> str:
    """'11/20' -> '2026-11-20', rolling into next year for Jan dates seen in Dec."""
    m, d = (int(x) for x in mmdd.split("/"))
    t = now()
    year = t.year + (1 if m < t.month - 6 else 0)
    return f"{year:04d}-{m:02d}-{d:02d}"


def shot(page: Page, name: str) -> None:
    if env("SCREENSHOTS") == "1":
        SHOTS_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS_DIR / f"{now():%H%M%S}-{name}.png"), full_page=True)


# ---------- watches ----------

def watches() -> list[dict]:
    """Ordered by priority: LaGuardia first. Venues are tabs on the same page;
    rows are also filtered by the gym name in each row so a missed tab click can't mislabel."""
    base = env("NYU_LGA_URL") or env("NYU_URL") or DEFAULT_LGA_URL
    return [
        {"venue": "LGA", "url": base, "tab": "Laguardia", "gym_rx": re.compile(r"la\s*guardia", re.I),
         "level_rx": None},
        # "Advanced Intermediate" only; plain "Advanced" rows don't match
        {"venue": "Beacon", "url": env("NYU_BEACON_URL") or base, "tab": "Beacon",
         "gym_rx": re.compile(r"beacon", re.I),
         "level_rx": re.compile(r"advanced\s*[-/]?\s*intermediate", re.I)},
    ]


def select_tab(page: Page, name: str) -> bool:
    """Click the venue tab/link/option whose text starts with `name`. Returns True if something was clicked."""
    rx = re.compile(rf"^\s*{re.escape(name)}", re.I)
    for sel in page.locator("select").all():  # dropdown variant
        try:
            labels = [o.inner_text().strip() for o in sel.locator("option").all()]
            hit = next((l for l in labels if rx.search(l)), None)
            if hit and sel.is_visible():
                sel.select_option(label=hit)
                _settle(page)
                return True
        except Exception:
            pass
    best = None
    # prefer real click targets; fall back to containers
    for css in ("a, button, [onclick], input[type=button], input[type=submit]", "li, span, td, label, div"):
        best_len = 10**9
        cands = page.locator(css, has_text=rx) if "input" not in css else page.locator(css)
        for i in range(min(cands.count(), 150)):
            c = cands.nth(i)
            try:
                t = " ".join((c.inner_text() or c.get_attribute("value") or "").split())
                if rx.search(t) and len(t) < 40 and c.is_visible() and len(t) < best_len:
                    best, best_len = c, len(t)
            except Exception:
                pass
        if best is not None:
            break
    if best is None:
        return False
    best.click()
    _settle(page)
    return True


def _settle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1000)



# ---------- venues (tabs on the signup page) ----------

VENUES = [
    # key, label shown to people, exact tab text, optional row filters that confirm the tab really switched
    {"key": "LGA", "label": "LaGuardia · Fri", "tab": "Laguardia / Fri.", "gym_rx": r"la\s*guardia"},
    {"key": "W50", "label": "W50th Street Campus · Fri", "tab": "W50th Street Campus / Fri."},
    {"key": "Beacon", "label": "Beacon · Fri", "tab": "Beacon / Fri.", "gym_rx": r"beacon"},
    {"key": "BrandeisFri", "label": "Brandeis · Fri", "tab": "Brandeis / Fri.", "day_rx": r"\bFri\b"},
    {"key": "BrandeisSun", "label": "Brandeis · Sun", "tab": "Brandeis / Sunday", "day_rx": r"\bSun\b"},
    {"key": "Clinics", "label": "Sunday Clinics", "tab": "Clinics"},
]
VENUE_LABEL = {v["key"]: v["label"] for v in VENUES}

# Before per-person prefs existed: LaGuardia (any level) + Beacon Advanced Intermediate -> NYU_NTFY_TOPIC
LEGACY_RULES = [{"venue": "LGA", "levels": []}, {"venue": "Beacon", "levels": ["Advanced Intermediate"]}]


def select_tab_exact(page: Page, text: str) -> bool:
    want = " ".join(text.split()).lower()
    for css in ("a", "button, [onclick]", "li"):
        loc = page.locator(css)
        for i in range(min(loc.count(), 300)):
            el = loc.nth(i)
            try:
                if " ".join(el.inner_text().split()).lower() == want and el.is_visible():
                    el.click()
                    _settle(page)
                    return True
            except Exception:
                pass
    return select_tab(page, text.split("/")[0].strip())


def row_sig(rows: list[dict]) -> tuple:
    return tuple(r["text"] for r in rows)


def scan_all(page: Page, url: str) -> dict:
    """{venue_key: rows} for every tab; None for a tab that failed to load or didn't switch."""
    out: dict = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        default_sig = row_sig(read_rows(page))  # page opens on LaGuardia
    except Exception as e:
        log(f"page load failed ({type(e).__name__})")
        return {v["key"]: None for v in VENUES}
    for v in VENUES:
        key = v["key"]
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            clicked = select_tab_exact(page, v["tab"])
            rows = read_rows(page)
        except Exception as e:
            log(f"{key}: load failed ({type(e).__name__})")
            out[key] = None
            continue
        if not clicked or (key != "LGA" and rows and row_sig(rows) == default_sig):
            log(f"{key}: tab '{v['tab']}' didn't switch; skipping this run")
            out[key] = None
            continue
        if v.get("gym_rx"):
            rows = [r for r in rows if re.search(v["gym_rx"], r["text"], re.I)]
        if v.get("day_rx"):
            rows = [r for r in rows if re.search(v["day_rx"], r["text"])]
        out[key] = rows
    return out


# ---------- per-person preferences (Vercel app) ----------

PREFS_AUDIENCE = "nyurban-prefs"


def oidc_token() -> str | None:
    url, tok = env("ACTIONS_ID_TOKEN_REQUEST_URL"), env("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not (url and tok):
        return None
    req = urllib.request.Request(f"{url}&audience={PREFS_AUDIENCE}", headers={"Authorization": f"bearer {tok}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["value"]


def prefs_api(method: str, body: dict | None = None):
    base = env("NYU_PREFS_URL").rstrip("/")
    if not base:
        return None
    jwt = oidc_token()
    if not jwt:
        return None
    req = urllib.request.Request(
        f"{base}/api/bot", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "User-Agent": "nyurban-bot"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def load_users() -> tuple[list[dict], str]:
    if env("NYU_USERS_FILE"):  # tests
        return json.loads(Path(env("NYU_USERS_FILE")).read_text()), "file"
    try:
        data = prefs_api("GET")
        if data is not None:
            return data.get("users", []), "prefs"
    except Exception as e:
        log(f"prefs fetch failed ({type(e).__name__}: {str(e)[:80]}); using legacy topic")
    topic = env("NYU_NTFY_TOPIC")
    legacy = [{"id": "legacy", "name": "legacy", "topic": topic, "paused": False, "rules": LEGACY_RULES, "snooze": []}] if topic else []
    return legacy, "legacy"


def norm_date(d: str) -> str:
    return "/".join(str(int(x)) for x in d.split("/"))


def user_matches(u: dict, venue: str, row: dict) -> bool:
    if u.get("paused") or not u.get("topic"):
        return False
    if norm_date(row["date"]) in {norm_date(d) for d in u.get("snooze", [])}:
        return False
    for rule in u.get("rules", []):
        if rule.get("venue") != venue:
            continue
        # A venue's own level list overrides the person's global level filter;
        # an empty list at both levels means any level.
        levels = [l.strip().lower() for l in (rule.get("levels") or u.get("levels") or [])]
        if not levels or row["level"].strip().lower() in levels:
            return True
    return False


def build_catalog(scans: dict) -> dict:
    cat = {}
    for v in VENUES:
        rows = scans.get(v["key"]) or []
        cat[v["key"]] = {
            "label": v["label"],
            "levels": sorted({r["level"] for r in rows if r["level"]}),
            "dates": sorted({r["date"] for r in rows}, key=lambda d: iso_date(d)),
        }
    return cat


def alert_line(venue: str, r: dict) -> str:
    court = f" · Court {r['court']}" if r["court"] != "?" else ""
    level = f" {r['level']}" if r["level"] else ""
    start = f" {r['start']}" if r.get("start") else ""
    return f"{VENUE_LABEL.get(venue, venue)} {r['date']}{start}{level}{court}: {fmt_count(r['count'])} open"


KEY_VERSION = 3  # bump when the row key format changes; that run re-baselines silently


def row_key(venue: str, r: dict) -> str:
    return f"{venue} {r['date']} {r.get('start', '')} {r['level']} C{r['court']}"


# ---------- state & sightings ----------

def load_state() -> dict:
    s = json.loads(STATE_FILE.read_text() or "{}") if STATE_FILE.exists() else {}
    s.setdefault("purchases", {})
    s.setdefault("last_seen", {})
    return s


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def sighting(venue: str, session: str, court: str, status: str, action: str = "") -> None:
    new = not SIGHTINGS_FILE.exists() or SIGHTINGS_FILE.stat().st_size == 0
    with SIGHTINGS_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["seen_at", "venue", "session", "court", "status", "action"])
        w.writerow([now().strftime("%Y-%m-%d %H:%M ET"), venue, session, court, status, action])


def set_purchase(state: dict, date_iso: str, status: str, **extra) -> None:
    state["purchases"][date_iso] = {"status": status, "at": now().isoformat(timespec="seconds"), **extra}
    save_state(state)  # persist immediately so a crash mid-pay still blocks retries


# ---------- page parsing ----------

def read_rows(page: Page, level_rx: re.Pattern | None = None, gym_rx: re.Pattern | None = None) -> list[dict]:
    rows = []
    trs = page.locator("tr")
    for i in range(trs.count()):
        tr = trs.nth(i)
        text = " ".join(tr.inner_text().split())
        dm = re.search(r"\b(\d{1,2}/\d{1,2})\b", text)
        if not dm:
            continue
        if level_rx and not level_rx.search(text):
            continue
        if gym_rx and not gym_rx.search(text):
            continue
        cm = re.search(r"Court\s*(\d+)", text, re.I)
        box = tr.locator("input[type=checkbox], input[type=radio]")
        tds = tr.locator("td")
        level, gym, start = parse_level_gym([" ".join(tds.nth(j).inner_text().split()) for j in range(tds.count())])
        last_cell = " ".join(tds.last.inner_text().split()) if tds.count() else ""
        nm = re.search(r"\b(\d+)\b", last_cell) if "/" not in last_cell else None
        sold_out = "sold out" in text.lower()
        enabled = box.count() > 0 and box.first.is_enabled()
        is_open = (not sold_out) and enabled
        # site shows the exact number when <= 5 spots are left, "Yes" when more than 5
        if is_open and re.search(r"\byes\b", last_cell, re.I):
            count = 6
        else:
            count = int(nm.group(1)) if (is_open and nm) else None
        rows.append({
            "date": dm.group(1),
            "court": cm.group(1) if cm else "?",
            "level": level,
            "gym": gym,
            "start": start,
            "text": text,
            "open": is_open,
            "count": count,  # None = site didn't show a number
            "status": "sold_out" if sold_out else (f"open:{fmt_count(count)}" if enabled else "unavailable"),
            "row": tr,
        })
    return rows


def parse_level_gym(cells: list[str]) -> tuple[str, str, str]:
    """Cells look like [select, 'Fri 11/20', ('LaGuardia H.S.'), 'Intermediate - Court 1', '7:00 pm - 10:30 pm', fee, avail].
    Level = cell before the time, minus ' - Court N'; gym = cell between date and level, if any."""
    di = next((i for i, c in enumerate(cells) if re.search(r"\b\d{1,2}/\d{1,2}\b", c)), None)
    ti = next((i for i, c in enumerate(cells) if re.search(r"\d\s*(am|pm)\b", c, re.I)), None)
    if di is None or ti is None or ti - 1 <= di:
        return "", "", ""
    level = re.sub(r"\s*[-–]?\s*Court\s*\d+\s*$", "", cells[ti - 1], flags=re.I).strip(" -")
    gym = cells[ti - 2] if ti - 2 > di else ""
    m = re.search(r"\d{1,2}(:\d{2})?\s*(am|pm)", cells[ti], re.I)
    start = re.sub(r":00\s*", "", m.group(0)).replace(" ", "").lower() if m else ""
    return level, gym, start


def fmt_count(c: int | None) -> str:
    return "?" if c is None else ("6+" if c >= 6 else str(c))


def wanted(mmdd: str, targets: str) -> bool:
    if targets == "any":
        return True
    if targets == "today":
        return iso_date(mmdd) == now().strftime("%Y-%m-%d")
    norm = lambda s: "/".join(str(int(x)) for x in s.split("/"))
    return norm(mmdd) in {norm(t) for t in targets.split()}


def improved(old: str | None, new: str) -> bool:
    """True if this row just opened or its open count went up."""
    if not new.startswith("open"):
        return False
    if not old or not old.startswith("open"):
        return True
    num = lambda x: 6 if x == "6+" else (int(x) if x.isdigit() else None)
    o, n = num(old.split(":")[1]), num(new.split(":")[1])
    return o is not None and n is not None and n > o


# ---------- form helpers (buy mode) ----------

def first_visible(page_or_frame, selector: str):
    loc = page_or_frame.locator(selector)
    for i in range(loc.count()):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


def innermost(page: Page, pattern: re.Pattern, inner_css: str, max_text: int = 120):
    """Smallest container whose text matches `pattern` and holds exactly one visible
    `inner_css` element. Avoids grabbing layout wrappers that contain the whole form."""
    best, best_len = None, 10**9
    cands = page.locator("tr, div, p, li, label, td", has_text=pattern)
    for i in range(min(cands.count(), 200)):
        c = cands.nth(i)
        try:
            text = " ".join(c.inner_text().split())
        except Exception:
            continue
        if not pattern.search(text) or len(text) > max_text:
            continue
        inner = [c.locator(inner_css).nth(j) for j in range(c.locator(inner_css).count())]
        inner = [x for x in inner if x.is_visible()]
        if len(inner) == 1 and len(text) < best_len:
            best, best_len = inner[0], len(text)
    return best


def fill_by_label(page: Page, rx: str, value: str, fallback_css: str) -> bool:
    pattern = re.compile(rx, re.I)
    for loc in (page.get_by_label(pattern), page.get_by_placeholder(pattern)):
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                loc.nth(i).fill(value)
                return True
    el = innermost(page, pattern, "input[type=text], input[type=email], input:not([type])") \
        or first_visible(page, fallback_css)
    if el:
        el.fill(value)
        return True
    return False


def login(page: Page) -> None:
    user, pw = env("NYU_USERNAME"), env("NYU_PASSWORD")
    if not (user and pw):
        return
    u = first_visible(page, "input[name*='user' i], input[id*='user' i], input[name*='login' i]")
    p = first_visible(page, "input[type=password]")
    if not (u and p):
        log("login form not found — continuing as guest")
        return
    u.fill(user)
    p.fill(pw)
    p.press("Enter")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    log("login submitted")


def tick(page: Page, box) -> None:
    """Check a checkbox the site has restyled (the real <input> sits under a styled label,
    so a plain .check() times out waiting for it to be clickable)."""
    if box.is_checked():
        return
    try:
        box.check(timeout=3000)
    except Exception:
        pass
    if not box.is_checked():
        bid = box.get_attribute("id")
        lbl = page.locator(f"label[for='{bid}']") if bid else None
        if lbl is not None and lbl.count():
            try:
                lbl.first.click(timeout=3000)
            except Exception:
                pass
    if not box.is_checked():
        box.evaluate("e => e.click()")  # real click semantics: toggles + fires the site's handlers
    if not box.is_checked():
        raise RuntimeError(f"couldn't check box id={box.get_attribute('id')}")


def fill_named(page: Page, name: str, value: str) -> bool:
    el = page.locator(f"input[name='{name}']")
    if el.count():
        el.first.fill(value)
        return True
    return False


def fill_booking_form(page: Page, row) -> None:
    tick(page, row.locator("input[type=checkbox], input[type=radio]").first)
    first, last, email = env("NYU_FIRST"), env("NYU_LAST"), env("NYU_EMAIL")
    # real field names from the live site (probe 2026-11-14); label heuristics as fallback
    missing = [n for n, ok in (
        ("first", fill_named(page, "f_first_name", first) or fill_by_label(page, r"first\s*name", first, "input[name*='first' i]")),
        ("last", fill_named(page, "f_last_name", last) or fill_by_label(page, r"last\s*name", last, "input[name*='last' i]")),
        ("email", fill_named(page, "f_email", email) or fill_by_label(page, r"^\s*email", email, "input[type=email], input[name*='email' i]")),
        ("confirm_email", fill_named(page, "f_cemail", email) or fill_by_label(page, r"confirm\s*email", email, "input[name*='confirm' i]")),
    ) if not ok]
    if missing:
        raise RuntimeError(f"booking fields not found: {missing}")
    waiver = page.locator("input[name='I_agree']")
    waiver = waiver.first if waiver.count() else innermost(page, re.compile(r"agree|waiver", re.I), "input[type=checkbox]", max_text=300)
    if not waiver:
        raise RuntimeError("waiver checkbox not found")
    tick(page, waiver)
    sel = page.locator("input[name='f_GameID']:checked").count()
    log(f"form: {sel} session(s) selected, waiver checked")


def click_checkout(page: Page) -> None:
    btn = first_visible(page,
        "input[type=submit][value*='checkout' i], input[type=button][value*='checkout' i], "
        "button:has-text('Checkout'), a:has-text('Checkout'), input[type=image][alt*='checkout' i], "
        "[onclick*='checkout' i]")
    if not btn:
        raise RuntimeError("Checkout button not found")
    btn.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass


def _fill_in_frames(page: Page, selector: str, value: str) -> bool:
    for frame in page.frames:  # card fields are often in a processor iframe
        try:
            el = first_visible(frame, selector)
        except Exception:
            continue
        if not el:
            continue
        if el.evaluate("e => e.tagName").lower() == "select":
            # match existing options; select_option on a missing one would wait 30s
            opts = el.evaluate("e => [...e.options].map(o => [o.value, o.text.trim()])")
            wants = {value, value.lstrip("0"), f"20{value}" if len(value) == 2 else value}
            hit = next((v for v, t in opts if v in wants or t in wants), None)
            if hit is None:
                return False
            el.select_option(value=hit)
            return True
        el.fill(value)
        return True
    return False


def fill_card(page: Page) -> None:
    num, exp, cvv, zip_ = env("NYU_CARD_NUMBER"), env("NYU_CARD_EXP"), env("NYU_CARD_CVV"), env("NYU_CARD_ZIP")
    if not (num and exp and cvv):
        raise RuntimeError("card secrets not set")
    mm, yy = exp.replace(" ", "").split("/")
    yy = yy[-2:]
    ok_num = _fill_in_frames(page,
        "input[autocomplete='cc-number'], input[name*='cardnum' i], input[name*='card_num' i], "
        "input[name*='ccnum' i], input[id*='cardnumber' i], input[name='x_card_num'], "
        "input[placeholder*='card number' i], input[name*='card' i][name*='number' i]", num)
    ok_exp = _fill_in_frames(page,
        "input[autocomplete='cc-exp'], input[placeholder*='MM/YY' i], input[placeholder*='MM / YY' i], "
        "input[name='x_exp_date']", f"{mm}/{yy}")
    if not ok_exp:
        ok_exp = (_fill_in_frames(page, "select[name*='month' i], input[name*='month' i], select[id*='month' i], [autocomplete='cc-exp-month']", mm)
                  and _fill_in_frames(page, "select[name*='year' i], input[name*='year' i], select[id*='year' i], [autocomplete='cc-exp-year']", yy))
    ok_cvv = _fill_in_frames(page,
        "input[autocomplete='cc-csc'], input[name*='cvv' i], input[name*='cvc' i], input[name*='cid' i], "
        "input[name*='security' i], input[name='x_card_code'], input[placeholder*='CVV' i], input[placeholder*='CVC' i]", cvv)
    if zip_:
        _fill_in_frames(page, "input[autocomplete='postal-code'], input[name*='zip' i], input[name*='postal' i]", zip_)
    missing = [n for n, ok in (("number", ok_num), ("exp", ok_exp), ("cvv", ok_cvv)) if not ok]
    if missing:
        raise RuntimeError(f"card fields not found: {missing}")


def find_pay_button(page: Page):
    for frame in page.frames:
        try:
            el = first_visible(frame,
                "button:has-text('Pay'), input[type=submit][value*='pay' i], button:has-text('Submit'), "
                "input[type=submit][value*='submit' i], input[type=submit][value*='purchase' i], "
                "button:has-text('Purchase'), button:has-text('Place Order'), button[type=submit], input[type=submit]")
        except Exception:
            continue
        if el:
            return el
    return None


def buy(page: Page, w: dict, target: dict, state: dict) -> int:
    """Single-ticket purchase of `target`. Only used when NYU_MODE=buy."""
    dry = env("DRY_RUN") == "1"
    venue = w["venue"]
    date_iso = iso_date(target["date"])
    if state["purchases"].get(date_iso, {}).get("status") in BLOCKING:
        log(f"buy: {date_iso} already {state['purchases'][date_iso]['status']}")
        return 0
    try:
        if env("NYU_USERNAME"):
            login(page)
            page.goto(w["url"], wait_until="domcontentloaded", timeout=30000)
            select_tab(page, w["tab"])
            target = next(r for r in read_rows(page, w["level_rx"], w["gym_rx"])
                          if r["date"] == target["date"] and r["court"] == target["court"])
            if not target["open"]:
                raise RuntimeError("spot gone after login")
        fill_booking_form(page, target["row"])
        shot(page, "form-filled")
        click_checkout(page)
        shot(page, "after-checkout")
        fill_card(page)
        shot(page, "card-filled")
        pay = find_pay_button(page)
        if not pay:
            raise RuntimeError("Pay button not found")
        if dry:
            sighting(venue, target["date"], target["court"], target["status"], "dry_run_ok")
            notify(f"Dry run OK: {venue} {target['date']} C{target['court']} reached Pay")
            return 0

        set_purchase(state, date_iso, "pending", venue=venue, court=target["court"])
        sighting(venue, target["date"], target["court"], target["status"], "paying")
        pay.click()
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
        shot(page, "after-pay")
        body = page.inner_text("body")
        result = "failed" if DECLINE_RX.search(body) else "purchased" if SUCCESS_RX.search(body) else "unknown"
        set_purchase(state, date_iso, result, venue=venue, court=target["court"])
        sighting(venue, target["date"], target["court"], target["status"], result)
        notify({"purchased": f"Booked {venue} {target['date']} Court {target['court']}",
                "failed": f"Card declined for {target['date']}. Remove it from state.json to retry.",
                "unknown": f"Paid for {target['date']} but couldn't confirm. Check email; retries blocked."}[result])
        return 0 if result == "purchased" else 1
    except Exception as e:
        msg = str(e).split("\n")[0][:120]
        shot(page, "error")
        sighting(venue, target["date"], target["court"], target["status"], f"error: {msg}")
        notify(f"Bot error booking {venue} {target['date']}: {msg}")
        return 1



# ---------- probe (live dry run, never pays) ----------

def dump_page(page: Page, label: str) -> None:
    """Log the page's form structure (names/types only, never values) so selectors can be fixed from public logs."""
    log(f"--- {label}: {page.url.split('?')[0]}  title={page.title()!r}")
    js = """() => {
      const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      const out = [];
      document.querySelectorAll('input,select,textarea,button,iframe').forEach(e => {
        if (e.type === 'hidden') return;
        const a = n => e.getAttribute(n) || '';
        let lbl = '';
        if (e.id) { const l = document.querySelector(`label[for="${e.id}"]`); if (l) lbl = l.innerText.trim(); }
        if (!lbl) { const r = e.closest('tr,label,div'); if (r) lbl = r.innerText.trim().replace(/\\s+/g,' ').slice(0,60); }
        out.push([e.tagName.toLowerCase(), a('type'), a('name'), a('id'), a('autocomplete'), a('placeholder'),
                  e.tagName==='BUTTON' ? e.innerText.trim() : (['submit','button','image'].includes(e.type) ? a('value') || a('alt') : ''),
                  e.tagName==='IFRAME' ? (a('src').split('?')[0]) : '', vis(e) ? 'vis' : 'hid', lbl].join(' | '));
      });
      return out.slice(0, 120);
    }"""
    for frame in page.frames:
        try:
            items = frame.evaluate(js)
        except Exception:
            continue
        if items:
            log(f"  frame {frame.url.split('?')[0]}  ({len(items)} controls)  tag|type|name|id|autocomplete|placeholder|button|iframe|vis|label")
            for it in items:
                print("    " + it, flush=True)


def log_popups(page: Page) -> None:
    """Log the text of any visible modal/popup (e.g. the one with an 'Ok' button after Checkout)."""
    texts = page.evaluate("""() => {
      const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      const out = new Set();
      document.querySelectorAll('input[type=button],button').forEach(b => {
        const label = (b.value || b.innerText || '').trim();
        if (!vis(b) || !/^(ok|close|continue|yes|no|cancel)$/i.test(label)) return;
        let c = b.parentElement;
        for (let i = 0; i < 6 && c && (c.innerText||'').trim().length < 15; i++) c = c.parentElement;
        if (c) out.add(`[${label}] ` + c.innerText.trim().replace(/\\s+/g, ' ').slice(0, 400));
      });
      document.querySelectorAll('.modal,.popup,.dialog,[role=dialog],.ui-dialog,.swal2-popup,.alert').forEach(m => {
        if (vis(m)) out.add('[modal] ' + m.innerText.trim().replace(/\\s+/g, ' ').slice(0, 400));
      });
      return [...out];
    }""")
    log(f"popups: {texts or 'none'}")


def probe(page: Page) -> int:
    """Live dry run: pick a venue tab, take the first open session, fill the form with
    placeholder info, click Checkout, and dump the next page. Never clicks Pay."""
    url = env("NYU_LGA_URL") or DEFAULT_LGA_URL
    venue = env("NYU_PROBE_VENUE") or "Brandeis"
    # 1. what alert mode sees at every venue tab
    scans = scan_all(page, url)
    for key, rows in scans.items():
        if rows is None:
            log(f"[venue {key}] FAILED to read")
            continue
        log(f"[venue {key}] {len(rows)} sessions; levels={sorted({r['level'] for r in rows})}")
        for r in rows:
            print(f"    {r['date']} {r['start']} | level={r['level']!r} gym={r['gym']!r} court={r['court']} | {r['status']}", flush=True)
    log(f"catalog: {json.dumps(build_catalog(scans))[:600]}")
    if env("NYU_PREFS_URL"):
        try:
            users, source = load_users()
            log(f"prefs: source={source} people={len(users)}")
        except Exception as e:
            log(f"prefs: error {type(e).__name__}")
    if env("NYU_PROBE_CHECKOUT") != "1":
        return 0
    # 2. checkout dry run
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    dump_page(page, "signup page (before tab)")
    tabs = page.evaluate("""() => [...document.querySelectorAll('a,button,li,option')]
        .map(e => (e.innerText||'').trim().replace(/\\s+/g,' ')).filter(t => /laguardia|beacon|brandeis|w50|clinic/i.test(t) && t.length < 60)""")
    log(f"venue-like controls: {tabs}")
    clicked = select_tab(page, venue)
    log(f"select_tab({venue!r}) -> {clicked}; url now {page.url}")
    rows = read_rows(page, gym_rx=re.compile(re.escape(venue), re.I))
    if not rows:
        log("no rows for that venue after tab click; all rows on page:")
        rows = read_rows(page)
    for r in rows:
        print("    " + " ".join(r["row"].inner_text().split()), flush=True)
    target = next((r for r in rows if r["open"]), None)
    if not target:
        log("nothing open to probe the checkout with")
        return 0
    log(f"probing with {target['date']} C{target['court']} ({target['status']})")
    for k, v in (("NYU_FIRST", "Test"), ("NYU_LAST", "Probe"), ("NYU_EMAIL", "probe@example.com")):
        if not env(k):  # workflow passes missing secrets as ""
            os.environ[k] = v
    page.on("dialog", lambda d: (log(f"JS {d.type} dialog: {d.message[:200]!r}"), d.accept()))
    try:
        fill_booking_form(page, target["row"])
        log("booking form filled OK")
        click_checkout(page)
        log("clicked Checkout")
    except Exception as e:
        log(f"FAILED before/at Checkout: {str(e).splitlines()[0][:150]}")
        dump_page(page, "page at failure")
        return 1
    page.wait_for_timeout(2000)
    log_popups(page)
    dump_page(page, "page after Checkout")
    for k, v in (("NYU_CARD_NUMBER", "4111111111111111"), ("NYU_CARD_EXP", "04/28"), ("NYU_CARD_CVV", "123")):
        if not env(k):
            os.environ[k] = v
    try:
        fill_card(page)
        log("card fields found and filled (dummy card)")
    except Exception as e:
        log(f"card step: {str(e).splitlines()[0][:150]}")
    pay = find_pay_button(page)
    label = (pay.get_attribute("value") or pay.inner_text() or "").strip() if pay else ""
    if pay and re.search(r"checkout|login", label, re.I):
        pay = None
    log(f"pay button: {'FOUND ' + repr(label) + ' (not clicked)' if pay else 'NOT FOUND (still on signup page?)'}")
    return 0

# ---------- main ----------

def run(args) -> int:
    mode = env("NYU_MODE") or "alert"
    url = env("NYU_LGA_URL") or env("NYU_URL") or DEFAULT_LGA_URL
    log(f"start  mode={mode}")

    if mode == "ping":
        if not env("NYU_NTFY_TOPIC"):
            log("ping: NYU_NTFY_TOPIC is not set")
            return 1
        notify("NY Urban bot is connected ✅ You'll get alerts here when courts open.")
        return 0
    if mode == "buy":
        log("buy mode was retired; the bot is alert-only")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1280, "height": 1800})
        try:
            if mode == "probe":
                return probe(page)
            return alert(page, url)
        finally:
            browser.close()


def alert(page: Page, url: str) -> int:
    state = load_state()
    rebaseline = state.get("key_version") != KEY_VERSION
    if rebaseline:
        log(f"row key format changed (v{state.get('key_version')} -> v{KEY_VERSION}); re-baselining without alerts")
    users, source = load_users()
    log(f"people: {len(users)} ({source}); active: {sum(1 for u in users if not u.get('paused'))}")

    scans = scan_all(page, url)
    new_seen: dict = {}
    per_user: dict[str, list[str]] = {}
    any_new = False

    for venue, rows in scans.items():
        if rows is None:  # keep last known state for a tab we couldn't read
            new_seen.update({k: v for k, v in state["last_seen"].items() if k.startswith(venue + " ")})
            continue
        log(f"{venue}: " + (" | ".join(f"{r['date']} {r['level'] or '-'} C{r['court']} {r['status']}" for r in rows) or "no sessions"))
        for r in rows:
            key = row_key(venue, r)
            old = state["last_seen"].get(key)
            new_seen[key] = r["status"]
            if old != r["status"]:
                sighting(venue, r["date"], f"{r['level']} C{r['court']}".strip(), r["status"],
                         "baseline" if old is None else "changed")
            if r["open"] and improved(old, r["status"]) and not rebaseline:
                any_new = True
                line = alert_line(venue, r)
                for u in users:
                    if user_matches(u, venue, r):
                        per_user.setdefault(u["id"], []).append(line)

    state["last_seen"] = new_seen
    if all(v is not None for v in scans.values()):
        state["key_version"] = KEY_VERSION
    catalog = build_catalog(scans)
    digest = json.dumps(catalog, sort_keys=True)
    if source == "prefs" and digest != state.get("catalog_digest") and all(v is not None for v in scans.values()):
        try:
            prefs_api("POST", {"venues": catalog})
            state["catalog_digest"] = digest
            log("catalog published")
        except Exception as e:
            log(f"catalog publish failed ({type(e).__name__})")
    save_state(state)

    by_id = {u["id"]: u for u in users}
    for uid, lines in per_user.items():
        notify("\n".join(lines), by_id[uid]["topic"], who=uid)
    if not per_user:
        log("new openings, but nobody's preferences match" if any_new else "no new openings")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the browser (local debugging)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
