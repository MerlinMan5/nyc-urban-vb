"""
NY Urban open-play watcher / booker.

Each run (GitHub Actions every 5 min on Fridays, or locally):
  1. Load state.json — skip any target date already purchased/pending.
  2. Load the session page, log every row's status, append changes to sightings.csv.
  3. If a target session is open: select it, fill the form, Checkout,
     fill the card, mark the date "pending" in state.json, click Pay,
     then mark "purchased" (or "unknown"/"failed" — both block retries).

Never prints personal info, so public Actions logs are safe.

Env (set as GitHub Secrets):
  NYU_FIRST, NYU_LAST, NYU_EMAIL          booking form
  NYU_USERNAME, NYU_PASSWORD              optional site login
  NYU_CARD_NUMBER, NYU_CARD_EXP (MM/YY), NYU_CARD_CVV, NYU_CARD_ZIP
  NYU_NTFY_TOPIC                          optional phone push via ntfy.sh
Config:
  NYU_TARGETS   "today" (default) | "any" | "11/20 12/04"
  NYU_URL       session page (default: LaGuardia Friday)
  DRY_RUN=1     do everything except click Pay
  SCREENSHOTS=1 save step screenshots to ./shots
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
DEFAULT_URL = "https://www.nyurban.com/open-play-volleyball-no/?id=6&gametypeid=1&filter_id=1"
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
    return os.environ.get(name, default).strip()


def notify(msg: str) -> None:
    log(f"NOTIFY: {msg}")
    topic = env("NYU_NTFY_TOPIC")
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=msg.encode(),
            headers={"Title": "NY Urban bot", "Priority": "high", "Tags": "volleyball"},
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


# ---------- state & sightings ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text() or "{}")
    return {}


def save_state(state: dict) -> None:
    state.setdefault("purchases", {})
    state.setdefault("last_seen", {})
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def sighting(session: str, court: str, status: str, action: str = "") -> None:
    new = not SIGHTINGS_FILE.exists()
    with SIGHTINGS_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["seen_at", "session", "court", "status", "action"])
        w.writerow([now().strftime("%Y-%m-%d %H:%M ET"), session, court, status, action])


def set_purchase(state: dict, date_iso: str, status: str, **extra) -> None:
    state.setdefault("purchases", {})[date_iso] = {
        "status": status, "at": now().isoformat(timespec="seconds"), **extra,
    }
    save_state(state)  # persist immediately so a crash mid-pay still blocks retries


# ---------- page parsing ----------

def read_rows(page: Page) -> list[dict]:
    rows = []
    trs = page.locator("tr")
    for i in range(trs.count()):
        tr = trs.nth(i)
        text = " ".join(tr.inner_text().split())
        dm = re.search(r"\b(\d{1,2}/\d{1,2})\b", text)
        if not dm:
            continue
        cm = re.search(r"Court\s*(\d+)", text, re.I)
        box = tr.locator("input[type=checkbox], input[type=radio]")
        sold_out = "sold out" in text.lower()
        enabled = box.count() > 0 and box.first.is_enabled()
        rows.append({
            "date": dm.group(1),
            "court": cm.group(1) if cm else "?",
            "open": (not sold_out) and enabled,
            "status": "sold_out" if sold_out else ("open" if enabled else "unavailable"),
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


# ---------- form helpers ----------

def first_visible(page_or_frame, selector: str):
    loc = page_or_frame.locator(selector)
    for i in range(loc.count()):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


def innermost(page: Page, pattern: re.Pattern, inner_css: str, max_text: int = 120):
    """Smallest container (tr/div/p/li/label) whose text matches `pattern` and holds exactly
    one visible `inner_css` element. Avoids grabbing layout wrappers that contain the whole form."""
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
    # label text sits in a sibling cell on this site -> smallest row containing the label
    el = innermost(page, pattern, "input[type=text], input[type=email], input:not([type])")
    if el:
        el.fill(value)
        return True
    el = first_visible(page, fallback_css)
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
    missing = []
    if not fill_by_label(page, r"first\s*name", first, "input[name*='first' i], input[id*='first' i]"):
        missing.append("first")
    if not fill_by_label(page, r"last\s*name", last, "input[name*='last' i], input[id*='last' i]"):
        missing.append("last")
    if not fill_by_label(page, r"^\s*email", email, "input[type=email], input[name*='email' i]"):
        missing.append("email")
    if not fill_by_label(page, r"confirm\s*email", email, "input[name*='confirm' i], input[name*='email2' i]"):
        missing.append("confirm_email")
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
        if el:
            tag = el.evaluate("e => e.tagName").lower()
            if tag == "select":
                for opt in (value, value.lstrip("0"), f"20{value}" if len(value) == 2 else value):
                    try:
                        el.select_option(opt)
                        return True
                    except Exception:
                        try:
                            el.select_option(label=opt)
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


# ---------- main ----------

def run(args) -> int:
    url = env("NYU_URL") or DEFAULT_URL
    targets = env("NYU_TARGETS") or "today"
    dry = env("DRY_RUN") == "1"
    state = load_state()
    state.setdefault("purchases", {})
    state.setdefault("last_seen", {})

    log(f"start  targets={targets}  dry_run={dry}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1280, "height": 1800})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log(f"page load failed: {type(e).__name__}")
            sighting("-", "-", "error", f"load failed: {type(e).__name__}")
            save_state(state)
            return 0  # transient; don't spam red runs

        rows = read_rows(page)
        if not rows:
            log("no session rows found (layout change?)")
            sighting("-", "-", "error", "no rows parsed")
            shot(page, "no-rows")
            save_state(state)
            return 1

        summary = []
        for r in rows:
            key = f"{r['date']} C{r['court']}"
            summary.append(f"{key} {r['status']}")
            if state["last_seen"].get(key) != r["status"]:
                sighting(r["date"], r["court"], r["status"],
                         "baseline" if key not in state["last_seen"] else "changed")
                state["last_seen"][key] = r["status"]
        log(" | ".join(summary))

        candidates = [r for r in rows if r["open"] and wanted(r["date"], targets)]
        candidates = [r for r in candidates
                      if state["purchases"].get(iso_date(r["date"]), {}).get("status") not in BLOCKING]
        if not candidates:
            already = [d for d, v in state["purchases"].items() if v.get("status") in BLOCKING]
            log(f"nothing to book (blocked dates: {already or 'none'})")
            save_state(state)
            return 0

        target = candidates[0]
        date_iso = iso_date(target["date"])
        notify(f"Open spot: {target['date']} Court {target['court']} — {'DRY RUN' if dry else 'booking'}")

        try:
            if env("NYU_USERNAME"):
                login(page)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                target = next(r for r in read_rows(page)
                              if r["date"] == target["date"] and r["court"] == target["court"])
                if not target["open"]:
                    raise RuntimeError("spot gone after login")

            fill_booking_form(page, target["row"])
            shot(page, "form-filled")
            click_checkout(page)
            shot(page, "after-checkout")
            log(f"reached: {page.url.split('?')[0]}")

            fill_card(page)
            shot(page, "card-filled")
            pay = find_pay_button(page)
            if not pay:
                raise RuntimeError("Pay button not found")

            if dry:
                log("DRY RUN: stopping before Pay")
                sighting(target["date"], target["court"], "open", "dry_run_ok")
                notify(f"Dry run OK for {target['date']} C{target['court']} — reached Pay")
                save_state(state)
                return 0

            set_purchase(state, date_iso, "pending", court=target["court"])
            sighting(target["date"], target["court"], "open", "paying")
            pay.click()
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                pass
            page.wait_for_timeout(3000)
            shot(page, "after-pay")
            body = page.inner_text("body")

            if DECLINE_RX.search(body):
                set_purchase(state, date_iso, "failed", court=target["court"])
                sighting(target["date"], target["court"], "open", "declined")
                notify(f"Card declined for {target['date']}. Remove the entry in state.json to retry.")
                return 1
            if SUCCESS_RX.search(body):
                set_purchase(state, date_iso, "purchased", court=target["court"])
                sighting(target["date"], target["court"], "open", "purchased")
                notify(f"Booked {target['date']} Court {target['court']} 🏐")
                return 0
            set_purchase(state, date_iso, "unknown", court=target["court"])
            sighting(target["date"], target["court"], "open", "unknown_result")
            notify(f"Paid for {target['date']} but couldn't confirm — check your email. Retries blocked.")
            return 1

        except Exception as e:
            msg = str(e).split("\n")[0][:120]
            log(f"booking error: {msg}")
            shot(page, "error")
            sighting(target["date"], target["court"], "open", f"error: {msg}")
            notify(f"Bot hit an error booking {target['date']}: {msg}")
            save_state(state)
            return 1
        finally:
            browser.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the browser (local debugging)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
