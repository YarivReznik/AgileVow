import unittest
from agilevow.review import prepare, verdict

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
