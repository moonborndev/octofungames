# app-ads-watch

Checks that `app-ads.txt` at the repo root (served at https://octofungames.com/app-ads.txt) contains every
line our ad networks require, and reports what is missing. It never removes or edits existing lines, and it
never commits.

## Two layers

1. **GitHub Actions, weekly (public checks).** Runs `watch.py report` in this repo: fetches the public
   network lists (today only Mintegral), reads the snapshot files committed under `snapshots/`, compares the
   repo file with the live site and flags format problems and duplicates. The schedule lives in the
   workflow's `cron` line (the workflow is added later); change the frequency there, not here.
2. **Mac weekly task (dashboards).** Most networks publish their list only behind a login (AppLovin, AdMob,
   Meta, Unity, DT Exchange, InMobi, Pangle, Liftoff). A scheduled task on the Mac opens each
   `dashboard_url` from `sources.json` in Chrome, waits `page_wait_seconds` for the page (and
   `error_wait_seconds` before giving up on an error), copies the list and saves it as
   `snapshots/<network key>.txt`. `watch.py` only reads those files; the wait values are data for that task.
   The Mac task also passes `--sdk-registry`, because the SDK repo is private.

## Running

```sh
# Report to stdout (default command)
python3 tools/app-ads-watch/watch.py

# Report to files, as the weekly runs do
python3 tools/app-ads-watch/watch.py report \
  --out tools/app-ads-watch/reports/2026-11-18.md \
  --json tools/app-ads-watch/reports/2026-11-18.json \
  --sdk-registry <path to SDK>/packages/com.octofun.sdk.core/Editor/SDKProviderRegistry.cs

# Skip the repo vs live site comparison
python3 tools/app-ads-watch/watch.py report --no-live

# Add the missing lines of some networks (or --all) to app-ads.txt
python3 tools/app-ads-watch/watch.py apply --networks applovin,mintegral
```

`--file <path>` points either command at another copy of app-ads.txt (useful for testing `apply`).

The report exits 0 even when it has findings; a non-zero exit means the script itself crashed. A network
whose list cannot be fetched is listed under "Errors" and the run continues.

### What the report contains

- Per network: source type, status (`ok`/`error` for public lists; `fresh`/`stale`/`missing` for snapshots),
  number of required lines, lines to add, mismatches.
- "Login needed": snapshot networks whose snapshot is missing or older than `snapshot_stale_days` (8), with
  their dashboard URL.
- "To add": the exact lines `apply` would add.
- Mismatches (report only): we have the same domain + id but a different relationship type, or the network
  gives a cert id that none of our lines for that domain + id + type carries. These are never added
  automatically.
- Duplicates in our file: same domain + id + type, either `case-only` (e.g. `B8N9YH` vs `b8n9yh`) or
  `cert-variant` (same line with a different or missing cert id).
- Format errors (lines not in `domain, id, TYPE[, certId]` form) and sort-order errors.
- Repo vs live site, and the SDK registry check (mediation ids in `SDKProviderRegistry.cs` vs
  `sdk_registry_mediation_map`).

The JSON file holds the same data in machine-readable form (counts per network, `login_needed` with
dashboard URLs, errors) for a later Slack step.

## Comparison rules

- Domains are lowercased, the relationship type is uppercased, whitespace is trimmed, comments (`#`, `//`),
  blank lines, variable lines (`OWNERDOMAIN=...`) and placeholder lines (anything with `[`, `]`, `<`, `>`,
  `{`, `}`, such as `mintegral.com, [yourPublisherID], DIRECT`) are dropped.
- Lines are compared on domain + id + type. Ids are compared case-insensitively. The cert id is kept and
  written out, but a differing cert id is only reported as a mismatch.
- Each network's `direct_lines` (our own DIRECT line) are always required, even when no list is available.
- **DIRECT/RESELLER normalization:** only a network's own domain (`own_domains` in `sources.json`) can be
  DIRECT for us. Any line in a network's list that is DIRECT for another domain is compared as RESELLER.
  Example: Mintegral's list marks adcolony, openx, pubmatic and webeyemob lines as DIRECT; we sell those
  through Mintegral, so our file has them as RESELLER on purpose.

## Sort rule of app-ads.txt

The record lines are sorted case-insensitively by the whole line (Python `str.lower`). Lines that differ only
in case keep their existing order. The file ends with a blank line and `OWNERDOMAIN=octofungames.com`.
`apply` inserts each new line before the first record line that sorts after it; a line that sorts last goes
after the last record line, so the `OWNERDOMAIN` block stays at the end. Existing lines are never moved.

## Snapshot format

`snapshots/<network key>.txt`, UTF-8, one app-ads line per line, exactly as the dashboard shows it. The
first line is a header comment with the source and the read date (`YYYY-MM-DD`):

```
# source: AppLovin dashboard App-Ads.txt Info, read 2026-11-18
applovin.com, d271b21f2a529bd779ccbe24f64e4e1c, DIRECT
bidease.com, bidease_seller_26, RESELLER
```

The first date found in a `#` comment line is the read date. No date, or a date older than 8 days, makes
the snapshot `stale`. A missing file is `missing`. Both put the network on the "Login needed" list. Write
the whole list each time; do not append.

## Adding a network

1. Add an entry to `networks` in `sources.json`: `key`, `name`, `required`, `type` (`public`, `snapshot`, `direct_only` or
   `none`; `direct_only` = the dashboard has no list, only our DIRECT line is checked), `own_domains`, `direct_lines` (our DIRECT line from app-ads.txt), and for `public` a
   `public_sources` list of `{url, parser}` (parsers: `markdown_code_blocks`, `plain`; add a new one to
   `PARSERS` in `watch.py` if needed). For `snapshot`, add `dashboard_path`, `dashboard_url`,
   `dashboard_url_verified`, `page_wait_seconds` (60, or 120 for slow dashboards) and `error_wait_seconds` (10).
2. If the network has an SDK adapter, map its registry id in `sdk_registry_mediation_map`.
3. Update the summary table in the SDK doc `docs/HESAP-KURULUMU.md` ("app-ads.txt kaynaklari") together with
   this file.
4. Run `report`, review the "To add" lines, then `apply --networks <key>` and commit the change on its own.

## Mac side

Files in `mac/`:

- `run_panels.py` (stdlib, `/usr/bin/python3`): the weekly run and the Slack retry. Takes a lock, runs
  `git pull --ff-only`, starts one Claude run (`claude -p --chrome`, Chrome tools only, prompt =
  `PANEL_TASK.md` + date + networks) that prints each list between markers, writes `snapshots/<key>.txt`,
  picks up the Liftoff download from `~/Downloads`, runs `watch.py report`, posts the summary to Slack and
  saves `state.json`. Options: `--networks a,b`, `--thread-ts <ts>`, `--no-slack` (print instead of post),
  `--skip-claude` (test: no pull, no Claude). It never commits or edits app-ads.txt.
- `listener.py` (venv, `slack_sdk`): Socket Mode listener; answers only `app_mention` in #alert-logger.
- `setup.sh`: creates `~/.config/app-ads-watch/venv` with the pinned `slack_sdk`.
- `launchd/`: `com.octofun.app-ads-listener.plist` (always on) and `com.octofun.app-ads-weekly.plist`
  (Monday 10:00 local time).
- `install.sh` / `uninstall.sh`: copy and load / unload and remove the two jobs in `~/Library/LaunchAgents`.

Install: `mac/setup.sh`, then `mac/install.sh`. Remove: `mac/uninstall.sh`.

Secrets live only in `~/.config/app-ads-watch/slack.env` (chmod 600: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`SLACK_CHANNEL`, `SLACK_CHANNEL_ID`). The GitHub Actions workflow posts its own short summary with the same bot
token and channel id, stored as the repository secrets `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` (added
2026-11-18). The channel id is kept out of this public repo on purpose. Logs: `~/.config/app-ads-watch/logs/` (`<date>-<time>.log` per run, `listener.log`,
launchd stdout/stderr). State: `~/.config/app-ads-watch/state.json` (last post ts, lines to add,
login-needed networks) and `last-report.md` / `last-report.json`. No token is ever logged. Claude runs on
the Max subscription login; the jobs never set `ANTHROPIC_API_KEY` and the scripts remove it.

Slack commands (mention the bot, any channel member may use them):

- `@app-ads tekrar dene` (or `retry`): reads the login-needed networks again (all snapshot networks if
  none) and replies in the thread. If a run is in progress the reply is "Zaten çalışıyor".
- `@app-ads onayla` (or `approve`), optionally with networks (`onayla unity inmobi`): runs `apply` for the
  networks that have lines to add, one commit per network, pushes, replies with the commit hashes and
  later when the lines are live. It only adds lines; if the push fails the commits stay local.
- Anything else gets a short help reply.

Weekly time: edit `StartCalendarInterval` (Weekday 1 = Monday, Hour, Minute; local time) in
`mac/launchd/com.octofun.app-ads-weekly.plist`, then run `mac/install.sh` again.
