"""Snapshots deliberately removed by a governance purge (ADR-0032)."""


def purged(db, prefix, receipt):
    try:
        return (
            db.execute(
                "SELECT 1 FROM cmh_purged_snapshots WHERE prefix=? AND receipt_id=?",
                (prefix, receipt),
            ).fetchone()
            is not None
        )
    except Exception:
        # Older stores without the table have purged nothing.
        return False
