"""Weekly journal delivery and recovery; no candidate detection rules."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import urllib.request
import uuid
import zipfile

from . import entry_monitor as monitor
from .outcome_tracking import _request_json

DEDUP = 'candidate-dedup.json'
JOURNAL = 'candidate-journal.csv'
REPORT_STATE = 'candidate-report-state.json'
RESET_MESSAGE = '⚠️ Jurnalul automat a fost resetat (memoria GitHub pierdută).'
MAX_ARCHIVE = 25_000_000


def week_window(now_ms):
    local = datetime.fromtimestamp(now_ms / 1000, monitor.BRUSSELS)
    end = (local - timedelta(days=(local.weekday() + 1) % 7)).replace(hour=20, minute=0, second=0, microsecond=0)
    if local < end:
        end -= timedelta(days=7)
    return end - timedelta(days=7), end


def read_rows(path):
    from .candidate_monitor import CSV_FIELDS
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise monitor.MonitorError('Antet jurnal invalid')
        return list(reader)


def weekly_message(path, now_ms):
    start, end = week_window(now_ms)
    rows = read_rows(path)
    # CSV stores local dates/minutes; cutoffs are 20:00, outside ambiguous DST hours.
    stamp = lambda row: row['data'] + ' ' + row['oră']
    lower, upper = start.strftime('%Y-%m-%d %H:%M'), end.strftime('%Y-%m-%d %H:%M')
    weekly = [row for row in rows if lower <= stamp(row) < upper]
    sent = Counter(row['monedă'] for row in weekly if row['status'] == 'SENT')
    missed = sum(row['status'] == 'MISSED_STALE' for row in weekly)
    paper = [row for row in weekly if row['status'] == 'PAPER']
    outcomes = Counter(row['rezultat paper'] for row in paper)
    total = sum((Decimal(row['R paper']) for row in paper), Decimal(0))
    # Every stored candidate has one creation row and at most one PAPER row.
    before = [row for row in rows if stamp(row) < upper]
    opened = sum(row['status'] in ('SENT', 'MISSED_STALE') for row in before) - sum(row['status'] == 'PAPER' for row in before)
    return '\n'.join((
        f'📋 RAPORT SĂPTĂMÂNAL ({start:%d.%m.%Y}–{end:%d.%m.%Y})',
        f"Candidați trimiși: {sum(sent.values())} (BTC {sent['BTC']}, ETH {sent['ETH']}) | ratați noaptea (MISSED_STALE): {missed}",
        f"Rezultate paper închise: 2R atins {outcomes['2R ATINS']} | SL {outcomes['SL ATINS']} | timeout {outcomes['TIMEOUT']} | R total paper: {total:+.6f}R",
        f'Candidați încă deschiși (paper): {opened}',
    ))


def send_document(token, chat_id, path, caption):
    """One Telegram message contains both the report caption and complete CSV."""
    boundary = 'journal-' + uuid.uuid4().hex
    parts = []
    for key, value in (('chat_id', chat_id), ('caption', caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{JOURNAL}"\r\nContent-Type: text/csv\r\n\r\n'.encode(),
              path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()]
    request = urllib.request.Request(f'{monitor.TELEGRAM_BASE}/bot{token}/sendDocument',
                                     data=b''.join(parts), headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
        if payload.get('ok') is not True:
            raise ValueError('Telegram rejected document')
    except Exception as exc:
        # Never expose the request URL, token or chat ID through exceptions.
        raise monitor.TelegramError(f'Telegram sendDocument eșuat: {type(exc).__name__}') from None
    return payload.get('result', {})


def weekly_delivery(directory, now_ms, deliver):
    local = datetime.fromtimestamp(now_ms / 1000, monitor.BRUSSELS)
    if local.weekday() != 6 or local.hour < 20:
        return False
    _, end = week_window(now_ms)
    key = end.date().isoformat()
    path = directory / REPORT_STATE
    state = monitor.read_json(path) if path.exists() else {}
    if key in state.get('sent_weeks', []):
        return False
    journal = directory / JOURNAL
    message = weekly_message(journal, now_ms)
    deliver(journal, message)
    state.setdefault('sent_weeks', []).append(key)
    monitor.write_json(path, state)
    return True


def latest_bundle(repository, token):
    if not repository or not token:
        raise RuntimeError('GitHub recovery credentials unavailable')
    # Search newest first, including pagination past unrelated diagnostic artifacts.
    for page in range(1, 101):
        payload = _request_json(f'https://api.github.com/repos/{repository}/actions/artifacts?per_page=100&page={page}', token)
        items = payload.get('artifacts', [])
        matches = [a for a in items if a.get('name', '').startswith('candidate-journal-')
                   and not a.get('expired') and a.get('workflow_run', {}).get('head_branch') == 'main']
        for artifact in sorted(matches, key=lambda a: (a['created_at'], a['id']), reverse=True):
            # Construct the API URL rather than accepting a token-bearing arbitrary redirect target.
            request = urllib.request.Request(f"https://api.github.com/repos/{repository}/actions/artifacts/{int(artifact['id'])}/zip",
                                             headers={'Authorization': f'Bearer {token}', 'User-Agent': 'candidate-journal-recovery'})
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(MAX_ARCHIVE + 1)
            if len(raw) > MAX_ARCHIVE:
                raise ValueError('Artifact too large')
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(i.file_size for i in archive.infolist()) > MAX_ARCHIVE:
                    raise ValueError('Expanded artifact too large')
                result = {}
                for name in (DEDUP, JOURNAL, REPORT_STATE):
                    matches = [n for n in archive.namelist() if Path(n).name == name]
                    if len(matches) > 1:
                        raise ValueError('Ambiguous artifact')
                    if matches:
                        result[name] = archive.read(matches[0])
                # Old manual main runs used the same prefix for dry-run CSVs.
                if JOURNAL in result:
                    return result
        if len(items) < 100:
            return {}
    raise RuntimeError('Artifact search limit reached')


def validate_bundle(bundle):
    from .candidate_monitor import Candidate, write_journal
    if JOURNAL not in bundle:
        raise ValueError('Journal absent from backup')
    with tempfile.TemporaryDirectory() as temp:
        journal = Path(temp) / JOURNAL
        journal.write_bytes(bundle[JOURNAL])
        original = read_rows(journal)
        if DEDUP in bundle:
            state = json.loads(bundle[DEDUP])
            if not isinstance(state, dict):
                raise ValueError('Invalid dedup')
            for key, item in state.items():
                if Candidate(**item['candidate']).key != key or item['status'] not in ('SENT', 'MISSED_STALE'):
                    raise ValueError('Invalid candidate state')
            write_journal(journal, state)
            if read_rows(journal) != original:
                raise ValueError('Journal and dedup disagree')
    if REPORT_STATE in bundle:
        state = json.loads(bundle[REPORT_STATE])
        if not isinstance(state, dict) or not isinstance(state.get('sent_weeks', []), list):
            raise ValueError('Invalid report state')


def prepare_live(directory, repository, token):
    dedup, journal, reports = (directory / name for name in (DEDUP, JOURNAL, REPORT_STATE))
    if journal.exists() and not dedup.exists():
        raise monitor.MonitorError('Jurnal fără dedup. Telegram oprit.')
    if dedup.exists():
        # Recover weekly dedup independently if only its cache was evicted.
        if not reports.exists():
            try:
                bundle = latest_bundle(repository, token)
                validate_bundle(bundle)
                if REPORT_STATE in bundle:
                    monitor.write_json(reports, json.loads(bundle[REPORT_STATE]))
            except Exception:
                print('REPORT_STATE_RECOVERY_UNAVAILABLE')
        return 'CACHE'
    try:
        bundle = latest_bundle(repository, token)
        validate_bundle(bundle)
    except Exception as exc:
        print(f'ARTIFACT_RECOVERY_FAILED: {type(exc).__name__}')
        bundle = {}
    directory.mkdir(parents=True, exist_ok=True)
    if bundle:
        # Journal first: interruption can only leave the fail-closed state.
        journal.write_bytes(bundle[JOURNAL])
        if DEDUP not in bundle:
            raise monitor.MonitorError('Artifact vechi: jurnal fără dedup. Telegram oprit.')
        monitor.write_json(dedup, json.loads(bundle[DEDUP]))
        if REPORT_STATE in bundle:
            restored = json.loads(bundle[REPORT_STATE])
            cached = monitor.read_json(reports) if reports.exists() else {}
            restored['sent_weeks'] = sorted(set(restored.get('sent_weeks', [])) | set(cached.get('sent_weeks', [])))
            if cached.get('reset_pending'):
                restored['reset_pending'] = True
            monitor.write_json(reports, restored)
        print('RECOVERED_FROM_ARTIFACT')
        return 'RECOVERED_FROM_ARTIFACT'
    state = monitor.read_json(reports) if reports.exists() else {}
    state['reset_pending'] = True
    monitor.write_json(reports, state)
    monitor.write_json(dedup, {})
    print('JOURNAL_RESET')
    return 'JOURNAL_RESET'


def notify_reset(directory, sender):
    path = directory / REPORT_STATE
    state = monitor.read_json(path) if path.exists() else {}
    if state.get('reset_pending'):
        sender(RESET_MESSAGE)
        state['reset_pending'] = False
        monitor.write_json(path, state)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Preview only: never reads credentials or sends Telegram')
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--as-of', required=True, help='Timezone-aware ISO timestamp')
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.as_of)
    if now.tzinfo is None:
        parser.error('--as-of must include an offset')
    print(weekly_message(args.journal, int(now.timestamp() * 1000)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
