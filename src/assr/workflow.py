"""Durable dispatch and plan admission, independent of model output prose."""
import hashlib
import json
from .core import Coordinator, Conflict, PARENT

class Workflow(Coordinator):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS sessions(task TEXT PRIMARY KEY, remote TEXT UNIQUE, status TEXT);
        CREATE TABLE IF NOT EXISTS dispatch(id INTEGER PRIMARY KEY, task TEXT, profile TEXT, status TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS plans(task TEXT PRIMARY KEY, digest TEXT, body TEXT, approver TEXT);
        ''')

    def propose(self, task, plan):
        if not plan.strip(): raise ValueError('empty plan')
        fingerprint = hashlib.sha256(plan.encode()).hexdigest()
        with self.transaction():
            row = self.db.execute('SELECT state,phase FROM tasks WHERE id=?',(task,)).fetchone()
            if row != ('READY','PLAN'): raise Conflict('task not ready to plan')
            self.db.execute('INSERT OR REPLACE INTO plans VALUES (?,?,?,NULL)',(task,fingerprint,plan))
            self.db.execute('UPDATE tasks SET plan=? WHERE id=?',(fingerprint,task))
            self.emit('plan.proposed', task=task, digest=fingerprint)
        return fingerprint

    def admit(self, task, fingerprint, actor):
        with self.transaction():
            row = self.db.execute('SELECT role,state,phase,plan FROM tasks WHERE id=?',(task,)).fetchone()
            if not row or row[1:] != ('READY','PLAN',fingerprint): raise Conflict('stale or blocked plan')
            if actor != PARENT[row[0]]: raise Conflict('wrong approver')
            pending = self.db.execute("SELECT count(*) FROM questions WHERE task=? AND state='OPEN'",(task,)).fetchone()[0]
            if pending: raise Conflict('unanswered question')
            self.db.execute('UPDATE plans SET approver=? WHERE task=? AND digest=?',(actor,task,fingerprint))
            self.db.execute("UPDATE tasks SET phase='BUILD' WHERE id=?",(task,))
            self.emit('plan.admitted', task=task,digest=fingerprint,actor=actor)

    def step(self, task, runtime, text):
        # Intent survives a lost response. UNKNOWN is not automatically replayed.
        with self.transaction():
            row = self.db.execute('SELECT role,state,phase FROM tasks WHERE id=?',(task,)).fetchone()
            if not row or row[1] != 'READY': raise Conflict('task not ready')
            if self.db.execute("SELECT 1 FROM dispatch WHERE task=? AND status IN ('STARTED','UNKNOWN')",(task,)).fetchone():
                raise Conflict('reconcile previous dispatch first')
            session = self.db.execute('SELECT remote,status FROM sessions WHERE task=?',(task,)).fetchone()
            if session and session[1] != 'READY': raise Conflict('reconcile session creation first')
            if not session:
                self.db.execute("INSERT INTO sessions VALUES (?,NULL,'CREATING')",(task,))
            profile = row[0] + '-' + row[2].lower()
            did = self.db.execute("INSERT INTO dispatch(task,profile,status) VALUES (?,?,'STARTED')",(task,profile)).lastrowid
            self.db.execute("UPDATE tasks SET state='RUNNING' WHERE id=?",(task,))
        try:
            sid = session[0] if session else runtime.create('ASSR ' + task)
            if not session:
                with self.transaction():
                    self.db.execute("UPDATE sessions SET remote=?,status='READY' WHERE task=?",(sid,task))
            result = runtime.prompt(sid,profile,text)
        except Exception:
            with self.transaction():
                self.db.execute("UPDATE dispatch SET status='UNKNOWN' WHERE id=?",(did,))
                self.db.execute("UPDATE tasks SET state='BLOCKED' WHERE id=? AND state='RUNNING'",(task,))
                self.emit('dispatch.unknown',task=task,dispatch=did)
            raise
        with self.transaction():
            self.db.execute("UPDATE dispatch SET status='DONE',result=? WHERE id=?",(json.dumps(result),did))
            # A question or cancellation delivered during I/O wins.
            self.db.execute("UPDATE tasks SET state='READY' WHERE id=? AND state='RUNNING'",(task,))
            self.emit('dispatch.completed',task=task,dispatch=did,profile=profile)
        return result
