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
    result = {}
    for (_, _, identity), values in productivity_references(
        base_path, experience_ids, fact_ids
    ).items():
        if identity in result:
            raise ValueError("sdk_productivity_identity_ambiguous")
        result[identity] = values
    return result


def productivity_references(base_path, experience_ids, fact_ids, *, variant=None):
    """Resolve result types from the recorded variant, or prove a unique legacy match."""
    if variant not in (None, "working", "coding"):
        raise ValueError("sdk_productivity_variant_invalid")
    rows, seen = {}, set()
    for name, short, suffix in (("working", "w", ""), ("coding", "c", "_cv")):
        if variant is not None and name != variant:
            continue
        with existing_database(_variant_db_path(str(base_path), suffix)) as db:
            if db is None:
                continue
            for kind, table, column, identities in (
                ("e", "coding_memory_meta", "experience_id", experience_ids),
                ("f", "working_fact", "fact_id", fact_ids),
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
                    if (kind, identity) in seen:
                        raise ValueError("sdk_productivity_identity_ambiguous")
                    seen.add((kind, identity))
                    rows[short, kind, identity] = (user, agent, session)
    if seen != {("e", identity) for identity in experience_ids} | {
        ("f", identity) for identity in fact_ids
    }:
        raise ValueError("sdk_productivity_reference_missing")
    return rows
