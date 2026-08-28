"""Re-encrypt every PHI column under a new key (Phase 4.1, P0-8).

The procedure this implements, and the reason it exists, are in
docs/runbooks/key-rotation.md. The short version: PHI columns are Fernet
tokens written by app/core/security.py's EncryptedString/EncryptedJSON, and
plain Fernet has no key id in the token, so "rotating the key" means
literally rewriting every encrypted value. There is no cheaper option
available from the application layer — decision 0031 records that as a
known, measured cost rather than a surprise.

Three properties this script is built around, each of which is a way the
obvious version of it destroys data:

1. **Columns are discovered, not listed.** A hand-maintained list of
   encrypted columns is a list that goes stale the first time someone adds
   a model. A column missed by a rotation stays encrypted under a key that
   is about to be deleted, and its contents are then gone permanently. So
   the column set comes from SQLAlchemy metadata, by type.

2. **It reads and writes ciphertext, never plaintext.** Everything goes
   through raw SQL, bypassing the TypeDecorators, so no PHI is ever
   materialised as a Python string here and MultiFernet.rotate preserves
   each token's original timestamp. It also means the script works when
   settings hold neither key.

3. **Verification is a separate pass against the new key ALONE.** A
   rotation that ran without error is not evidence that every row moved;
   only a full read under the new key on its own is. `--verify-only` is
   what the runbook uses to decide it is safe to delete the old key, and
   deleting the old key is the irreversible step.

Resumable by design: rotation is idempotent per value (re-rotating an
already-rotated token just rewrites it), batches commit as they go, and an
interrupted run leaves a database of mixed ciphertext that the application
reads correctly as long as both keys are in PHI_ENCRYPTION_KEY /
PHI_ENCRYPTION_KEY_PREVIOUS.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field

from cryptography.fernet import InvalidToken, MultiFernet
from sqlalchemy import Engine, Table, create_engine, text

# The script lives beside the package rather than inside it, so make the
# package importable when run as `python scripts/rotate_phi_key.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings, secret_fingerprint  # noqa: E402
from app.core.security import EncryptedJSON, EncryptedString, build_phi_cipher  # noqa: E402
from app.db.base import Base  # noqa: E402

_ENCRYPTED_TYPES = (EncryptedString, EncryptedJSON)

# Big enough that per-batch commit overhead disappears, small enough that an
# interrupted run has re-done at most this much work and that no single
# transaction holds row locks across a whole table. Measured: see the
# runbook's timing table.
DEFAULT_BATCH_SIZE = 500


@dataclass
class TableResult:
    table: str
    columns: list[str]
    rows_scanned: int = 0
    values_rewritten: int = 0
    values_null: int = 0
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)


def encrypted_columns() -> dict[Table, list[str]]:
    """Every column in the schema whose type is one of ours, found by type.

    Importing app.models is what populates Base.metadata — models register
    on import, and app/models/__init__.py is the module that imports them
    all (the same contract alembic/env.py relies on). Without it this
    returns an empty mapping and the script would cheerfully report a
    successful rotation of nothing.
    """
    import app.models  # noqa: F401  - import for the side effect of registering models

    found: dict[Table, list[str]] = {}
    for table in Base.metadata.sorted_tables:
        names = [c.name for c in table.columns if isinstance(c.type, _ENCRYPTED_TYPES)]
        if names:
            found[table] = names
    return found


def _quoted(engine: Engine, name: str) -> str:
    return engine.dialect.identifier_preparer.quote(name)


def _primary_key(table: Table) -> str:
    """Rotation pages by primary key rather than OFFSET: an OFFSET-paged
    scan over a table being UPDATE-ed as it is read can skip rows, and a
    skipped row is a row left under the retired key.
    """
    pk = list(table.primary_key.columns)
    if len(pk) != 1:
        raise SystemExit(
            f"{table.name} has a composite primary key; rotation pages by a single "
            "key column and would need extending before it can touch this table."
        )
    return pk[0].name


def rotate_table(
    engine: Engine,
    table: Table,
    columns: list[str],
    cipher: MultiFernet,
    *,
    batch_size: int,
    dry_run: bool,
) -> TableResult:
    """Rewrite every value of `columns` in `table` under `cipher`'s first key."""
    result = TableResult(table=table.name, columns=list(columns))
    pk = _primary_key(table)
    qpk = _quoted(engine, pk)
    qtable = _quoted(engine, table.name)
    qcols = [_quoted(engine, c) for c in columns]

    select_sql = text(
        f"SELECT {qpk}, {', '.join(qcols)} FROM {qtable} "  # noqa: S608 - identifiers come from our own metadata, quoted by the dialect
        f"WHERE {qpk} > :last ORDER BY {qpk} LIMIT :limit"
    )
    update_sql = text(
        f"UPDATE {qtable} SET {', '.join(f'{c} = :{n}' for c, n in zip(qcols, columns))} "  # noqa: S608 - as above
        f"WHERE {qpk} = :pk"
    )

    started = time.perf_counter()
    # "" sorts before every uuid string and every non-empty text key. Ids in
    # this schema are uuid4 strings (app/models/*), so a text comparison is
    # a total order over them.
    last: str = ""
    while True:
        with engine.begin() as conn:
            rows = conn.execute(select_sql, {"last": last, "limit": batch_size}).all()
            if not rows:
                break
            for row in rows:
                result.rows_scanned += 1
                last = row[0]
                updates: dict[str, str] = {}
                for name, value in zip(columns, row[1:]):
                    if value is None:
                        result.values_null += 1
                        continue
                    try:
                        updates[name] = cipher.rotate(value.encode()).decode()
                    except InvalidToken:
                        # Loud and specific: this value is readable by none
                        # of the supplied keys. Continuing would let the run
                        # end "successfully" with unreadable rows left behind.
                        result.failures.append(f"{table.name}.{name} id={row[0]}")
                        continue
                if updates and not dry_run:
                    conn.execute(update_sql, {**updates, "pk": row[0]})
                result.values_rewritten += len(updates)

    result.seconds = time.perf_counter() - started
    return result


