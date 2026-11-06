"""Shared finance application used by CLI, website and MCP."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from . import mapping, reporting
from .client import PlaidClient
from .config import Config, Credentials, initialize, read_config
from .errors import FinanceError, ProviderError
from .models import (
    Account,
    AccountsSnapshot,
    Cashflow,
    CashflowQuery,
    Connection,
    ConnectionReadiness,
    ConnectionSyncOutcome,
    Coverage,
    CoverageQuery,
    Holdings,
    InvestmentRefresh,
    Liabilities,
    SnapshotInfo,
    SnapshotQuery,
    SnapshotRecord,
    Snapshots,
    Status,
    SyncAllResult,
    SyncResult,
    TransactionQuery,
    Transactions,
)
from .store import Store, initialize_store


def now() -> datetime:
    return datetime.now(UTC)


def initialize_application(root: Path, config: Config) -> None:
    initialize(root, config)
    initialize_store(root / config.environment)


class Finance:
    def __init__(
        self,
        root: Path,
        *,
        env_file: Path | None = None,
        client: PlaidClient | None = None,
        config: Config | None = None,
    ):
        self.root = root.resolve()
        self.config = config or read_config(root)
        self.store = Store(root / self.config.environment)
        profile_credentials = root / ".env"
        self.env_file = env_file or (profile_credentials if profile_credentials.is_file() else None)
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> PlaidClient:
        if self._client is None:
            self._client = PlaidClient(
                self.config, Credentials.from_environment(self.config.environment, self.env_file)
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def status(self, coverage_query: CoverageQuery | None = None) -> Status:
        connections = self.store.connections()
        accounts = self.store.accounts()
        records = self.store.transactions()
        account_counts = Counter(item.connection_id for item in accounts)
        posted_counts = Counter(
            item.connection_id
            for item in records
            if item.source_kind == "bank" and not item.pending
        )
        return Status(
            environment=self.config.environment,
            initialized=True,
            products=self.config.products,
            connections=connections,
            data_dir=str(self.root),
            readiness=[
                ConnectionReadiness(
                    connection_id=item.id,
                    cached_accounts=account_counts[item.id],
                    cached_posted_transactions=posted_counts[item.id],
                    transaction_cache="not_enabled"
                    if "transactions" not in item.products
                    else "cached_records"
                    if posted_counts[item.id]
                    else "not_synced"
                    if item.last_transaction_sync is None
                    else "no_cached_records",
                )
                for item in connections
            ],
            coverage=reporting.coverage(
                records, connections, accounts, coverage_query or CoverageQuery()
            ),
        )

    def transaction_coverage(self, query: CoverageQuery) -> Coverage:
        connections = self.store.connections()
        if query.connection_id is not None and query.connection_id not in {
            item.id for item in connections
        }:
            raise FinanceError("Connection not found in this environment")
        return reporting.coverage(
            self.store.transactions(query.connection_id),
            connections,
            self.store.accounts(query.connection_id),
            query,
        )

    def sync_all_transactions(self) -> SyncAllResult:
        outcomes = []
        for connection in self.list_connections():
            fields = {"connection_id": connection.id, "institution": connection.institution}
            if "transactions" not in connection.products:
                outcomes.append(
                    ConnectionSyncOutcome(
                        **fields, outcome="skipped", reason="Transactions access is not enabled"
                    )
                )
                continue
            try:
                result = self.sync_transactions(connection.id)
            except FinanceError as exc:
                outcomes.append(ConnectionSyncOutcome(**fields, outcome="failed", error=str(exc)))
            except (OSError, sqlite3.Error):
                outcomes.append(
                    ConnectionSyncOutcome(
                        **fields, outcome="failed", error="Local financial storage failed"
                    )
                )
            else:
                outcomes.append(ConnectionSyncOutcome(**fields, outcome="success", result=result))
        failed = sum(item.outcome == "failed" for item in outcomes)
        eligible = sum(item.outcome != "skipped" for item in outcomes)
        return SyncAllResult(
            outcome="success" if not failed else "failed" if failed == eligible else "partial",
            eligible_connections=eligible,
            connections=outcomes,
        )

    def list_connections(self) -> list[Connection]:
        return self.store.connections()

    def list_accounts(self, connection_id: str | None = None) -> list[Account]:
        return self.store.accounts(connection_id)

    def refresh_accounts(self, connection_id: str, *, realtime: bool = False) -> list[Account]:
        stored = self.store.connection(connection_id)
        endpoint = "/accounts/balance/get" if realtime else "/accounts/get"
        payload = self.client.post(endpoint, {"access_token": stored.token})
        fetched_at = now()
        records = self._accounts(payload, connection_id, fetched_at)
        self.store.add_snapshot(
            "balances",
            AccountsSnapshot(connection_id=connection_id, accounts=records, fetched_at=fetched_at),
            payload,
        )
        return records

    def _accounts(self, payload: dict, connection_id: str, fetched_at: datetime) -> list[Account]:
        try:
            return [mapping.account(x, connection_id, fetched_at) for x in payload["accounts"]]
        except (KeyError, TypeError, ValidationError) as exc:
            raise ProviderError("INVALID_RESPONSE") from exc

    def _connection_for(self, connection_id: str, product: str):
        connection = self.store.connection(connection_id)
        if product not in connection.public.products:
            raise FinanceError(f"{product} is not enabled for this connection")
        return connection

    def sync_transactions(self, connection_id: str) -> SyncResult:
        original = self._connection_for(connection_id, "transactions")
        # Discard pages from a failed attempt before restarting at the original cursor.
        for attempt in range(3):
            try:
                return self._sync_pages(original)
            except ProviderError as exc:
                if exc.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" or attempt == 2:
                    raise
        raise AssertionError("Unreachable sync state")

    def _sync_pages(self, original) -> SyncResult:
        cursor = original.cursor
        seen = {cursor}
        changes = []
        balances = []
        added = modified = removed = 0
        fetched_at = now()
        for _page in range(10000):
            request = {"access_token": original.token, "count": 500}
            if cursor is not None:
                request["cursor"] = cursor
            payload = self.client.post("/transactions/sync", request)
            try:
                new = [
                    (mapping.transaction(x, original.public.id, fetched_at), x)
                    for x in payload["added"]
                ]
                edits = [
                    (mapping.transaction(x, original.public.id, fetched_at), x)
                    for x in payload["modified"]
                ]
                deleted = [str(x["transaction_id"]) for x in payload["removed"]]
                next_cursor = payload["next_cursor"]
                more = payload["has_more"]
                if not isinstance(next_cursor, str) or type(more) is not bool:
                    raise ValueError("Invalid sync cursor")
                if "accounts" in payload:
                    accounts = self._accounts(payload, original.public.id, fetched_at)
                    balances.append(
                        (
                            AccountsSnapshot(
                                connection_id=original.public.id,
                                accounts=accounts,
                                fetched_at=fetched_at,
                                complete=False,
                            ),
                            {"accounts": payload["accounts"]},
                        )
                    )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("INVALID_RESPONSE") from exc
            changes.append((new + edits, deleted))
            added += len(new)
            modified += len(edits)
            removed += len(deleted)
            if not more:
                self.store.commit_sync(original, next_cursor, changes, balances, fetched_at)
                return SyncResult(
                    connection_id=original.public.id,
                    added=added,
                    modified=modified,
                    removed=removed,
                    fetched_at=fetched_at,
                )
            if next_cursor in seen:
                raise ProviderError("NON_ADVANCING_CURSOR")
            cursor = next_cursor
            seen.add(cursor)
        raise ProviderError("PAGINATION_LIMIT_EXCEEDED")

    def query_transactions(self, query: TransactionQuery) -> Transactions:
        rows = reporting.matching(
            self.store.transactions(query.connection_id, include_details=query.include_details),
            query,
        )
        stop = query.offset + query.limit
        return Transactions(
            records=rows[query.offset : stop],
            total=len(rows),
            limit=query.limit,
            offset=query.offset,
            next_offset=stop if stop < len(rows) else None,
        )

    def summarize_cashflow(self, query: CashflowQuery) -> Cashflow:
        return reporting.cashflow(self.store.transactions(query.connection_id), query)

    def refresh_holdings(self, connection_id: str) -> Holdings:
        original = self._connection_for(connection_id, "investments")
        payload = self.client.post("/investments/holdings/get", {"access_token": original.token})
        fetched_at = now()
        try:
            securities = {x["security_id"]: x for x in payload["securities"]}
            result = Holdings(
                connection_id=connection_id,
                accounts=self._accounts(payload, connection_id, fetched_at),
                records=[mapping.holding(x, securities) for x in payload["holdings"]],
                fetched_at=fetched_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("INVALID_RESPONSE") from exc
        self.store.add_snapshot("holdings", result, payload)
        return result

    def list_holdings(self, connection_id: str | None = None) -> list[Holdings]:
        return self.store.latest_snapshots("holdings", Holdings, connection_id)

    def refresh_liabilities(self, connection_id: str) -> Liabilities:
        original = self._connection_for(connection_id, "liabilities")
        payload = self.client.post("/liabilities/get", {"access_token": original.token})
        try:
            result = mapping.liabilities(payload, connection_id, now())
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("INVALID_RESPONSE") from exc
        self.store.add_snapshot("liabilities", result, payload)
        return result

    def list_liabilities(self, connection_id: str | None = None) -> list[Liabilities]:
        return self.store.latest_snapshots("liabilities", Liabilities, connection_id)

    def list_snapshots(
        self,
        *,
        connection_id: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SnapshotInfo]:
        return self.store.snapshots(
            connection_id=connection_id, kind=kind, limit=limit, offset=offset
        )

    def get_snapshot(self, identity: str, *, include_details: bool = False) -> SnapshotRecord:
        return self.store.snapshot(identity, include_details=include_details)

    def query_snapshots(self, query: SnapshotQuery) -> Snapshots:
        records, total = self.store.query_snapshot_infos(query)
        stop = query.offset + query.limit
        return Snapshots(
            records=[self.store.snapshot(record.id) for record in records],
            total=total,
            offset=query.offset,
            next_offset=stop if stop < total else None,
        )

    def refresh_investment_transactions(
        self, connection_id: str, start: date, end: date
    ) -> InvestmentRefresh:
        TransactionQuery(start=start, end=end)
        original = self._connection_for(connection_id, "investments")
        records = []
        seen = set()
        expected_total = None
        fetched_at = now()
        balances = []
        for _page in range(10000):
            payload = self.client.post(
                "/investments/transactions/get",
                {
                    "access_token": original.token,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "options": {"count": 500, "offset": len(records)},
                },
            )
            try:
                total = int(payload["total_investment_transactions"])
                page = [
                    (mapping.investment_transaction(x, connection_id, fetched_at), x)
                    for x in payload["investment_transactions"]
                ]
                if total < 0 or (expected_total is not None and total != expected_total):
                    raise ValueError("Investment history changed during pagination")
                expected_total = total
                if "accounts" in payload:
                    balances = [
                        (
                            AccountsSnapshot(
                                connection_id=connection_id,
                                accounts=self._accounts(payload, connection_id, fetched_at),
                                fetched_at=fetched_at,
                                complete=False,
                            ),
                            {"accounts": payload["accounts"]},
                        )
                    ]
                for record, _source in page:
                    if record.id in seen or not start <= record.date <= end:
                        raise ValueError("Inconsistent investment page")
                    seen.add(record.id)
                records.extend(page)
                if len(records) == total:
                    self.store.replace_investment_range(original, start, end, records, balances)
                    return InvestmentRefresh(
                        connection_id=connection_id,
                        start=start,
                        end=end,
                        count=total,
                        fetched_at=fetched_at,
                    )
                if not page or len(records) > total:
                    raise ValueError("Invalid investment pagination")
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("INVALID_RESPONSE") from exc
        raise ProviderError("PAGINATION_LIMIT_EXCEEDED")

    def disconnect(self, connection_id: str) -> None:
        stored = self.store.connection(connection_id)
        self.client.post("/item/remove", {"access_token": stored.token})
        self.store.remove_connection(connection_id)
