#!/usr/bin/env python3
"""
LateTrains fetcher — queries the National Rail HSP API for commute delays
and writes a rolling 5-week report to data.json.

Runs via LaunchAgent on a schedule. On each run:
  1. Loads existing data.json (if any).
  2. Works out which weekdays in the last 35 days are missing.
  3. Fetches them sequentially, classifying each train as direct/stopping
     by intermediate-stop count.
  4. Stores any train that arrived 15+ mins late.
  5. Prunes anything older than 35 days.
  6. Commits and pushes data.json to the repo.

Credentials are read from ~/.commute-tracker/creds.env (outside the repo).
Usage:
    python3 fetcher.py                 # normal run (catch up missing days)
    python3 fetcher.py --date 2026-04-15   # re-fetch one specific day
    python3 fetcher.py --no-push       # skip the git commit/push step
"""

import argparse
import base64
import json
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib import error, request

# macOS Python doesn't have system CA certs configured; disable verification
# for the HSP API (hsp-prod.rockshore.net — internal National Rail service).
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ─── Config ─────────────────────────────────────────────────────────────────
REPO_DIR   = Path(__file__).resolve().parent
DATA_FILE  = REPO_DIR / 'data.json'
CREDS_FILE = Path.home() / '.commute-tracker' / 'creds.env'
LOG_FILE   = REPO_DIR / 'fetcher.log'

FROM_STATION = 'DID'
TO_STATION   = 'PAD'
AM_WINDOW = ('0530', '0900')   # DID → PAD
PM_WINDOW = ('1530', '1930')   # PAD → DID

LATE_THRESHOLD_MINS          = 15
DIRECT_MAX_INTERMEDIATE_STOPS = 1   # 0 or 1 intermediate stops = direct
PRUNE_DAYS = 35                     # keep last 5 weeks of weekdays

HSP_METRICS = 'https://hsp-prod.rockshore.net/api/v1/serviceMetrics'
HSP_DETAILS = 'https://hsp-prod.rockshore.net/api/v1/serviceDetails'

INTER_REQUEST_DELAY = 0.15   # seconds between HSP calls (polite pacing)
DETAILS_CONCURRENCY = 3      # max concurrent serviceDetails calls per day

# ─── Logging ────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().isoformat(timespec='seconds')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass  # logging failures should never crash the fetcher

