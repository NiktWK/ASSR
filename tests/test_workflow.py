import tempfile
import unittest
from pathlib import Path
from assr.workflow import Workflow
from assr.core import Conflict

class Runtime:
    def __init__(self): self.calls=[]
    def create(self,title): self.calls.append(('create',title)); return 'session-one'
    def prompt(self,sid,profile,text): self.calls.append((sid,profile,text)); return {'parts':[]}

class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'db'
        self.w=Workflow(self.path); self.w.create('t','developer');self.r=Runtime()
    def tearDown(self): self.w.db.close();self.tmp.cleanup()
    def test_plan_question_answer_build_preserves_session(self):
        self.w.step('t',self.r,'Plan')
        self.w.ask('q','t','Which method?')
        with self.assertRaises(Conflict):self.w.step('t',self.r,'Cannot run')
        self.w.resolve('q','k2',1,'Method A')
        digest=self.w.propose('t','Implement method A')
        self.w.admit('t',digest,'k2')
        self.w.db.close();self.w=Workflow(self.path)
        self.w.step('t',self.r,'Execute approved plan')
        self.assertEqual(self.r.calls[-1][1],'developer-build')
        self.assertEqual(sum(c[0]=='create' for c in self.r.calls),1)
    def test_new_question_invalidates_admission(self):
        digest=self.w.propose('t','Plan')
        self.w.ask('q','t','Question')
        self.w.resolve('q','k2',1,'Answer')
        with self.assertRaises(Conflict):self.w.admit('t',digest,'k2')
    def test_unknown_is_not_retried(self):
        def broken(*args): raise TimeoutError()
        self.r.prompt=broken
        with self.assertRaises(TimeoutError):self.w.step('t',self.r,'Plan')
        with self.assertRaises(Conflict):self.w.step('t',self.r,'Retry')
    def test_cancel_during_io_wins(self):
        def cancel(*args):self.w.cancel('t');return {}
        self.r.prompt=cancel
        self.w.step('t',self.r,'Plan')
        self.assertEqual(self.w.db.execute('SELECT state FROM tasks').fetchone()[0],'CANCELLED')
