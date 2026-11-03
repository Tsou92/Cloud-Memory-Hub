"""Shared bounded, immutable SQL archive for internally named evidence tables."""

import re

from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.reconcile_evidence import MAX_BYTES, digest, encode
from cloud_memory_hub.remote_access import AccessError


class EvidenceArchive:
    def __init__(self, name, schema, table, validate):
        if any(re.fullmatch(r"[a-z_]+", value) is None for value in (name, schema, table)):
            raise ValueError("evidence_archive_name_invalid")
        self.name, self.schema, self.table, self.validate = name, schema, table, validate

    def initialize(self, db):
        if (
            db.execute("SELECT name FROM sqlite_master WHERE name=?", (self.schema,)).fetchone()
            is None
        ):
            if db.execute("SELECT name FROM sqlite_master WHERE name=?", (self.table,)).fetchone():
                raise AccessError(f"lineage_{self.name}_store_unrecognized", 503)
            db.execute(f"CREATE TABLE {self.schema}(id INTEGER PRIMARY KEY,version INTEGER)")
            db.execute(f"INSERT INTO {self.schema} VALUES (1,1)")
            db.execute(
                f"CREATE TABLE {self.table}(receipt_id TEXT PRIMARY KEY "
                "REFERENCES cmh_event_outcomes(receipt_id), "
                f"{self.name}_json TEXT NOT NULL, {self.name}_digest TEXT NOT NULL)"
            )
        self.check(db)

    def check(self, db):
        if [tuple(row) for row in db.execute(f"SELECT id,version FROM {self.schema}")] != [(1, 1)]:
            raise AccessError(f"lineage_{self.name}_store_version_unsupported", 503)
        db.execute(
            f"SELECT receipt_id,{self.name}_json,{self.name}_digest FROM {self.table} LIMIT 0"
        )

    def stored(self, db, receipt, request):
        row = db.execute(
            f"SELECT CASE WHEN length(CAST({self.name}_json AS BLOB))<=? "
            f"THEN {self.name}_json END,{self.name}_digest FROM {self.table} WHERE receipt_id=?",
            (MAX_BYTES, receipt),
        ).fetchone()
        if row is None:
            return None
        if row[0] is None or digest(row[0]) != row[1]:
            raise ValueError(f"lineage_{self.name}_digest_invalid")
        value = self.validate(parse_json(row[0]), request)
        if encode(value) != row[0]:
            raise ValueError(f"lineage_{self.name}_encoding_invalid")
        return value

    def commit(self, db, receipt, value, request):
        self.validate(value, request)
        encoded = encode(value)
        if len(encoded.encode()) > MAX_BYTES:
            raise ValueError(f"lineage_{self.name}_limit")
        previous = self.stored(db, receipt, request)
        if previous is not None:
            if previous != value:
                raise ValueError(f"lineage_{self.name}_changed")
            return
        db.execute(f"INSERT INTO {self.table} VALUES (?,?,?)", (receipt, encoded, digest(encoded)))
