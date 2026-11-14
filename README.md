# nyc-urban-vb

Watches NY Urban open-play volleyball for openings and sends a phone alert. It does **not** buy anything by default.

Watched sessions, in priority order:
1. **LaGuardia H.S.**: any session
2. **Beacon**: **Advanced Intermediate** only (plain "Advanced" is ignored)

Runs on GitHub Actions every 5 min, 9am–9pm ET, every day.

## Alerts

One push per run, and only when something new opens or a count goes up. Each court is listed separately because the group of 4 needs one court:

```
LGA 11/20 Court 2: 1 open (need 4)
LGA 12/04 Court 2: 4 open ✅ enough for 4
Beacon 11/20 Court 2: 2 open (need 4)
```

The alert is marked urgent when a single court has at least 4 open. The site shows the exact count when 5 or fewer spots are left, and "Yes" when there are more. The bot reports "Yes" as `6+`.

Every status change is appended to `sightings.csv` (venue, session, court, status). That's the history of when spots open.

## Setup

1. Install the **ntfy** app, subscribe to a random topic name, and add it as Secret `NYU_NTFY_TOPIC`.

Venues are tabs on the same signup page, so the bot clicks "Laguardia" and "Beacon" and keeps only rows whose Gym column matches.

Optional Variables:

| Variable | Default | Meaning |
|---|---|---|
| `NYU_TARGETS` | `any` | `any`, `today`, or `11/20 12/04` |
| `NYU_GROUP_SIZE` | `4` | spots needed on one court for the ✅ / urgent alert |
| `NYU_LGA_URL` | LaGuardia Friday page | signup page URL |
| `NYU_BEACON_URL` | same page | only if Beacon turns out to have its own URL |
| `NYU_MODE` | `alert` | `buy` = old single-ticket auto-buy (not suited to a group of 4) |

Buy mode also needs Secrets `NYU_FIRST`, `NYU_LAST`, `NYU_EMAIL`, `NYU_CARD_NUMBER`, `NYU_CARD_EXP` (MM/YY), `NYU_CARD_CVV`, `NYU_CARD_ZIP`, and optionally `NYU_USERNAME`/`NYU_PASSWORD`. It buys at most one ticket per date (tracked in `state.json`).

## Testing

- Offline: `./tests/run_mock.sh`. It runs against a local mock of the site: alerts, the Beacon level filter, no repeat alerts, and a buy-mode dry run.
- Live observe: Actions → watch → Run workflow, mode `alert`. The log prints every row it saw at each venue.
- **Live probe (dry run of checkout):** Run workflow with mode `probe` and `probe_venue` set to a venue that has open spots (default `Brandeis`). It clicks the tab, takes the first open session, fills the form with placeholder info (Test Probe / probe@example.com), clicks **Checkout**, fills a dummy card, finds the Pay button, and **stops without paying**. It logs the structure of every page (field names and types, never values), so any broken selector can be fixed straight from the log. Probe runs write nothing back to the repo.

## Notes

- Public repo: logs and `sightings.csv` are public. The bot never prints personal info.
- DST: the cron is in UTC. When daylight saving starts in March, change `14-23,0-1` → `13-23,0`.
- Local: `pip install -r requirements.txt && playwright install chromium && python bot.py --headed`
