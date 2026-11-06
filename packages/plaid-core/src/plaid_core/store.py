"""Encrypted application records and atomic synchronization in SQLite."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

import simplejson
from cryptography.fernet import Fernet, InvalidToken

from .errors import ConcurrentSyncError, FinanceError
from .models import (
    DTO,
    Account,
    AccountsSnapshot,
    Connection,
    Holdings,
    Liabilities,
    LinkState,
    SnapshotInfo,
    SnapshotQuery,
    SnapshotRecord,
    Transaction,
)

T = TypeVar("T", bound=DTO)

SCHEMA = """
CREATE TABLE connections (
    id TEXT PRIMARY KEY,
    payload BLOB NOT NULL,
    token BLOB NOT NULL,
    cursor TEXT,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE accounts (
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    fetched_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (connection_id, account_id)
);
CREATE TABLE transactions (
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('bank', 'investment')),
    transaction_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    source_payload BLOB NOT NULL,
    PRIMARY KEY (connection_id, source_kind, transaction_id)
);
CREATE INDEX transactions_account ON transactions(connection_id, account_id);
CREATE TABLE snapshots (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('balances', 'holdings', 'liabilities')),
    fetched_at TEXT NOT NULL,
    payload BLOB NOT NULL,
    source_payload BLOB NOT NULL
);
CREATE INDEX snapshots_latest ON snapshots(connection_id, kind, fetched_at DESC, id DESC);
PRAGMA user_version = 1;
CREATE TABLE link_sessions (
    id TEXT PRIMARY KEY,
    payload BLOB NOT NULL
);
"""


@dataclass(frozen=True)
class StoredConnection:
    public: Connection
    token: str = field(repr=False)
    cursor: str | None
    revision: int


def private_write(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


def initialize_store(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    database, key = root / "finance.sqlite3", root / "encryption.key"
    if database.exists() or key.exists():
        # Existing stores are validated, never regenerated or overwritten.
        Store(root)
        return
    private_write(key, Fernet.generate_key())
    private_write(database, b"")
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)


class Store:
    def __init__(self, root: Path):
        self.database = root / "finance.sqlite3"
        key = root / "encryption.key"
        if not self.database.is_file() or not key.is_file():
            raise FinanceError("Storage is not initialized or its encryption key is missing")
        try:
            self.cipher = Fernet(key.read_bytes())
        except (ValueError, OSError) as exc:
            raise FinanceError("Cannot load the storage encryption key") from exc
        with self.connect() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise FinanceError("Unsupported database schema version")

    @contextmanager
    def connect(self):
        # mode=rw prevents a read from accidentally creating a new empty database.
        conn = sqlite3.connect(self.database.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def seal(self, value: DTO) -> bytes:
        return self.cipher.encrypt(value.model_dump_json(round_trip=True).encode())

    def seal_source(self, value: dict) -> bytes:
        # Decimal stays a JSON number, preserving the provider's numeric values.
        return self.cipher.encrypt(simplejson.dumps(value, use_decimal=True).encode())

    def unseal_source(self, value: bytes) -> dict:
        try:
            return simplejson.loads(self.cipher.decrypt(value), use_decimal=True)
        except (InvalidToken, ValueError) as exc:
            raise FinanceError("Cannot decrypt the original provider payload") from exc

    def unseal(self, value: bytes, model: type[T]) -> T:
        try:
            return model.model_validate_json(self.cipher.decrypt(value))
        except (InvalidToken, ValueError) as exc:
            raise FinanceError("Cannot decrypt or validate stored financial data") from exc

    def add_connection(self, value: Connection, token: str) -> None:
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO connections (id, payload, token) VALUES (?, ?, ?)",
                    (value.id, self.seal(value), self.cipher.encrypt(token.encode())),
                )
        except sqlite3.IntegrityError as exc:
            raise FinanceError("That connection already exists; use Link reauthorization") from exc

    def connections(self) -> list[Connection]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM connections ORDER BY id").fetchall()
        return [self.unseal(row[0], Connection) for row in rows]

    def connection(self, connection_id: str) -> StoredConnection:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
        if row is None:
            raise FinanceError("Connection not found in this environment")
        try:
            token = self.cipher.decrypt(row["token"]).decode()
        except (InvalidToken, ValueError) as exc:
            raise FinanceError("Cannot decrypt the connection token") from exc
        return StoredConnection(
            self.unseal(row["payload"], Connection), token, row["cursor"], row["revision"]
        )

    def remove_connection(self, connection_id: str) -> None:
        with self.connect() as conn:
            if conn.execute("DELETE FROM connections WHERE id=?", (connection_id,)).rowcount != 1:
                raise FinanceError("Connection not found in this environment")

    def accounts(
        self, connection_id: str | None = None, *, include_inactive=False
    ) -> list[Account]:
        if connection_id:
            self.connection(connection_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM accounts WHERE (? IS NULL OR connection_id=?) AND (active=1 OR ?) ORDER BY connection_id, account_id",
                (connection_id, connection_id, include_inactive),
            ).fetchall()
        return [self.unseal(row[0], Account) for row in rows]

    def transactions(
        self, connection_id: str | None = None, *, include_details=False
    ) -> list[Transaction]:
        if connection_id:
            self.connection(connection_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT t.payload, t.source_payload, a.payload account_payload, c.payload connection_payload FROM transactions t JOIN connections c ON c.id=t.connection_id LEFT JOIN accounts a ON a.connection_id=t.connection_id AND a.account_id=t.account_id WHERE (? IS NULL OR t.connection_id=?) ORDER BY t.connection_id, t.transaction_id",
                (connection_id, connection_id),
            ).fetchall()
        result = []
        for row in rows:
            value = self.unseal(row["payload"], Transaction)
            account = (
                self.unseal(row["account_payload"], Account) if row["account_payload"] else None
            )
            connection = self.unseal(row["connection_payload"], Connection)
            value = value.model_copy(
                update={
                    "account_name": account.name if account else None,
                    "account_mask": account.mask if account else None,
                    "institution": connection.institution,
                }
            )
            if include_details:
                value = value.model_copy(
                    update={"details": self.unseal_source(row["source_payload"])}
                )
            result.append(value)
        return result

    def _save_snapshot(self, conn, kind, snapshot, source):
        identity = str(uuid4())
        conn.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (
                identity,
                snapshot.connection_id,
                kind,
                snapshot.fetched_at.isoformat(timespec="microseconds"),
                self.seal(snapshot),
                self.seal_source(source),
            ),
        )
        if kind == "balances" and snapshot.complete:
            conn.execute(
                "UPDATE accounts SET active=0, fetched_at=? WHERE connection_id=? AND fetched_at<=?",
                (
                    snapshot.fetched_at.isoformat(timespec="microseconds"),
                    snapshot.connection_id,
                    snapshot.fetched_at.isoformat(timespec="microseconds"),
                ),
            )
        conn.executemany(
            "INSERT INTO accounts (connection_id, account_id, payload, fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(connection_id, account_id) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at, active=1 WHERE excluded.fetched_at>=accounts.fetched_at",
            [
                (
                    snapshot.connection_id,
                    record.id,
                    self.seal(record),
                    snapshot.fetched_at.isoformat(timespec="microseconds"),
                )
                for record in snapshot.accounts
            ],
        )
        return SnapshotInfo(
            id=identity,
            connection_id=snapshot.connection_id,
            kind=kind,
            fetched_at=snapshot.fetched_at,
        )

    def add_snapshot(self, kind: str, snapshot: DTO, source: dict) -> SnapshotInfo:
        with self.connect() as conn:
            return self._save_snapshot(conn, kind, snapshot, source)

    def latest_snapshots(
        self, kind: str, model: type[T], connection_id: str | None = None
    ) -> list[T]:
        if connection_id:
            self.connection(connection_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM (SELECT payload, connection_id, ROW_NUMBER() OVER (PARTITION BY connection_id ORDER BY fetched_at DESC, rowid DESC) rank FROM snapshots WHERE kind=? AND (? IS NULL OR connection_id=?)) WHERE rank=1 ORDER BY connection_id",
                (kind, connection_id, connection_id),
            ).fetchall()
        return [self.unseal(row[0], model) for row in rows]

    def snapshots(
        self, *, connection_id=None, kind=None, limit=100, offset=0
    ) -> list[SnapshotInfo]:
        if (
            kind not in {None, "balances", "holdings", "liabilities"}
            or not 1 <= limit <= 1000
            or offset < 0
        ):
            raise FinanceError("Invalid snapshot kind or pagination")
        if connection_id:
            self.connection(connection_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, connection_id, kind, fetched_at FROM snapshots WHERE (? IS NULL OR connection_id=?) AND (? IS NULL OR kind=?) ORDER BY fetched_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (connection_id, connection_id, kind, kind, limit, offset),
            ).fetchall()
        return [SnapshotInfo.model_validate(dict(row)) for row in rows]

    def query_snapshot_infos(self, query: SnapshotQuery) -> tuple[list[SnapshotInfo], int]:
        if query.connection_id:
            self.connection(query.connection_id)
        start = (
            query.start.astimezone(UTC).isoformat(timespec="microseconds") if query.start else None
        )
        end = query.end.astimezone(UTC).isoformat(timespec="microseconds") if query.end else None
        selection = "WHERE (? IS NULL OR connection_id=?) AND (? IS NULL OR kind=?) AND (? IS NULL OR fetched_at>=?) AND (? IS NULL OR fetched_at<=?)"
        parameters = (
            query.connection_id,
            query.connection_id,
            query.kind,
            query.kind,
            start,
            start,
            end,
            end,
        )
        with self.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute(
                "SELECT COUNT(*) FROM snapshots " + selection, parameters
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT id, connection_id, kind, fetched_at FROM snapshots "
                + selection
                + " ORDER BY fetched_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (*parameters, query.limit, query.offset),
            ).fetchall()
        return [SnapshotInfo.model_validate(dict(row)) for row in rows], total

    def snapshot(self, identity: str, *, include_details=False) -> SnapshotRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise FinanceError("Snapshot not found in this environment")
        models = {"balances": AccountsSnapshot, "holdings": Holdings, "liabilities": Liabilities}
        return SnapshotRecord(
            id=row["id"],
            connection_id=row["connection_id"],
            kind=row["kind"],
            fetched_at=row["fetched_at"],
            data=self.unseal(row["payload"], models[row["kind"]]),
            details=self.unseal_source(row["source_payload"]) if include_details else None,
        )

    def commit_sync(
        self,
        original: StoredConnection,
        cursor: str,
        changes: list[tuple[list[tuple[Transaction, dict]], list[str]]],
        balances: list[tuple[AccountsSnapshot, dict]],
        fetched_at: datetime,
    ) -> None:
        connection_id = original.public.id
        updated = original.public.model_copy(update={"last_transaction_sync": fetched_at})
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE connections SET cursor=?, revision=revision+1, payload=? WHERE id=? AND revision=?",
                (cursor, self.seal(updated), connection_id, original.revision),
            )
            if changed.rowcount != 1:
                raise ConcurrentSyncError(
                    "Connection changed during sync; retry the synchronization"
                )
            for records, removed in changes:
                conn.executemany(
                    "INSERT INTO transactions VALUES (?, 'bank', ?, ?, ?, ?) ON CONFLICT(connection_id, source_kind, transaction_id) DO UPDATE SET account_id=excluded.account_id, payload=excluded.payload, source_payload=excluded.source_payload",
                    [
                        (
                            connection_id,
                            record.id,
                            record.account_id,
                            self.seal(record),
                            self.seal_source(source),
                        )
                        for record, source in records
                    ],
                )
                conn.executemany(
                    "DELETE FROM transactions WHERE connection_id=? AND source_kind='bank' AND transaction_id=?",
                    [(connection_id, identity) for identity in removed],
                )
            for snapshot, source in balances:
                self._save_snapshot(conn, "balances", snapshot, source)

    def replace_investment_range(
        self,
        original: StoredConnection,
        start: date,
        end: date,
        records: list[tuple[Transaction, dict]],
        balances: list[tuple[AccountsSnapshot, dict]],
    ):
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE connections SET revision=revision+1 WHERE id=? AND revision=?",
                (original.public.id, original.revision),
            )
            if changed.rowcount != 1:
                raise ConcurrentSyncError("Connection changed during investment refresh; retry")
            rows = conn.execute(
                "SELECT transaction_id, payload FROM transactions WHERE connection_id=? AND source_kind='investment'",
                (original.public.id,),
            ).fetchall()
            removed = [
                row["transaction_id"]
                for row in rows
                if start <= self.unseal(row["payload"], Transaction).date <= end
            ]
            conn.executemany(
                "DELETE FROM transactions WHERE connection_id=? AND source_kind='investment' AND transaction_id=?",
                [(original.public.id, identity) for identity in removed],
            )
            conn.executemany(
                "INSERT INTO transactions VALUES (?, 'investment', ?, ?, ?, ?) ON CONFLICT(connection_id, source_kind, transaction_id) DO UPDATE SET account_id=excluded.account_id, payload=excluded.payload, source_payload=excluded.source_payload",
                [
                    (
                        original.public.id,
                        record.id,
                        record.account_id,
                        self.seal(record),
                        self.seal_source(source),
                    )
                    for record, source in records
                ],
            )

            for snapshot, source in balances:
                self._save_snapshot(conn, "balances", snapshot, source)

    def save_link_session(self, session: LinkState) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO link_sessions VALUES (?, ?)", (session.id, self.seal(session))
            )

    def _link_state(self, conn: sqlite3.Connection, identity: str) -> LinkState:
        row = conn.execute("SELECT payload FROM link_sessions WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise FinanceError("Link session not found; start a new connection flow")
        return self.unseal(row[0], LinkState)

    def link_session(self, identity: str) -> LinkState:
        with self.connect() as conn:
            return self._link_state(conn, identity)

    def claim_link_session(self, identity: str) -> LinkState:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = self._link_state(conn, identity)
            if session.status == "pending":
                if session.expires <= datetime.now(UTC):
                    raise FinanceError("Link session expired; start again")
                claimed = session.model_copy(update={"status": "processing"})
                conn.execute(
                    "UPDATE link_sessions SET payload=? WHERE id=?", (self.seal(claimed), identity)
                )
        return session

    def reset_link_session(self, identity: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = self._link_state(conn, identity)
            if session.status == "processing":
                conn.execute(
                    "UPDATE link_sessions SET payload=? WHERE id=?",
                    (self.seal(session.model_copy(update={"status": "pending"})), identity),
                )

    def finish_link_session(
        self, identity: str, connection: Connection, access_token: str | None = None
    ) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = self._link_state(conn, identity)
            if session.status != "processing":
                raise FinanceError("Link completion was not claimed")
            if session.mode == "connect":
                if not access_token:
                    raise FinanceError("Missing connection token")
                conn.execute(
                    "INSERT INTO connections (id, payload, token) VALUES (?, ?, ?)",
                    (
                        connection.id,
                        self.seal(connection),
                        self.cipher.encrypt(access_token.encode()),
                    ),
                )
            elif session.connection_id != connection.id:
                raise FinanceError("Reauthorization returned a different connection")
            elif (
                conn.execute("SELECT 1 FROM connections WHERE id=?", (connection.id,)).fetchone()
                is None
            ):
                raise FinanceError("Connection was disconnected during reauthorization")
            completed = session.model_copy(
                update={"status": "completed", "result_connection_id": connection.id, "token": ""}
            )
            conn.execute(
                "UPDATE link_sessions SET payload=? WHERE id=?", (self.seal(completed), identity)
            )
