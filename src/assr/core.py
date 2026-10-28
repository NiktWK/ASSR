"""Transactional question routing. This module does not execute LLMs or jobs."""
import json
import sqlite3
from contextlib import contextmanager

PARENT = {'developer': 'k2', 'runner': 'k2', 'k2': 'researcher', 'researcher': 'k1', 'k1': 'human'}

class Conflict(ValueError):
    pass

class Coordinator:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, role TEXT, state TEXT, phase TEXT, plan TEXT);
        CREATE TABLE IF NOT EXISTS questions(id TEXT PRIMARY KEY, task TEXT, recipient TEXT, revision INTEGER, state TEXT, body TEXT, answer TEXT);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, kind TEXT, body TEXT);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def emit(self, kind, **body):
        self.db.execute('INSERT INTO events(kind,body) VALUES (?,?)', (kind, json.dumps(body)))

    def create(self, task, role):
        if role not in PARENT:
            raise ValueError('unknown role')
        with self.transaction():
            self.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', (task, role, 'READY', 'PLAN', None))
            self.emit('task.created', task=task, role=role)

    def ask(self, question, task, text):
        if not text.strip():
            raise ValueError('empty question')
        with self.transaction():
            old = self.db.execute('SELECT task,body FROM questions WHERE id=?', (question,)).fetchone()
            if old:
                if old != (task, text):
                    raise Conflict('id reused with different content')
                return
            row = self.db.execute('SELECT role,state FROM tasks WHERE id=?', (task,)).fetchone()
            if not row or row[1] == 'CANCELLED':
                raise Conflict('inactive task')
            recipient = PARENT[row[0]]
            self.db.execute('INSERT INTO questions VALUES (?,?,?,?,?,?,?)', (question, task, recipient, 1, 'OPEN', text, None))
            self.db.execute("UPDATE tasks SET phase='PLAN',state='WAITING_RESPONSE',plan=NULL WHERE id=?", (task,))
            self.emit('question.created', question=question, recipient=recipient, notification='ACTION_REQUIRED' if recipient == 'human' else 'FYI')

    def resolve(self, question, actor, revision, answer=None):
        """answer=None escalates; human can never escalate or implicitly approve."""
        with self.transaction():
            row = self.db.execute('SELECT task,recipient,revision,state FROM questions WHERE id=?', (question,)).fetchone()
            if not row or row[1:] != (actor, revision, 'OPEN'):
                raise Conflict('stale question or wrong recipient')
            task = row[0]
            if answer is None:
                if actor not in PARENT:
                    raise Conflict('human answer required')
                recipient = PARENT[actor]
                self.db.execute('UPDATE questions SET recipient=?,revision=revision+1 WHERE id=?', (recipient, question))
                self.emit('question.escalated', question=question, sender=actor, recipient=recipient, notification='ACTION_REQUIRED' if recipient == 'human' else 'FYI')
            else:
                if not answer.strip():
                    raise ValueError('empty answer')
                self.db.execute("UPDATE questions SET state='ANSWERED',answer=?,revision=revision+1 WHERE id=?", (answer, question))
                pending = self.db.execute("SELECT count(*) FROM questions WHERE task=? AND state='OPEN'", (task,)).fetchone()[0]
                if not pending:
                    self.db.execute("UPDATE tasks SET state='READY' WHERE id=?", (task,))
                self.emit('question.answered', question=question, actor=actor, answer=answer, notification='FYI')

    def cancel(self, task):
        with self.transaction():
            self.db.execute("UPDATE tasks SET state='CANCELLED' WHERE id=?", (task,))
            self.db.execute("UPDATE questions SET state='CANCELLED',revision=revision+1 WHERE task=? AND state='OPEN'", (task,))
            self.emit('task.cancelled', task=task)

    def history(self):
        return [(seq, kind, json.loads(body)) for seq, kind, body in self.db.execute('SELECT * FROM events ORDER BY seq')]