def verify_table(
    engine: Engine,
    table: Table,
    columns: list[str],
    cipher: MultiFernet,
    *,
    batch_size: int,
) -> TableResult:
    """Prove every value reads under `cipher` — which the caller builds from
    the NEW key alone. This is the only evidence that retiring the old key
    is safe.
    """
    result = TableResult(table=table.name, columns=list(columns))
    pk = _primary_key(table)
    qpk = _quoted(engine, pk)
    select_sql = text(
        f"SELECT {qpk}, {', '.join(_quoted(engine, c) for c in columns)} "  # noqa: S608 - identifiers from our own metadata
        f"FROM {_quoted(engine, table.name)} WHERE {qpk} > :last ORDER BY {qpk} LIMIT :limit"
    )

    started = time.perf_counter()
    last: str = ""
    with engine.connect() as conn:
        while True:
            rows = conn.execute(select_sql, {"last": last, "limit": batch_size}).all()
            if not rows:
                break
            for row in rows:
                result.rows_scanned += 1
                last = row[0]
                for name, value in zip(columns, row[1:]):
                    if value is None:
                        result.values_null += 1
                        continue
                    try:
                        cipher.decrypt(value.encode())
                        result.values_rewritten += 1
                    except InvalidToken:
                        result.failures.append(f"{table.name}.{name} id={row[0]}")

    result.seconds = time.perf_counter() - started
    return result