# ─── Credentials ────────────────────────────────────────────────────────────
def load_creds():
    if not CREDS_FILE.exists():
        log(f'ERROR: creds file not found at {CREDS_FILE}')
        log('Create it with:')
        log('  HSP_EMAIL=your@email.com')
        log('  HSP_PASSWORD=yourpassword')
        sys.exit(1)
    creds = {}
    with open(CREDS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")
    if 'HSP_EMAIL' not in creds or 'HSP_PASSWORD' not in creds:
        log('ERROR: missing HSP_EMAIL or HSP_PASSWORD in creds.env')
        sys.exit(1)
    return creds['HSP_EMAIL'], creds['HSP_PASSWORD']

def auth_header(email, password):
    token = base64.b64encode(f'{email}:{password}'.encode()).decode()
    return f'Basic {token}'

# ─── HSP API ────────────────────────────────────────────────────────────────
def hsp_post(url, body, auth, timeout=60, retries=2):
    payload = json.dumps(body).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = request.Request(url, data=payload, method='POST', headers={
                'Content-Type': 'application/json',
                'Authorization': auth,
            })
            with request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                time.sleep(INTER_REQUEST_DELAY)
                return json.loads(resp.read().decode())
        except error.HTTPError as e:
            body_txt = e.read().decode(errors='replace')[:200]
            # 401/403 = auth problem, don't retry
            if e.code in (401, 403):
                raise RuntimeError(f'HTTP {e.code}: {body_txt}')
            last_err = RuntimeError(f'HTTP {e.code}: {body_txt}')
        except Exception as e:
            last_err = e
        if attempt < retries:
            time.sleep(1 + attempt * 2)  # 1s, 3s backoff
    raise last_err

def fetch_metrics(from_crs, to_crs, iso_date, from_time, to_time, auth):
    body = {
        'from_loc':  from_crs,
        'to_loc':    to_crs,
        'from_time': from_time,
        'to_time':   to_time,
        'from_date': iso_date,
        'to_date':   iso_date,
        'days':      'WEEKDAY',
    }
    resp = hsp_post(HSP_METRICS, body, auth)
    return resp.get('Services') or []

def fetch_details(rid, auth):
    resp = hsp_post(HSP_DETAILS, {'rid': rid}, auth)
    return resp.get('serviceAttributesDetails') or {}

# ─── Classification & parsing ───────────────────────────────────────────────
def classify_service(locations, from_crs, to_crs):
    """
    Returns a dict with delay info, or None if the service doesn't match
    this origin→destination or has no actual arrival time.
    """
    origin_idx = next((i for i, l in enumerate(locations)
                      if l.get('location') == from_crs), None)
    dest_idx   = next((i for i, l in enumerate(locations)
                      if l.get('location') == to_crs), None)
    if origin_idx is None or dest_idx is None or dest_idx <= origin_idx:
        return None

    origin = locations[origin_idx]
    dest   = locations[dest_idx]

    intermediate_count = dest_idx - origin_idx - 1
    svc_type = ('direct' if intermediate_count <= DIRECT_MAX_INTERMEDIATE_STOPS
                else 'stopping')

    dep_sched  = origin.get('gbtt_ptd') or origin.get('gbtt_pta') or ''
    arr_sched  = dest.get('gbtt_pta')   or dest.get('gbtt_ptd')   or ''
    arr_actual = dest.get('actual_ta')  or dest.get('actual_td')  or ''

    if not dep_sched or not arr_sched or not arr_actual:
        return None

    return {
        'svc_type':   svc_type,
        'dep_sched':  dep_sched,
        'arr_sched':  arr_sched,
        'arr_actual': arr_actual,
    }

def mins_between(hhmm_a, hhmm_b):
    """Minutes from a to b. Handles midnight crossover."""
    if len(hhmm_a) < 4 or len(hhmm_b) < 4:
        return 0
    a = int(hhmm_a[:2]) * 60 + int(hhmm_a[2:4])
    b = int(hhmm_b[:2]) * 60 + int(hhmm_b[2:4])
    diff = b - a
    if diff < -720:    # b is next day
        diff += 1440
    elif diff > 720:   # a is next day (shouldn't happen for our window)
        diff -= 1440
    return diff

def fmt_time(hhmm):
    if len(hhmm) < 4:
        return ''
    return f'{hhmm[:2]}:{hhmm[2:4]}'

# ─── Fetch one day ──────────────────────────────────────────────────────────
def fetch_day(iso_date, auth):
    """
    Fetch both commute windows for one date.
    Returns {am_direct: [...], am_stopping: [...], pm_direct: [...], pm_stopping: [...]}
    Each list contains all trains >=15m late, sorted by delay desc.
    Raises on metrics-level failures (so the day won't be saved partial).
    """
    result = {
        'am_direct':   [],
        'am_stopping': [],
        'pm_direct':   [],
        'pm_stopping': [],
    }

    windows = [
        ('am', FROM_STATION, TO_STATION, AM_WINDOW),
        ('pm', TO_STATION,   FROM_STATION, PM_WINDOW),
    ]

    for period, from_crs, to_crs, (t_from, t_to) in windows:
        services = fetch_metrics(from_crs, to_crs, iso_date, t_from, t_to, auth)
        log(f'  {period} {from_crs}→{to_crs}: {len(services)} services')

        # Collect rids to fetch in parallel
        rids = []
        for svc in services:
            metrics = svc.get('serviceAttributesMetrics') or {}
            rid_list = metrics.get('rids') or []
            if rid_list:
                rids.append((rid_list[0], metrics.get('toc_code') or ''))

        def fetch_one(rid_and_toc):
            rid, metrics_toc = rid_and_toc
            try:
                details = fetch_details(rid, auth)
                return rid, metrics_toc, details
            except Exception as e:
                log(f'    details failed for rid {rid}: {e}')
                return rid, metrics_toc, None

        with ThreadPoolExecutor(max_workers=DETAILS_CONCURRENCY) as pool:
            results = list(pool.map(fetch_one, rids))

        for rid, metrics_toc, details in results:
            if not details:
                continue
            locations = details.get('locations') or []
            info = classify_service(locations, from_crs, to_crs)
            if not info:
                continue

            delay = mins_between(info['arr_sched'], info['arr_actual'])
            if delay < LATE_THRESHOLD_MINS:
                continue

            toc = (details.get('toc_code') or metrics_toc or '').strip()

            entry = {
                'delay':      delay,
                'dep':        fmt_time(info['dep_sched']),
                'arr_sched':  fmt_time(info['arr_sched']),
                'arr_actual': fmt_time(info['arr_actual']),
                'toc':        toc,
            }
            key = f'{period}_{info["svc_type"]}'
            result[key].append(entry)

    for key in result:
        result[key].sort(key=lambda e: -e['delay'])

    return result

# ─── Data file ──────────────────────────────────────────────────────────────
def load_data():
    if not DATA_FILE.exists():
        return {'last_updated': None, 'days': {}}
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        data.setdefault('days', {})
        return data
    except Exception as e:
        log(f'data.json load failed ({e}), starting fresh')
        return {'last_updated': None, 'days': {}}

def save_data(data):
    data['last_updated'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2, sort_keys=True)

def prune(data, today):
    cutoff = today - timedelta(days=PRUNE_DAYS)
    before = len(data['days'])
    data['days'] = {
        d: v for d, v in data['days'].items()
        if date.fromisoformat(d) >= cutoff
    }
    removed = before - len(data['days'])
    if removed:
        log(f'pruned {removed} day(s) older than {cutoff.isoformat()}')

def missing_dates(data, today):
    """Weekdays in the last 35 days (excluding today) that aren't in data."""
    existing = set(data['days'].keys())
    missing = []
    for i in range(1, PRUNE_DAYS + 1):
        d = today - timedelta(days=i)
        if d.weekday() < 5 and d.isoformat() not in existing:
            missing.append(d.isoformat())
    return sorted(missing)

# ─── Git ────────────────────────────────────────────────────────────────────
def git_commit_push():
    try:
        subprocess.run(
            ['git', '-C', str(REPO_DIR), 'add', 'data.json'],
            check=True, capture_output=True,
        )
        commit = subprocess.run(
            ['git', '-C', str(REPO_DIR), 'commit',
             '-m', f'data: update {date.today().isoformat()}'],
            capture_output=True,
        )
        if commit.returncode != 0:
            combined = (commit.stdout + commit.stderr).decode()
            if 'nothing to commit' in combined:
                log('git: no changes to commit')
                return
            log(f'git commit failed: {combined.strip()}')
            return
        push = subprocess.run(
            ['git', '-C', str(REPO_DIR), 'push'],
            capture_output=True,
        )
        if push.returncode != 0:
            log(f'git push failed: {(push.stdout + push.stderr).decode().strip()}')
        else:
            log('git: pushed to origin')
    except Exception as e:
        log(f'git failed: {e}')

# ─── Main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='Fetch a specific ISO date (YYYY-MM-DD)')
    ap.add_argument('--no-push', action='store_true', help='Skip git push')
    args = ap.parse_args()

    log('=' * 48)
    log('fetcher run starting')

    email, password = load_creds()
    auth = auth_header(email, password)

    data = load_data()
    today = date.today()

    if args.date:
        dates_to_fetch = [args.date]
        log(f'targeted fetch: {args.date}')
    else:
        dates_to_fetch = missing_dates(data, today)
        log(f'{len(dates_to_fetch)} missing date(s) to fetch')

    fetched = 0
    for iso in dates_to_fetch:
        log(f'fetching {iso}')
        try:
            day_data = fetch_day(iso, auth)
            data['days'][iso] = day_data
            save_data(data)   # save after each day so partial progress sticks
            fetched += 1
            totals = {k: len(v) for k, v in day_data.items()}
            log(f'  stored · {totals}')
        except Exception as e:
            log(f'  FAILED: {e}')
            continue

    prune(data, today)
    save_data(data)

    if fetched and not args.no_push:
        git_commit_push()
    elif not fetched:
        log('no new data, skipping git push')

    log(f'done · {fetched} day(s) fetched')

if __name__ == '__main__':
    main()
