"""Prepare criterion-by-criterion QA reports and submit reviewed decisions."""
import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen


def prepare(task):
    if task['status'] != 'post_validation':
        raise ValueError('Task must be in Post validation')
    report = {'task_id': task['id'], 'revision': task['revision'], 'reviewer': '',
            'principles_sha256': task.get('understanding_context', {}).get('principles_sha256'),
            'summary': '',
            'principles': [{**p, 'result': 'pending', 'method': '', 'observations': '', 'evidence': [], 'expected': '', 'avoid': '', 'fix': ''}
                           for p in task.get('understanding_context', {}).get('principles', [])],
            'checks': [{'criterion': i, 'acceptance': a, 'result': 'pending',
                        'method': '', 'observations': '', 'evidence': [], 'expected': '', 'avoid': '', 'fix': ''}
                       for i, a in enumerate(task['acceptance'])]}
    return report


def verdict(task, report):
    if task['status'] != 'post_validation' or report['task_id'] != task['id'] or report['revision'] != task['revision']:
        raise ValueError('Task changed or is not in Post validation; prepare a fresh report')
    if not report.get('reviewer', '').strip():
        raise ValueError('Reviewer is required')
    if not report.get('summary', '').strip():
        raise ValueError('Validation summary is required')
    context = task.get('understanding_context', {})
    if report.get('principles_sha256') != context.get('principles_sha256'):
        raise ValueError('Principles changed; read them and prepare a fresh report')
    principles = report.get('principles', [])
    if [{k: p.get(k) for k in ('key','title','content')} for p in principles] != context.get('principles', []):
        raise ValueError('Check every current principle with its exact full content')
    checks = report.get('checks', [])
    if [c['criterion'] for c in checks] != list(range(len(task['acceptance']))):
        raise ValueError('Exactly one ordered check per criterion is required')
    for c, acceptance in zip(checks, task['acceptance']):
        if c.get('acceptance') != acceptance:
            raise ValueError('Acceptance text changed')
    for c in checks + principles:
        if c.get('result') not in ('pass', 'fail'):
            raise ValueError('Pending checks cannot be submitted')
        if not c.get('method', '').strip() or not c.get('observations', '').strip():
            raise ValueError('Each check needs its method and actual observations')
        if not isinstance(c.get('evidence'), list) or not c['evidence'] or not all(isinstance(e, str) and e.strip() for e in c['evidence']):
            raise ValueError('Each check needs evidence references, including failures')
        if c['result'] == 'fail' and any(not c.get(f, '').strip() for f in ('expected','avoid','fix')):
            raise ValueError('Failure needs expected example, unwanted example and clear repair')
    if task.get('source_changed'):
        raise ValueError('Sources changed; reconcile before review')
    failures = any(c['result'] == 'fail' for c in checks + principles)
    return 'review' if failures else 'done'


def main(argv=None):
    """Runs the command; every expected failure is one line on stderr and exit status 1, never a traceback."""
    try:
        return _main(argv)
    except FileExistsError as error:
        print('agilevow review: the report file already exists: %s. Delete it, or pick another --report.' % error.filename, file=sys.stderr)
    except urllib.error.HTTPError as error:
        print('agilevow review: the server answered %s: %s' % (error.code, error.read().decode(errors='replace')[:300]), file=sys.stderr)
    except urllib.error.URLError as error:
        print('agilevow review: cannot reach the server: %s. Is `python -m agilevow serve` running?' % error.reason, file=sys.stderr)
    except (OSError, ValueError, KeyError) as error:
        print('agilevow review: %s' % (error.strerror or error if isinstance(error, OSError) else error), file=sys.stderr)
    return 1


def _main(argv):
    p = argparse.ArgumentParser(prog='agilevow review', description=__doc__)
    p.add_argument('operation', choices=['prepare', 'submit'])
    p.add_argument('--task', required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--url', default=os.environ.get('AGILEVOW_URL', 'http://127.0.0.1:8765'))
    a = p.parse_args(argv)
    def api(route, body=None):
        request = Request(a.url.rstrip('/')+route, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type':'application/json'})
        with urlopen(request, timeout=15) as r: return json.load(r)
    task = api('/api/tasks/item?id='+a.task)['task']
    if a.operation == 'prepare':
        with a.report.open('x') as f: json.dump(prepare(task), f, indent=2)
        print('Prepared pending report; run relevant tests and inspect artifacts before completing checks.')
        return
    report = json.loads(a.report.read_text())
    status = verdict(task, report)
    # Retain the complete decision on the card before the guarded transition.
    body = json.dumps(report, ensure_ascii=False)
    if len(body) > 11000: raise ValueError('Report too large for retained card comment')
    actor = report['reviewer']
    task = api('/api/tasks/comment', dict(id=task['id'], revision=task['revision'], actor=actor,
                kind='decision', body='Post validation: '+status+'\n'+body))['task']
    def row(check, key):
        return {key: check[key], 'passed': check['result']=='pass',
                'observed': check['method']+' — '+check['observations'], 'ref': check['evidence'][0],
                **{f:check.get(f,'') for f in ('expected','avoid','fix')}}
    task = api('/api/tasks/action', dict(id=task['id'], revision=task['revision'], actor=actor,
                op='validate', principles_sha256=report['principles_sha256'], validation={
                    'result': 'pass' if status=='done' else 'fail', 'summary': report['summary'],
                    'checks': [row(c,'criterion') for c in report['checks']],
                    'principles': [row(c,'key') for c in report['principles']]}))['task']
    print(json.dumps({'task':task['id'], 'status':task['status']}))


if __name__ == '__main__':
    sys.exit(main())
