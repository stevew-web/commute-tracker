# LateTrains — setup notes

New architecture, at a glance:

```
 ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
 │  fetcher.py      │─────▶│  data.json       │─────▶│  index.html      │
 │  (runs on Mac    │      │  (static JSON    │      │  (GitHub Pages,  │
 │   via launchd)   │      │   in the repo)   │      │   reads JSON)    │
 └──────────────────┘      └──────────────────┘      └──────────────────┘
         │                         ▲
         ▼                         │
    HSP API                    git push
```

Terminal is gone. Proxy is gone. The site is a dumb reader, the Mac does the work in the background.

## One-time setup

### 1. Put the files in the repo

In your existing commute-tracker repo directory:

```
commute-tracker/
├── .git/
├── index.html               ← replace the old commute-delay-tracker.html
├── fetcher.py               ← new
├── data.json                ← will be created on first run
├── fetcher.log              ← will be created on first run (gitignore it)
└── .gitignore               ← add fetcher.log
```

Append to `.gitignore`:

```
fetcher.log
.DS_Store
```

### 2. Store HSP credentials outside the repo

```sh
mkdir -p ~/.commute-tracker
cat > ~/.commute-tracker/creds.env <<EOF
HSP_EMAIL=your@email.com
HSP_PASSWORD=yourpassword
EOF
chmod 600 ~/.commute-tracker/creds.env
```

The fetcher reads from this path. The file is never touched by git.

### 3. Test a manual run

```sh
cd ~/path/to/commute-tracker
python3 fetcher.py
```

On first run it'll fetch all missing weekdays in the last 35 days — that's up to 25 dates, and it's paced politely, so expect 3–8 minutes. Subsequent runs only fetch what's new.

You'll see `data.json` appear, a `fetcher.log` file, and a git commit + push if anything changed.

If the git push prompts for credentials, configure an SSH key or a credential helper first — launchd won't be able to answer prompts.

### 4. Install the LaunchAgent

Edit `com.stevew.commute-tracker-fetcher.plist` — replace the three `REPLACE_WITH_ABSOLUTE_PATH` occurrences with the real repo path (e.g. `/Users/steve/code/commute-tracker`).

Then:

```sh
cp com.stevew.commute-tracker-fetcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stevew.commute-tracker-fetcher.plist
```

From here on:
- **Every day at 22:00** the fetcher runs. If the Mac is asleep, launchd fires it on wake.
- **On login** (e.g. after reboot) the fetcher also runs, catching up anything missed.

To check it's loaded:
```sh
launchctl list | grep commute-tracker
```

To uninstall:
```sh
launchctl unload ~/Library/LaunchAgents/com.stevew.commute-tracker-fetcher.plist
```

### 5. Point GitHub Pages at index.html

If the old page was `commute-delay-tracker.html`, rename to `index.html` or update the Pages config to serve the new file. GitHub Pages auto-publishes on push.

## Operational notes

- **5 weeks rolling window.** The fetcher prunes anything older than 35 days on every run. No manual housekeeping.
- **Today is skipped.** Only completed weekdays are fetched. Friday's data appears Saturday morning.
- **Partial failures are self-healing.** If a day fails (network blip, HSP down), the next run finds it missing and retries. No state to manage.
- **One-off re-fetch.** If a day looks wrong:
  ```sh
  python3 fetcher.py --date 2026-04-15
  ```
- **Debug without publishing.**
  ```sh
  python3 fetcher.py --no-push
  ```
- **Logs.** `fetcher.log` in the repo dir, with timestamps per run.

## What's in data.json

```json
{
  "last_updated": "2026-04-20T22:04:17+00:00",
  "days": {
    "2026-04-13": {
      "am_direct":   [{ "delay": 16, "dep": "07:24", "arr_sched": "08:14", "arr_actual": "08:30", "toc": "GW" }],
      "am_stopping": [],
      "pm_direct":   [{ "delay": 54, "dep": "17:03", "arr_sched": "18:14", "arr_actual": "19:08", "toc": "GW" }],
      "pm_stopping": []
    },
    "2026-04-14": { ... }
  }
}
```

Each scenario holds **every** train 15+ mins late, sorted worst-first. The frontend shows the top 3 in the grid and the full list in the click-through panel.

## Things that could go wrong

| Symptom | Likely cause | Fix |
|--|--|--|
| No data, error on page | `data.json` not yet pushed to repo | Run `python3 fetcher.py` manually once |
| Fetcher silently does nothing | Already up to date | Check `fetcher.log` to confirm |
| Git push asks for password | No SSH key / credential helper | Set up SSH key or `git config credential.helper osxkeychain` |
| 401 errors in log | Wrong credentials | Re-check `creds.env` |
| launchd doesn't fire at 22:00 | Mac was off/asleep | It'll run on next wake via `RunAtLoad` |
