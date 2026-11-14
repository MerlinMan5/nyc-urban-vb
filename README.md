# nyc-urban-vb

Watches NY Urban open-play volleyball (LaGuardia H.S., Fridays) for refund spots and books one automatically.

Runs on GitHub Actions every 5 min, **Fridays 9am–5pm ET**. Refunds are allowed until 3pm on session day, so that's when spots open.

## How it works

1. Skips any date already in `state.json` as `pending` / `purchased` / `unknown` / `failed`.
2. Loads the session page and logs each row. Status changes are appended to `sightings.csv`.
3. If a target session is open: select it → fill name/email → waiver → Checkout → card → mark `pending` → Pay → mark `purchased`.
4. Commits `state.json` + `sightings.csv` back to the repo.

At most one booking per run. Runs never overlap (`concurrency`). The prepaid/virtual card limit is the final backstop.

## Setup

**Secrets** (Settings → Secrets and variables → Actions → Secrets):

| Secret | Example |
|---|---|
| `NYU_FIRST`, `NYU_LAST`, `NYU_EMAIL` | booking form |
| `NYU_CARD_NUMBER` | `4111...` (use a capped virtual/prepaid card) |
| `NYU_CARD_EXP` | `04/27` |
| `NYU_CARD_CVV` | `123` |
| `NYU_CARD_ZIP` | `10024` |
| `NYU_USERNAME`, `NYU_PASSWORD` | optional, only if checkout needs login |
| `NYU_NTFY_TOPIC` | optional: random string; subscribe to it in the ntfy app for phone alerts |

**Variables** (same page → Variables), optional:

| Variable | Default | Meaning |
|---|---|---|
| `NYU_TARGETS` | `today` | `today` = that Friday's session, `any`, or `11/20 12/04` |
| `NYU_URL` | LaGuardia Friday page | another session page |

## Testing

- **Offline:** `./tests/run_mock.sh` runs dry run → buy → re-run (must skip) against a local mock of the site.
- **Live dry run:** Actions → watch → Run workflow, `dry_run` checked, `targets: any`, and `url` set to a page with open spots. It goes all the way to the Pay button and stops.
- **Full test:** same with `dry_run` unchecked on a session you don't want, then email NY Urban before 3pm to cancel for a refund.

## Re-arming a date

If a run ends `failed` (declined) or `unknown` (paid but couldn't confirm), that date is blocked. Check your email, then delete the entry from `state.json` to let the bot try again.

## Notes

- Public repo: logs, `state.json` and `sightings.csv` are public. The bot never prints personal info. Leave screenshot upload off unless debugging.
- DST: the cron is UTC. When daylight saving starts in March, change `14-21` → `13-20`.
- Local debugging: `pip install -r requirements.txt && playwright install chromium && python bot.py --headed`
