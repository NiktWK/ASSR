import tempfile
import unittest
from pathlib import Path
from assr.core import Coordinator, Conflict

class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.c = Coordinator(self.path)
        self.c.create('task', 'developer')

    def tearDown(self):
        self.c.db.close()
        self.tmp.cleanup()

    def test_chain_restart_and_answer(self):
        self.c.ask('q','task','Which approach?')
        self.c.resolve('q','k2',1)
        self.c.db.close()
        self.c = Coordinator(self.path)
        self.c.resolve('q','researcher',2)
        self.c.resolve('q','k1',3)
        self.assertEqual(self.c.history()[-1][2]['notification'], 'ACTION_REQUIRED')
        with self.assertRaises(Conflict): self.c.resolve('q','human',4)
        self.c.resolve('q','human',4,'Use baseline')
        self.assertEqual(self.c.db.execute('SELECT state,phase FROM tasks').fetchone(), ('READY','PLAN'))

    def test_duplicate_and_stale(self):
        self.c.ask('q','task','Question')
        self.c.ask('q','task','Question')
        self.assertEqual(len(self.c.history()),2)
        with self.assertRaises(Conflict): self.c.ask('q','task','Other')
        with self.assertRaises(Conflict): self.c.resolve('q','researcher',1,'Answer')
        self.c.resolve('q','k2',1,'Answer')
        with self.assertRaises(Conflict): self.c.resolve('q','k2',1,'Late')

    def test_cancel_and_multiple_questions(self):
        self.c.ask('q','task','First')
        self.c.ask('q2','task','Second')
        self.c.resolve('q','k2',1,'Answer')
        self.assertEqual(self.c.db.execute('SELECT state FROM tasks').fetchone()[0],'WAITING_RESPONSE')
        self.c.cancel('task')
        with self.assertRaises(Conflict): self.c.resolve('q2','k2',1,'Late')
        self.assertEqual(self.c.db.execute('SELECT state FROM tasks').fetchone()[0],'CANCELLED')
