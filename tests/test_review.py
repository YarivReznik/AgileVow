import unittest
from agilevow.review import prepare, submit, verdict

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.t=dict(id='t',revision=3,status='post_validation',acceptance=['Works'])
        self.r=prepare(self.t);self.r['reviewer']='qa';self.r['summary']='Inspected goals and principles'
    def complete(self,result='pass'):
        self.r['checks'][0].update(result=result,method='Run integration test',observations='Observed result',evidence=['capture.json'],expected='Correct result',avoid='Wrong result',fix='Repair the result')
    def test_pending_cannot_pass(self):
        with self.assertRaises(ValueError):verdict(self.t,self.r)
    def test_pass_and_fail(self):
        self.complete();self.assertEqual(verdict(self.t,self.r),'done')
        self.complete('fail');self.assertEqual(verdict(self.t,self.r),'review')
    def test_stale_and_source_drift(self):
        self.complete();self.t['revision']=4
        with self.assertRaises(ValueError):verdict(self.t,self.r)
        self.t['revision']=3;self.t['source_changed']=True
        with self.assertRaises(ValueError):verdict(self.t,self.r)
    def test_missing_evidence(self):
        self.complete();self.r['checks'][0]['evidence']=[]
        with self.assertRaises(ValueError):verdict(self.t,self.r)

class PrincipleReviewTests(unittest.TestCase):
    def test_principle_failure_and_change(self):
        t=dict(id='p',revision=1,status='post_validation',acceptance=['Works'],understanding_context={'principles_sha256':'hash','principles':[dict(key='calm',title='Calm',content='No idle pulse')]})
        r=prepare(t);r.update(reviewer='qa',summary='Inspected output')
        for c in r['checks']+r['principles']:
            c.update(result='pass',method='Inspect',observations='Stable',evidence=['result.png'])
        self.assertEqual(verdict(t,r),'done')
        r['principles'][0].update(result='fail',expected='Still',avoid='Pulse',fix='Remove idle pulse')
        self.assertEqual(verdict(t,r),'review')
        t['understanding_context']['principles_sha256']='changed'
        with self.assertRaises(ValueError):verdict(t,r)

class SignedByWhoeverSubmitsTests(unittest.TestCase):
    """The reviewer is whoever submits the report, never a name typed into it (the CLI and MCP both pass `actor`)."""
    def setUp(self):
        self.t=dict(id='t',revision=3,status='post_validation',acceptance=['Works'])
        self.r=prepare(self.t,reviewer='claude');self.r['summary']='Looked'
        self.r['checks'][0].update(result='pass',method='Ran it',observations='It works',evidence=['out.txt'])
        self.calls=[]
        self.api=lambda route,body=None:(self.calls.append(route),{'task':dict(self.t,status='done',revision=4)})[1]
    def test_prepare_fills_in_who_is_reviewing(self):
        self.assertEqual(self.r['reviewer'],'claude')
    def test_another_name_in_the_report_is_refused_before_anything_is_sent(self):
        self.r['reviewer']='qa-bot'
        with self.assertRaisesRegex(ValueError,"you are acting as 'claude'"):submit(self.api,self.t,self.r,actor='claude')
        self.assertEqual(self.calls,[])
    def test_the_same_name_however_it_is_typed_is_accepted(self):
        self.r['reviewer']='  Claude '
        submit(self.api,self.t,self.r,actor='claude')
        self.assertEqual(self.calls,['/api/tasks/action','/api/tasks/comment'])

class CriteriaNoteTests(unittest.TestCase):
    def test_the_report_warns_when_the_worker_wrote_the_criteria(self):
        t=dict(id='t',revision=1,status='post_validation',acceptance=['Works'],owner='agent-1',criteria_by='Agent-1')
        self.assertIn('also wrote these acceptance criteria',prepare(t,reviewer='qa')['criteria_note'])
    def test_no_warning_when_someone_else_wrote_them(self):
        t=dict(id='t',revision=1,status='post_validation',acceptance=['Works'],owner='agent-1',criteria_by='lead')
        self.assertNotIn('criteria_note',prepare(t,reviewer='qa'))

