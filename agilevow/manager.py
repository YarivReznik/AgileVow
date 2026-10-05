"""Read-only flow audit. The worker applies decisions via revision/lease API."""
import argparse
import json
import os
import sys
import time
import urllib.error
from urllib.parse import urlencode
from urllib.request import urlopen


def audit(tasks, now, wip_limit=1, stale_seconds=3600):
    by_id = {t['id']: t for t in tasks}
    findings = []
    doing = [t for t in tasks if t['status'] == 'doing']
    if len(doing) > wip_limit:
        findings.append({'kind': 'wip_exceeded', 'tasks': [t['id'] for t in doing]})
    eligible = []
    for t in tasks:
        status = t['status']
        if status in ('cancelled', 'backlog'):
            continue
        unmet = [d for d in t.get('depends_on', []) if by_id.get(d, {}).get('status') != 'done']
        if status in ('ready', 'doing', 'review', 'post_validation') and unmet:
            findings.append({'kind': 'dependencies_unfinished', 'task': t['id'], 'dependencies': unmet})
        if t.get('source_changed'):
            findings.append({'kind': 'sources_changed', 'task': t['id']})
        if t.get('principles_need_acknowledgement') and status in ('doing', 'review', 'post_validation'):
            findings.append({'kind': 'principles_read_required', 'task': t['id']})
        if status == 'post_validation':
            findings.append({'kind': 'post_validation_required', 'task': t['id']})
        if status == 'doing':
            if (t.get('claim_expires_at') or 0) <= now:
                findings.append({'kind': 'lease_expired', 'task': t['id']})
            if now - t['updated_at'] >= stale_seconds:
                findings.append({'kind': 'stale_activity', 'task': t['id']})
        if status == 'blocked':
            findings.append({'kind': 'blocked', 'task': t['id'], 'reason': t.get('blocked_reason', '')})
        if status in ('review', 'post_validation', 'done'):
            covered = {e['criterion'] for e in t.get('evidence', []) if e.get('ref')}
            missing = [i for i in range(len(t.get('acceptance', []))) if i not in covered]
            if missing:
                findings.append({'kind': 'missing_evidence', 'task': t['id'], 'criteria': missing})
        if status == 'ready' and not unmet and not t.get('source_changed'):
            eligible.append(t)
    validation = [t for t in tasks if t['status'] == 'post_validation']
    repairs = [t for t in tasks if t['status'] == 'review' and any(not c.get('checked') for c in t.get('corrections', []))]
    validation.sort(key=lambda t: (t.get('priority', 2), t['created_at'], t['id']))
    repairs.sort(key=lambda t: (t.get('priority', 2), t['created_at'], t['id']))
    eligible.sort(key=lambda t: (t.get('priority', 2), t['created_at'], t['id']))
    next_work = validation + repairs + eligible
    return {'mode': 'advisory', 'wip_limit': wip_limit, 'doing': [t['id'] for t in doing],
            'post_validation_queue': [t['id'] for t in validation],
            'correction_queue': [t['id'] for t in repairs],
            'review_queue': [t['id'] for t in tasks if t['status'] == 'review'],
            'ready_queue': [t['id'] for t in eligible],
            'next_task': next_work[0]['id'] if next_work and len(doing) < wip_limit else None,
            'findings': findings,
            'worker_policy': 'Checkpoint current work before reprioritizing. Never auto-complete, steal a lease, or promote backlog. Read the complete current principles before understanding work. Complete checklists, then substantively validate every goal AND principle. Failed validation returns Review with explicit repair examples; status does not schedule agents.'}


def main(argv=None):
    p = argparse.ArgumentParser(prog='agilevow manager', description=__doc__)
    p.add_argument('--url', default=os.environ.get('AGILEVOW_URL', 'http://127.0.0.1:8765'))
    p.add_argument('--board', required=True, help='board key or id (see: python -m agilevow client boards)')
    p.add_argument('--wip-limit', type=int, default=1)
    p.add_argument('--stale-seconds', type=int, default=3600)
    a = p.parse_args(argv)
    if a.wip_limit < 1 or a.stale_seconds < 1:
        p.error('limits must be positive')
    base = a.url.rstrip('/')
    try:
        with urlopen(base + '/api/boards', timeout=15) as r:
            boards = json.load(r)['boards']
        match = next((b for b in boards if a.board in (b['id'], b['source_key'])), None)
        if match is None:
            raise ValueError('no board %r; boards on this server: %s' % (a.board, ', '.join(b['source_key'] for b in boards) or 'none yet'))
        with urlopen(base + '/api/tasks?' + urlencode({'board': match['id']}), timeout=15) as r:
            snapshot = json.load(r)
    except urllib.error.HTTPError as error:
        print('agilevow manager: %s answered %s: %s' % (a.url, error.code, error.read().decode(errors='replace')[:300]), file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print('agilevow manager: cannot reach %s: %s. Is `python -m agilevow serve` running?' % (a.url, error.reason), file=sys.stderr)
        return 1
    except ValueError as error:
        print('agilevow manager: %s' % error, file=sys.stderr)
        return 1
    result = audit(snapshot['tasks'], int(time.time()), a.wip_limit, a.stale_seconds)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
