"""Transactional snapshots and append-only audit journal. PostgreSQL in deployment."""

import json
from contextlib import contextmanager

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, select, update
from sqlalchemy.pool import StaticPool

from .models import now, uid

metadata = MetaData()
campaigns = Table(
    "campaigns",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("body", Text, nullable=False),
)
events = Table(
    "events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("campaign_id", String(36), index=True),
    Column("body", Text, nullable=False),
)
requests = Table(
    "requests",
    metadata,
    Column("key", String(180), primary_key=True),
    Column("fingerprint", String(64)),
    Column("response", Text),
)
locks = Table("locks", metadata, Column("id", Integer, primary_key=True), Column("value", Integer))


class Conflict(Exception):
    pass


class Store:
    def __init__(self, url: str):
        opts = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        if url == "sqlite://":
            opts["poolclass"] = StaticPool
        self.engine = create_engine(url, **opts)
        metadata.create_all(self.engine)
        with self.engine.begin() as c:
            if c.execute(select(locks.c.id)).first() is None:
                c.execute(locks.insert().values(id=1, value=0))

    @contextmanager
    def transaction(self):
        # Serialize mutations globally (MVP: one campaign/worker), including SQLite.
        with self.engine.begin() as c:
            c.execute(update(locks).where(locks.c.id == 1).values(value=locks.c.value + 1))
            yield c

    def get(self, cid, c=None):
        if c is None:
            with self.engine.connect() as conn:
                return self.get(cid, conn)
        row = c.execute(select(campaigns.c.body).where(campaigns.c.id == cid)).first()
        if row is None:
            raise KeyError(cid)
        return json.loads(row[0])

    def list(self):
        with self.engine.connect() as c:
            return [json.loads(x[0]) for x in c.execute(select(campaigns.c.body))]

    def save(self, c, data, expected):
        data["revision"] = expected + 1
        result = c.execute(
            update(campaigns)
            .where(campaigns.c.id == data["id"], campaigns.c.revision == expected)
            .values(revision=data["revision"], body=json.dumps(data, allow_nan=False))
        )
        if result.rowcount != 1:
            raise Conflict("stale revision")

    def insert(self, c, data):
        c.execute(
            campaigns.insert().values(
                id=data["id"], revision=data["revision"], body=json.dumps(data, allow_nan=False)
            )
        )

    def event(self, c, data, kind, payload, sender="orchestrator", recipient="human"):
        envelope = {
            "message_id": uid(),
            "schema_version": 1,
            "campaign_id": data["id"],
            "sender": sender,
            "recipient": recipient,
            "type": kind,
            "correlation_id": data.get("experiment", {}).get("experiment_id", data["id"]),
            "causation_id": data.get("last_event_id"),
            "target_revision": data["revision"],
            "created_at": now(),
            "payload": payload,
        }
        data["last_event_id"] = envelope["message_id"]
        c.execute(events.insert().values(campaign_id=data["id"], body=json.dumps(envelope)))

    def history(self, cid):
        with self.engine.connect() as c:
            return [
                json.loads(x[0])
                for x in c.execute(
                    select(events.c.body).where(events.c.campaign_id == cid).order_by(events.c.seq)
                )
            ]

    def replay(self, c, key, fingerprint):
        row = c.execute(select(requests).where(requests.c.key == key)).mappings().first()
        if row:
            if row["fingerprint"] != fingerprint:
                raise Conflict("idempotency key reused with different payload")
            return json.loads(row["response"])

    def remember(self, c, key, fingerprint, response):
        c.execute(requests.insert().values(key=key, fingerprint=fingerprint, response=json.dumps(response)))
