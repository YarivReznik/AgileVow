"""Binding context and post-validation contracts on isolated persistent boards."""
import copy
import tempfile
import unittest
from pathlib import Path
from agilevow.board import TaskBoard, TaskError

P=[{'key':'quiet','title':'Calm default','content':'Keep motion quiet; no idle flashing. <script>data only</script>'}]
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=TaskBoard(self.root)
        self.board=self.store.create_board({'actor':'user','source_key':'a','title':'Board','principles':P})['board']
        self.plan=self.store.create_plan({'actor':'user','board_id':self.board['id'],'source_key':'a','title':'Plan'})['plan']
        self.task=self.store.create_task({'actor':'user','plan_id':self.plan['id'],'key':'a','title':'Task','outcome':'A stable visual','acceptance':['Result is visible'],'status':'ready'})['task']
    def act(self,op,**values):
        req={'actor':'worker','id':self.task['id'],'revision':self.task['revision'],'op':op,**values}
        result=self.store.action(req);self.task=result['task'];return result
    def update(self,**values):
        self.task=self.store.update_task({'actor':'worker','id':self.task['id'],'revision':self.task['revision'],**values})['task']
    def digest(self):return self.task['understanding_context']['principles_sha256']
    def changed_principles(self,values):
        self.board=self.store.update_board({'actor':'user','id':self.board['id'],'revision':self.board['revision'],'principles':values,'note':'New adopted requirement'})['board']
    def execute(self):
        self.lease=self.act('claim',principles_sha256=self.digest())['lease']
        self.update(lease=self.lease,evidence=[{'criterion':0,'ref':'proof.txt','note':'Observed actual candidate'}],checklist_checks=[{'criterion':0,'checked':True}])
        self.act('transition',status='post_validation',lease=self.lease)
    def report(self,principle_pass=True,criterion_pass=True):
        def row(passed):return {'passed':passed,'observed':'Observed steady output' if passed else 'Observed flashing output','ref':'capture.json',**({} if passed else {'expected':'A steady status light after a real success','avoid':'Idle flashing when nothing happened','fix':'Remove the idle loop; keep the real success cue'})}
        return {'result':'pass' if principle_pass and criterion_pass else 'fail','summary':'Compared target and principles','checks':[{'criterion':0,**row(criterion_pass)}],'principles':[{'key':'quiet',**row(principle_pass)}]}
    def test_current_context_in_existing_new_imported_and_exported_tasks(self):
        self.assertEqual(self.task['understanding_context']['principles'],P)
        self.assertLess(self.task['understanding_context']['text'].index(P[0]['content']),self.task['understanding_context']['text'].index('# Task'))
        self.changed_principles([{**P[0],'content':'A revised full requirement'}])
        before_revision=self.task['revision'];self.task=self.store.get_task(self.task['id'])['task']
        self.assertEqual(self.task['revision'],before_revision)
        self.assertEqual(self.task['understanding_context']['principles'][0]['content'],'A revised full requirement')
        imported=self.store.import_plan({'actor':'user','board_id':self.board['id'],'source_key':'import','title':'Imported','tasks':[{'key':'i','title':'Imported','outcome':'Result','acceptance':['A check']} ]})['tasks'][0]
        self.assertEqual(imported['understanding_context']['principles'],self.task['understanding_context']['principles'])
        restarted=TaskBoard(self.root);archive=restarted.export()
        self.assertEqual(archive['tasks'][0]['understanding_context']['principles'],self.task['understanding_context']['principles'])
        self.assertTrue(any(e.get('principles')==P for e in archive['events']))
    def test_other_boards_do_not_inherit_principles_from_another_board(self):
        board=self.store.create_board({'actor':'u','source_key':'b','title':'Other'})['board']
        plan=self.store.create_plan({'actor':'u','board_id':board['id'],'source_key':'b','title':'Other'})['plan']
        task=self.store.create_task({'actor':'u','plan_id':plan['id'],'key':'b','title':'Other','outcome':'Own outcome','acceptance':['Own criterion']})['task']
        self.assertEqual(task['understanding_context']['principles'],[])
    def test_missing_and_stale_context_cannot_claim(self):
        for digest in [None,'0'*64]:
            with self.assertRaises(TaskError):self.act('claim',principles_sha256=digest)
        self.act('claim',principles_sha256=self.digest())
        self.assertEqual(self.task['principles_acknowledgement']['actor'],'worker')
    def test_principle_changes_invalidate_validation_context_not_history(self):
        self.execute();old=self.digest()
        self.changed_principles([{**P[0],'content':'Never flash at rest, even on small screens'}])
        with self.assertRaises(TaskError):self.act('validate',principles_sha256=old,validation=self.report())
        self.task=self.store.get_task(self.task['id'])['task'];self.assertTrue(self.task['principles_changed'])
        self.act('validate',principles_sha256=self.digest(),validation=self.report())
        self.assertEqual(self.task['status'],'done')
    def test_every_current_principle_must_be_validated(self):
        self.execute();report=self.report();report['principles']=[]
        with self.assertRaisesRegex(TaskError,'every current binding principle'):self.act('validate',validation=report)
        self.assertEqual(self.store.get_task(self.task['id'])['task']['status'],'post_validation')
    def test_checklists_and_post_validation_cannot_be_bypassed(self):
        self.lease=self.act('claim',principles_sha256=self.digest())['lease']
        with self.assertRaises(TaskError):self.update(lease=self.lease,checklist_checks=[{'criterion':0,'checked':True}])
        self.update(lease=self.lease,evidence=[{'criterion':0,'ref':'proof.txt','note':'Actual result'}])
        with self.assertRaises(TaskError):self.act('transition',status='post_validation',lease=self.lease)
        with self.assertRaises(TaskError):self.act('transition',status='done',lease=self.lease)
        self.update(lease=self.lease,checklist_checks=[{'criterion':0,'checked':True}])
        self.act('transition',status='post_validation',lease=self.lease)
        with self.assertRaises(TaskError):self.act('transition',status='done')
    def test_failed_principle_creates_actionable_repair_checklist_then_retests(self):
        self.execute();self.act('validate',validation=self.report(principle_pass=False))
        self.assertEqual(self.task['status'],'review');c=self.task['corrections'][0]
        self.assertEqual(c['principle_key'],'quiet');self.assertFalse(c['checked'])
        self.assertIn('steady',c['expected']);self.assertIn('flashing',c['avoid'])
        self.lease=self.act('claim',principles_sha256=self.digest())['lease']
        with self.assertRaises(TaskError):self.act('transition',status='post_validation',lease=self.lease)
        with self.assertRaises(TaskError):self.update(lease=self.lease,correction_checks=[{'id':c['id'],'checked':True,'note':'','ref':''}])
        self.update(lease=self.lease,correction_checks=[{'id':c['id'],'checked':True,'note':'Removed idle animation and replayed win','ref':'retest.json'}])
        self.act('transition',status='post_validation',lease=self.lease)
        self.act('validate',validation=self.report())
        self.assertEqual(self.task['status'],'done');self.assertEqual([v['result'] for v in self.task['validations']],['fail','pass'])
    def test_failed_goal_unchecks_its_execution_item(self):
        self.execute();self.act('validate',validation=self.report(criterion_pass=False))
        self.assertFalse(self.task['checklist'][0]['checked']);self.assertEqual(self.task['corrections'][0]['criterion'],0)
    def test_failure_requires_expected_unwanted_examples_and_fix(self):
        self.execute();report=self.report(principle_pass=False);del report['principles'][0]['avoid']
        with self.assertRaises(TaskError):self.act('validate',validation=report)
        self.assertEqual(self.store.get_task(self.task['id'])['task'].get('corrections',[]),[])
    def test_board_conflict_duplicate_keys_and_malformed_principles_are_transactional(self):
        for principles in [P+P,[{**P[0],'content':''}],[{**P[0],'system_role':'system'}]]:
            with self.assertRaises(TaskError):self.changed_principles(principles)
        old_revision=self.board['revision'];self.changed_principles([{**P[0],'content':'Valid change'}])
        with self.assertRaises(TaskError):self.store.update_board({'actor':'u','id':self.board['id'],'revision':old_revision,'principles':P,'note':'stale'})
    def test_scope_correction_resets_checks_and_retains_validation_history(self):
        self.execute();self.act('validate',validation=self.report())
        self.update(outcome='Revised task',note='New request')
        self.assertEqual(self.task['status'],'backlog');self.assertFalse(self.task['checklist'][0]['checked'])
        self.assertEqual(len(self.task['validations']),1)

if __name__=='__main__':unittest.main()
