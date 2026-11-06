from datetime import date

import httpx
import pytest
from conftest import sync_page, transaction_payload
from plaid_cli.output import human
from plaid_core.config import read_config
from plaid_core.errors import FinanceError
from plaid_core.models import Account, AccountsSnapshot, Connection, CoverageQuery, Transaction


def test_sync_all_preserves_partial_success_and_skips_other_products(app, fake, timestamp):
    for index in range(4):
        app.store.add_connection(
            Connection(
                id=f"bank-{index}",
                institution=f"Bank {index}",
                products=("transactions",),
                connected_at=timestamp,
            ),
            "test-access-token",
        )
    app.store.add_connection(
        Connection(id="investments", products=("investments",), connected_at=timestamp),
        "test-access-token",
    )
    prior = Transaction(
        id="prior",
        connection_id="bank-1",
        account_id="checking",
        name="Prior",
        date="2026-11-01",
        amount="2",
        currency="USD",
        pending=False,
        fetched_at=timestamp,
    )
    app.store.commit_sync(
        app.store.connection("bank-1"), "prior-cursor", [([(prior, {})], [])], [], timestamp
    )
    fake.sync = [
        sync_page([transaction_payload("first")]),
        httpx.Response(400, json={"error_code": "ITEM_LOGIN_REQUIRED"}),
        sync_page([transaction_payload("third")]),
        sync_page([transaction_payload("fourth")]),
    ]
    result = app.sync_all_transactions()
    assert result.outcome == "partial"
    assert [row.outcome for row in result.connections] == [
        "success",
        "failed",
        "success",
        "success",
        "skipped",
    ]
    assert sum(row.result.added for row in result.connections if row.result) == 3
    assert "ITEM_LOGIN_REQUIRED" in result.connections[1].error
    assert app.store.connection("bank-1").cursor == "prior-cursor"
    assert app.store.connection("bank-0").cursor == "complete"
    assert len(app.store.transactions()) == 4
    assert app.status().readiness[1].transaction_cache == "cached_records"
    assert [path for path, _ in fake.calls] == ["/transactions/sync"] * 4


def test_sync_all_empty_eligible_set_is_explicit_noop(app, fake, timestamp):
    app.store.add_connection(
        Connection(id="investments", products=("investments",), connected_at=timestamp), "unused"
    )
    result = app.sync_all_transactions()
    assert result.outcome == "success" and result.eligible_connections == 0
    assert result.connections[0].outcome == "skipped"
    assert "No eligible" in human(result)
    assert fake.calls == []
    assert app.status().readiness[0].transaction_cache == "not_enabled"


def test_coverage_answers_counts_dates_and_readiness_without_details(app, fake, timestamp):
    ranges = [
        (540, "2023-01-01", "2023-12-31"),
        (360, "2024-02-01", "2024-06-30"),
        (240, "2025-03-01", "2025-09-30"),
        (180, "2026-04-01", "2026-07-31"),
    ]
    for index, (count, first, last) in enumerate(ranges):
        identity = f"bank-{index}"
        app.store.add_connection(
            Connection(
                id=identity,
                institution=f"Bank {index}",
                products=("transactions",),
                connected_at=timestamp,
            ),
            "unused",
        )
        account = Account(
            id="checking",
            connection_id=identity,
            name="Checking",
            type="depository",
            currency="USD",
            fetched_at=timestamp,
        )
        app.store.add_snapshot(
            "balances",
            AccountsSnapshot(connection_id=identity, accounts=[account], fetched_at=timestamp),
            {},
        )
        records = [
            Transaction(
                id=f"tx-{n}",
                connection_id=identity,
                account_id="checking",
                name="PRIVATE-MERCHANT",
                date=first if n == 0 else last,
                amount="1",
                currency="USD",
                pending=False,
                fetched_at=timestamp,
            )
            for n in range(count)
        ]
        records.append(
            records[-1].model_copy(
                update={"id": "pending", "pending": True, "date": date(2020, 1, 1)}
            )
        )
        records.append(
            records[-1].model_copy(
                update={"id": "investment", "source_kind": "investment", "pending": False}
            )
        )
        app.store.commit_sync(
            app.store.connection(identity),
            "cursor",
            [([(record, {}) for record in records], [])],
            [],
            timestamp,
        )
    app.store.add_connection(
        Connection(id="unsynced", products=("transactions",), connected_at=timestamp), "unused"
    )
    coverage = app.transaction_coverage(CoverageQuery())
    assert sum(row.count for row in coverage.records) == 1320
    for row, (count, first, last) in zip(coverage.records, ranges, strict=False):
        assert (row.count, str(row.earliest_date), str(row.latest_date)) == (count, first, last)
    assert coverage.records[-1].count == 0 and coverage.records[-1].earliest_date is None
    assert "PRIVATE-MERCHANT" not in coverage.model_dump_json()
    accounts = app.transaction_coverage(CoverageQuery(group_by="account", limit=2))
    assert accounts.total == 4 and accounts.next_offset == 2
    assert accounts.records[0].account_id == "checking"
    assert "bank-0" in human(accounts) and "checking" in human(accounts)
    included = app.transaction_coverage(CoverageQuery(include_pending=True))
    assert sum(row.count for row in included.records) == 1324
    assert fake.calls == []
    status = app.status()
    assert status.data_dir == str(app.store.database.parents[1])
    assert status.readiness[-1].transaction_cache == "not_synced"
    assert status.readiness[0].transaction_cache == "cached_records"
    assert "not yet synced" in human(status)


def test_freshness_and_config_errors_are_honest_and_safe(app, tmp_path, timestamp):
    account = Account(
        id="a",
        connection_id="c",
        name="Checking",
        type="depository",
        currency="USD",
        fetched_at=timestamp,
    )
    assert "Provider balance update: not supplied" in human(account)
    assert "Fetched from Plaid" in human(account)
    assert str(timestamp.year) in human(
        account.model_copy(update={"provider_updated_at": timestamp})
    )
    path = tmp_path / "config.toml"
    with path.open("a") as stream:
        stream.write('unsupported_field = "NEVER-PRINT-THIS"\n')
    with pytest.raises(FinanceError) as caught:
        read_config(tmp_path)
    message = str(caught.value)
    assert str(path) in message and "unsupported_field" in message and "extra_forbidden" in message
    assert "NEVER-PRINT-THIS" not in message
