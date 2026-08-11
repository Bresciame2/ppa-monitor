# PPA Tender Monitor

Watches the Lebanese Public Procurement Authority portal (ppa.gov.lb) and emails
you when a tender relevant to BME appears.

## How it works

Tenders on the PPA site have sequential IDs (`/en/tenders/details/12120`). Each run,
the script walks forward from the last ID it saw until it hits a run of missing IDs,
so it cannot miss a published tender the way a keyword search could.

Each new tender is parsed, then scored against two filter streams:

| Stream | Matches on | Purpose |
|---|---|---|
| **A — Entity** | Army, MoD, ISF, General Security, State Security, Civil Defence, Customs | Everything those buyers procure |
| **B — Category** | Official sector tags **plus keywords in the title** | Relevant items from any buyer |

Hit by both → **HIGH** priority. Hit by one → **NORMAL**.

**Every new tender is emailed.** The filters decide highlighting, not inclusion:
HIGH/NORMAL matches appear at the top with full details; everything else is
listed compactly under "All other new tenders" so nothing slips by unseen.

**Why the title keywords matter:** the portal's `Sector` field is frequently left
blank. Tender 12120 — riot helmets and suits from the Ministry of Defence — had no
sector at all. A sector-only filter would have missed it. The title always has content,
so it does most of the work.

Cases where the buyer is right but the item is unclear go to Claude for a yes/no call.

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install requests beautifulsoup4 lxml pyyaml
```

## Configure

1. Open `config.yaml`, set `notify_email` to your address.
2. Check `start_id`. Open the newest tender on the site, take its ID from the URL,
   and set `start_id` to roughly 20 below it. First run then picks up recent tenders.
3. Set the secrets as environment variables — **never put them in config.yaml**:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="youraddress@gmail.com"
export SMTP_PASS="your-app-password"
export ANTHROPIC_API_KEY="sk-ant-..."
```

For Gmail you need an **App Password** (Google Account → Security → 2-Step
Verification → App passwords), not your normal login password.

`ANTHROPIC_API_KEY` is optional. Without it, borderline tenders are passed through
and emailed rather than filtered out — you get more noise, never less signal.

## Test run

```bash
python3 ppa_monitor.py
```

Watch `monitor.log`. On the first run it scans forward from `start_id`, so expect
a larger batch than usual.

## Schedule it

Twice a day is plenty — tenders are announced during business hours and the
minimum advertising period is 21 days.

**cron** (Linux/macOS):
```
0 8,15 * * * cd /path/to/ppa_monitor && /path/to/venv/bin/python ppa_monitor.py >> cron.log 2>&1
```

Put the `export` lines in a small wrapper script, or use `EnvironmentFile` if you
run it under systemd — cron does not read your shell profile.

## Tuning

After a week or two, check `monitor.log`:

- **Too much noise** → add terms to `exclude_keywords`. Broad words like `بيع` (sale)
  and `حماية` (protection) are the usual culprits.
- **Missed something** → add a term to `keywords`. Arabic is normalised for alef,
  ya, and ta-marbuta variants, so one spelling is enough.
- **Note:** an entity match now overrides the exclusion list. If the Army tenders
  something whose title contains a noise word, Claude decides rather than the
  filter dropping it silently.

## Files

| File | Purpose |
|---|---|
| `ppa_monitor.py` | The agent |
| `config.yaml` | Filters, email address, scan settings |
| `state.json` | Last ID seen, already-notified IDs (auto-created) |
| `monitor.log` | Run history |

## Adding AID Italy later

The Italian AID portal blocks automated access, so it needs Playwright with a real
browser profile rather than plain `requests`. The parsing and notification halves of
this script are reusable — only `fetch()` and `parse()` would be swapped out.
