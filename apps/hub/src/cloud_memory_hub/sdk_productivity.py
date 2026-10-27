"""Read identities in the pinned SDK's separate productivity stores, without writers."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from hy_memory.coding.store import _variant_db_path


@contextmanager
def existing_database(path, *, cross_thread=False):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("sdk_productivity_path_invalid")
    try:
        path.stat()
    except FileNotFoundError:
        yield None
        return
    # mode=ro does not create a database or run upstream initialize/migrations.
    with closing(
        sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=0.2, check_same_thread=not cross_thread
        )
    ) as db:
        db.execute("PRAGMA query_only=ON")
        yield db


def productivity_identities(base_path, experience_ids, fact_ids):
    """Require exactly one row per ID across both variants; return identity fields only."""
    rows = {}
    for suffix in ("", "_cv"):
        with existing_database(_variant_db_path(str(base_path), suffix)) as db:
            if db is None:
                continue
            for table, column, identities in (
                ("coding_memory_meta", "experience_id", experience_ids),
                ("working_fact", "fact_id", fact_ids),
            ):
                if not identities:
                    continue
                placeholders = ",".join("?" for _ in identities)
                found = db.execute(
                    f"SELECT {column},user_id,agent_id,session_id FROM {table} "
                    f"WHERE {column} IN ({placeholders})",
                    identities,
                ).fetchall()
                for identity, user, agent, session in found:
                    if identity in rows:
                        raise ValueError("sdk_productivity_identity_ambiguous")
                    rows[identity] = (user, agent, session)
    if set(rows) != set(experience_ids) | set(fact_ids):
        raise ValueError("sdk_productivity_reference_missing")
    return rows


def has_productivity_data(base_path, namespace):
    """Guard an inventory which cannot yet represent productivity/trace lineage."""
    for suffix, tables in (
        ("", ("coding_memory_meta", "working_fact")),
        ("_cv", ("coding_memory_meta", "working_fact")),
        ("_trace", ("coding_trace",)),
    ):
        with existing_database(_variant_db_path(str(base_path), suffix)) as db:
            if db is None:
                continue
            for table in tables:
                if db.execute(
                    f"SELECT 1 FROM {table} WHERE user_id=? LIMIT 1", (namespace,)
                ).fetchone():
                    return True
    return False