def _resolve_keys(args: argparse.Namespace) -> tuple[str, list[str]]:
    """New key from the CLI or PHI_ENCRYPTION_KEY_NEW; old keys default to
    whatever the app is currently configured with, so the common case is
    `--new-key <k>` and nothing else.
    """
    new_key = args.new_key or os.environ.get("PHI_ENCRYPTION_KEY_NEW")
    if not new_key:
        raise SystemExit(
            "No new key. Pass --new-key, or set PHI_ENCRYPTION_KEY_NEW. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )

    if args.old_key:
        old_keys = list(args.old_key)
    else:
        settings = get_settings()
        old_keys = [k for k in [settings.phi_encryption_key, *settings.phi_previous_key_list] if k]
    old_keys = [k for k in old_keys if k != new_key]
    return new_key, old_keys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-encrypt every PHI column under a new Fernet key.",
        epilog="Procedure and preconditions: docs/runbooks/key-rotation.md",
    )
    parser.add_argument("--new-key", help="The key to encrypt with (default: $PHI_ENCRYPTION_KEY_NEW)")
    parser.add_argument(
        "--old-key",
        action="append",
        help="A key that may decrypt existing rows; repeatable. "
        "Defaults to the app's configured current + previous keys.",
    )
    parser.add_argument("--database-url", help="Override DATABASE_URL for this run.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and re-encrypt in memory but write nothing — proves every "
        "value is decryptable and gives a true timing, without changing data.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip rotation; assert every value already reads under the new key alone.",
    )
    args = parser.parse_args(argv)

    new_key, old_keys = _resolve_keys(args)
    db_url = args.database_url or get_settings().database_url
    engine = create_engine(db_url, pool_pre_ping=True)

    try:
        return _run(args, engine, new_key, old_keys)
    finally:
        # Explicitly, because this is a script: a pooled connection left
        # open holds a lock the next tool to touch this database will wait
        # on (and on SQLite, blocks a schema change outright).
        engine.dispose()


def _run(args: argparse.Namespace, engine: Engine, new_key: str, old_keys: list[str]) -> int:
    tables = encrypted_columns()
    if not tables:
        raise SystemExit("Found no encrypted columns — refusing to report a rotation of nothing.")

    # Fingerprints, never keys. This output goes into a change record, and a
    # change record is not a place to write key material.
    print(f"database:  {engine.url.render_as_string(hide_password=True)}")
    print(f"new key:   {secret_fingerprint(new_key)}")
    print(f"old keys:  {', '.join(secret_fingerprint(k) for k in old_keys) or '(none)'}")
    total_columns = sum(len(c) for c in tables.values())
    print(f"columns:   {total_columns} across {len(tables)} tables\n")

    # Rotation needs both keys; verification must use the new key ALONE, or
    # it would pass on rows that never moved.
    rotate_cipher = build_phi_cipher(new_key, old_keys)
    verify_cipher = build_phi_cipher(new_key)

    results: list[TableResult] = []
    for table, columns in tables.items():
        if args.verify_only:
            res = verify_table(engine, table, columns, verify_cipher, batch_size=args.batch_size)
            verb = "verified"
        else:
            res = rotate_table(
                engine,
                table,
                columns,
                rotate_cipher,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
            )
            verb = "would rewrite" if args.dry_run else "rewrote"
        results.append(res)
        print(
            f"{res.table:<22} {res.rows_scanned:>7} rows  "
            f"{verb} {res.values_rewritten:>7} values  "
            f"({', '.join(res.columns)})  {res.seconds:8.3f}s"
        )

    total_rows = sum(r.rows_scanned for r in results)
    total_values = sum(r.values_rewritten for r in results)
    total_seconds = sum(r.seconds for r in results)
    failures = [f for r in results for f in r.failures]

    print(
        f"\ntotal: {total_rows} rows, {total_values} values, {total_seconds:.3f}s"
        + (f" ({total_values / total_seconds:,.0f} values/s)" if total_seconds else "")
    )

    if failures:
        print(f"\nFAILED: {len(failures)} value(s) no key could decrypt:", file=sys.stderr)
        for f in failures[:20]:
            print(f"  {f}", file=sys.stderr)
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\nDry run: nothing was written.")
    elif not args.verify_only:
        print(
            "\nNow: set PHI_ENCRYPTION_KEY to the new key, keep the old one in "
            "PHI_ENCRYPTION_KEY_PREVIOUS, restart, then re-run with --verify-only "
            "before deleting the old key anywhere."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
