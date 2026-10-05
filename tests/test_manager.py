import contextlib
import io
import unittest
from agilevow.manager import audit, main

def task(id, status='ready', **kw):
    return dict(id=id,status=status,created_at=1,updated_at=90,**kw)

class FlowTests(unittest.TestCase):
    def test_ready_before_backlog_and_dependency_guard(self):
        r=audit([task('a','backlog'),task('b',depends_on=['a']),task('c')],100)
        self.assertEqual(r['next_task'],'c')
        self.assertEqual(r['ready_queue'],['c'])
    def test_wip_prevents_second_claim(self):
        r=audit([task('a','doing',claim_expires_at=200),task('b')],100)
        self.assertIsNone(r['next_task'])
    def test_expired_is_reported_not_recovered(self):
        tasks=[task('a','doing',claim_expires_at=50)]
        r=audit(tasks,100)
        self.assertIn('lease_expired',[f['kind'] for f in r['findings']])
        self.assertEqual(tasks[0]['status'],'doing')
    def test_source_drift_and_evidence(self):
        r=audit([task('a',source_changed=True),task('b','done',acceptance=['one','two'],evidence=[{'criterion':0,'ref':'proof'}])],100)
        self.assertIsNone(r['next_task'])
        self.assertIn({'kind':'missing_evidence','task':'b','criteria':[1]},r['findings'])

if __name__=='__main__':unittest.main()

class ValidationFlowTests(unittest.TestCase):
    def test_validate_then_repair_before_new_work(self):
        rows=[task('new'),task('fix','review',corrections=[{'checked':False}]),task('inspect','post_validation',principles_need_acknowledgement=True)]
        result=audit(rows,100)
        self.assertEqual(result['next_task'],'inspect')
        self.assertEqual(result['post_validation_queue'],['inspect'])
        self.assertEqual(result['correction_queue'],['fix'])
        self.assertEqual(audit(rows[:2],100)['next_task'],'fix')


class ManagerOutputTests(unittest.TestCase):
    def test_a_ready_card_is_not_flagged_for_principles_nobody_could_have_read_yet(self):
        rows = [task('a', principles_need_acknowledgement=True),
                task('b', 'doing', claim_expires_at=200, principles_need_acknowledgement=True)]
        found = {(f['kind'], f['task']) for f in audit(rows, 100)['findings']}
        self.assertNotIn(('principles_read_required', 'a'), found)
        self.assertIn(('principles_read_required', 'b'), found)  # a claimed card whose principles changed under it

    def test_an_unreachable_server_is_one_line_not_a_traceback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(['--url', 'http://127.0.0.1:1', '--board', 'x'])
        self.assertEqual(code, 1)
        self.assertIn('python -m agilevow serve', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())
