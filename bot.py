"""
NY Urban open-play watcher (alert-only by default).

Each run:
  1. Loads every watched session page (LaGuardia; Beacon "Advanced Intermediate" only).
  2. Logs each row's status (+ spots left when the site shows a number).
  3. Appends changes to sightings.csv and pushes a phone alert when spots open
     or the count goes up. Alerts are per court, since the group of 4 needs one court.
     LaGuardia is listed first.

NYU_MODE=buy enables the old single-ticket auto-buy for the first open match.
It buys ONE ticket, so it doesn't fit a group of 4; alert mode is the default.

Never prints personal info, so public Actions logs are safe.

Env:
  NYU_NTFY_TOPIC     phone push via ntfy.sh (secret)
  NYU_TARGETS        "any" (default) | "today" | "11/20 12/04"
  NYU_GROUP_SIZE     4 (default) — alerts say whether a court has enough spots
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


def notify(msg: str, urgent: bool = False) -> None:
    log("NOTIFY: " + msg.replace("\n", " / "))
    topic = env("NYU_NTFY_TOPIC")
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=msg.encode(),
            headers={"Title": "NY Urban volleyball", "Priority": "urgent" if urgent else "high",
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
    """Ordered by priority: LaGuardia first."""
    w = [{"venue": "LGA", "url": env("NYU_LGA_URL") or env("NYU_URL") or DEFAULT_LGA_URL, "level_rx": None}]
    if env("NYU_BEACON_URL"):
        # "Advanced Intermediate" only; plain "Advanced" rows don't match
        w.append({"venue": "Beacon", "url": env("NYU_BEACON_URL"),
                  "level_rx": re.compile(r"advanced\s*[-/]?\s*intermediate", re.I)})
    return w


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

def read_rows(page: Page, level_rx: re.Pattern | None = None) -> list[dict]:
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
        cm = re.search(r"Court\s*(\d+)", text, re.I)
        box = tr.locator("input[type=checkbox], input[type=radio]")
        tds = tr.locator("td")
        last_cell = " ".join(tds.last.inner_text().split()) if tds.count() else ""
        nm = re.search(r"\b(\d+)\b", last_cell) if "/" not in last_cell else None
        sold_out = "sold out" in text.lower()
        enabled = box.count() > 0 and box.first.is_enabled()
        is_open = (not sold_out) and enabled
        count = int(nm.group(1)) if (is_open and nm) else None
        rows.append({
            "date": dm.group(1),
            "court": cm.group(1) if cm else "?",
            "open": is_open,
            "count": count,  # None = site didn't show a number
            "status": "sold_out" if sold_out else (f"open:{count if count is not None else '?'}" if enabled else "unavailable"),
            "row": tr,
        })
    return rows


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
    o, n = old.split(":")[1], new.split(":")[1]
    return o.isdigit() and n.isdigit() and int(n) > int(o)


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


def fill_booking_form(page: Page, row) -> None:
    row.locator("input[type=checkbox], input[type=radio]").first.check()
    first, last, email = env("NYU_FIRST"), env("NYU_LAST"), env("NYU_EMAIL")
    missing = [n for n, ok in (
        ("first", fill_by_label(page, r"first\s*name", first, "input[name*='first' i]")),
        ("last", fill_by_label(page, r"last\s*name", last, "input[name*='last' i]")),
        ("email", fill_by_label(page, r"^\s*email", email, "input[type=email], input[name*='email' i]")),
        ("confirm_email", fill_by_label(page, r"confirm\s*email", email, "input[name*='confirm' i]")),
    ) if not ok]
    if missing:
        raise RuntimeError(f"booking fields not found: {missing}")
    waiver = innermost(page, re.compile(r"agree|waiver", re.I), "input[type=checkbox]", max_text=300)
    if not waiver:
        raise RuntimeError("waiver checkbox not found")
    waiver.check()


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
            for opt in (value, value.lstrip("0"), f"20{value}" if len(value) == 2 else value):
                for kw in ({"value": opt}, {"label": opt}):
                    try:
                        el.select_option(**kw)
                        return True
                    except Exception:
                        pass
            return False
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


def buy(page: Page, url: str, venue: str, target: dict, level_rx, state: dict) -> int:
    """Single-ticket purchase of `target`. Only used when NYU_MODE=buy."""
    dry = env("DRY_RUN") == "1"
    date_iso = iso_date(target["date"])
    if state["purchases"].get(date_iso, {}).get("status") in BLOCKING:
        log(f"buy: {date_iso} already {state['purchases'][date_iso]['status']}")
        return 0
    try:
        if env("NYU_USERNAME"):
            login(page)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            target = next(r for r in read_rows(page, level_rx)
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


# ---------- main ----------

def run(args) -> int:
    targets = env("NYU_TARGETS") or "any"
    group = int(env("NYU_GROUP_SIZE") or "4")
    mode = env("NYU_MODE") or "alert"
    state = load_state()
    log(f"start  mode={mode}  targets={targets}  group={group}")

    alerts: list[str] = []
    enough = False
    rc = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1280, "height": 1800})
        try:
            for w in watches():
                venue = w["venue"]
                try:
                    page.goto(w["url"], wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    log(f"{venue}: load failed ({type(e).__name__})")
                    sighting(venue, "-", "-", "error", f"load failed: {type(e).__name__}")
                    continue
                rows = read_rows(page, w["level_rx"])
                if not rows:
                    log(f"{venue}: no matching rows (layout change, or no sessions at that level)")
                    continue

                log(f"{venue}: " + " | ".join(f"{r['date']} C{r['court']} {r['status']}" for r in rows))

                for r in rows:
                    key = f"{venue} {r['date']} C{r['court']}"
                    old = state["last_seen"].get(key)
                    if old != r["status"]:
                        sighting(venue, r["date"], r["court"], r["status"], "baseline" if old is None else "changed")
                        state["last_seen"][key] = r["status"]
                    if r["open"] and wanted(r["date"], targets) and improved(old, r["status"]):
                        if r["count"] is None:
                            alerts.append(f"{venue} {r['date']} Court {r['court']}: open (count not shown)")
                        else:
                            ok = r["count"] >= group
                            enough |= ok
                            alerts.append(f"{venue} {r['date']} Court {r['court']}: {r['count']} open"
                                          + (f" ✅ enough for {group}" if ok else f" (need {group})"))

                if mode == "buy" and rc == 0:
                    cand = next((r for r in rows if r["open"] and wanted(r["date"], targets)), None)
                    if cand:
                        rc = buy(page, w["url"], venue, cand, w["level_rx"], state)
                        mode = "done"  # at most one purchase per run
        finally:
            browser.close()
            save_state(state)

    if alerts:
        notify("\n".join(alerts), urgent=enough)
    else:
        log("no new openings")
    return rc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the browser (local debugging)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
