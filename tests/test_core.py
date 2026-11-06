import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from conftest import account_payload, sync_page, transaction_payload
from plaid_core.application import Finance
from plaid_core.config import Config, Credentials, update_config
from plaid_core.errors import ConcurrentSyncError, FinanceError, ProviderError
from plaid_core.link import Link
from plaid_core.models import CashflowQuery, SnapshotQuery, TransactionQuery


def connect(app):
    return Link(app).sandbox_connection("ins_test").id


def test_failed_pagination_is_atomic_and_retry_discards_partial_attempt(app, fake):
    identity = connect(app)
    fake.sync = [sync_page([transaction_payload("original")], cursor="base")]
    app.sync_transactions(identity)
    fake.sync = [
        sync_page([transaction_payload("phantom")], cursor="p1", more=True),
        httpx.Response(
            400, json={"error_code": "PRODUCT_NOT_READY", "error_message": "test-access-token"}
        ),
    ]
    with pytest.raises(ProviderError, match="PRODUCT_NOT_READY") as caught:
        app.sync_transactions(identity)
    assert "test-access-token" not in str(caught.value)
    assert [x.id for x in app.query_transactions(TransactionQuery()).records] == ["original"]
    assert app.store.connection(identity).cursor == "base"
    fake.sync = [
        sync_page([transaction_payload("discard-me")], cursor="bad-page", more=True),
        httpx.Response(400, json={"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"}),
        sync_page([transaction_payload("replacement")], removed=["original"], cursor="final"),
    ]
    app.sync_transactions(identity)
    assert [x.id for x in app.query_transactions(TransactionQuery()).records] == ["replacement"]
    assert app.store.connection(identity).cursor == "final"
    assert [
        body.get("cursor") for endpoint, body in fake.calls if endpoint == "/transactions/sync"
    ][-3:] == ["base", "bad-page", "base"]


def test_stale_writer_cannot_overwrite_another_sync(app, fake, timestamp):
    identity = connect(app)
    stale = app.store.connection(identity)
    fake.sync = [sync_page([transaction_payload("new")], cursor="new")]
    app.sync_transactions(identity)
    with pytest.raises(ConcurrentSyncError):
        app.store.commit_sync(stale, "stale", [], None, timestamp)
    assert app.store.connection(identity).cursor == "new"


def test_cashflows_preserve_decimal_currency_pending_and_pagination(app, fake):
    identity = connect(app)
    fake.sync = [
        sync_page(
            [
                transaction_payload("one", "0.1"),
                transaction_payload("two", "0.2"),
                transaction_payload("credit", "-1.05"),
                transaction_payload("euro", "12.34", "EUR"),
                transaction_payload("pending", "1000", pending=True),
            ]
        )
    ]
    app.sync_transactions(identity)
    result = app.summarize_cashflow(CashflowQuery(start=date(2026, 11, 1), end=date(2026, 11, 30)))
    currencies = {x.currency: x for x in result.currencies}
    assert currencies["USD"].debits == Decimal("0.3")
    assert currencies["USD"].credits == Decimal("1.05")
    assert currencies["EUR"].debits == Decimal("12.34")
    assert sum(x.count for x in result.currencies) == 4
    page = app.query_transactions(TransactionQuery(limit=2))
    second = app.query_transactions(TransactionQuery(limit=2, offset=page.next_offset))
    assert len({x.id for x in page.records + second.records}) == 4
    assert second.next_offset is None
    assert app.query_transactions(TransactionQuery(include_pending=True)).total == 5
    fake.sync = [sync_page([transaction_payload("unknown-currency", "22.22", None)], cursor="next")]
    app.sync_transactions(identity)
    incomplete = app.summarize_cashflow(
        CashflowQuery(start=date(2026, 11, 1), end=date(2026, 11, 30))
    )
    assert incomplete.unknown_currency_count == 1
    assert {x.currency for x in incomplete.currencies} == {"USD", "EUR"}


def test_storage_is_encrypted_redacted_and_environment_isolated(app, fake, tmp_path):
    identity = connect(app)
    fake.sync = [sync_page([transaction_payload()])]
    app.sync_transactions(identity)
    visible = app.query_transactions(TransactionQuery()).model_dump_json() + json.dumps(
        [x.model_dump(mode="json") for x in app.list_accounts()]
    )
    assert "NEVER-RETURN" not in visible
    assert "test-access-token" not in visible
    database = tmp_path / "sandbox/finance.sqlite3"
    raw = database.read_bytes()
    for text in [b"test-access-token", b"Daily checking", b"FOOD_AND_DRINK", b"Cafe"]:
        assert text not in raw
    assert database.stat().st_mode & 0o077 == 0
    with Finance(tmp_path) as reopened:
        assert reopened.query_transactions(TransactionQuery()).total == 1
    update_config(tmp_path, Config(environment="production"))
    with Finance(tmp_path) as production:
        assert production.list_connections() == []
        assert production.query_transactions(TransactionQuery()).total == 0
    assert (tmp_path / "sandbox/encryption.key").read_bytes() != (
        tmp_path / "production/encryption.key"
    ).read_bytes()


def test_missing_key_is_not_regenerated(app, tmp_path):
    connect(app)
    (tmp_path / "sandbox/encryption.key").unlink()
    with pytest.raises(FinanceError, match="key is missing"):
        Finance(tmp_path)
    assert not (tmp_path / "sandbox/encryption.key").exists()


def test_credentials_select_environment_and_do_not_mutate_process(tmp_path, monkeypatch):
    for key in ["PLAID_CLIENT_ID", "PLAID_SANDBOX_SECRET", "PLAID_CLIENT_SECRET"]:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "PLAID_CLIENT_ID=test-client\nPLAID_SANDBOX_SECRET=test-sandbox-secret\nPLAID_CLIENT_SECRET=test-production-secret\n"
    )
    path.chmod(0o600)
    sandbox = Credentials.from_environment("sandbox", path)
    production = Credentials.from_environment("production", path)
    assert sandbox.secret.get_secret_value() == "test-sandbox-secret"
    assert production.secret.get_secret_value() == "test-production-secret"
    assert "test-sandbox-secret" not in repr(sandbox)
    with pytest.raises(FinanceError):
        Credentials.from_environment("sandbox")


def test_product_snapshots_redact_sensitive_fields_and_investment_pages(app, fake):
    identity = connect(app)
    fake.holdings = [
        {
            "account_id": "checking",
            "security_id": "fund",
            "quantity": 2,
            "institution_value": 42,
            "institution_price": 21,
            "cost_basis": 40,
            "iso_currency_code": "USD",
        }
    ]
    fake.liabilities["credit"] = [
        {
            "account_id": "checking",
            "account_number": "NEVER-RETURN",
            "minimum_payment_amount": 25,
            "next_payment_due_date": "2026-12-01",
            "is_overdue": False,
        }
    ]
    holdings = app.refresh_holdings(identity)
    liabilities = app.refresh_liabilities(identity)
    assert holdings.records[0].ticker == "EXAM"
    assert liabilities.records[0].minimum_payment_amount == Decimal(25)
    assert "NEVER-RETURN" not in liabilities.model_dump_json()
    assert app.list_holdings(identity) == [holdings]
    assert app.list_liabilities(identity) == [liabilities]
    fake.investments = [
        {
            "investment_transaction_id": str(i),
            "account_id": "checking",
            "date": "2026-11-05",
            "name": "Buy fund",
            "type": "buy",
            "amount": 21,
            "iso_currency_code": "USD",
        }
        for i in range(501)
    ]
    refreshed = app.refresh_investment_transactions(identity, date(2026, 11, 1), date(2026, 11, 30))
    assert refreshed.count == 501
    first = app.query_transactions(TransactionQuery(source_kind="investment", limit=500))
    second = app.query_transactions(
        TransactionQuery(source_kind="investment", limit=500, offset=first.next_offset)
    )
    assert len({x.id for x in first.records + second.records}) == 501
    assert second.next_offset is None
    assert first.records[0].investment.type == "buy"
    assert app.query_transactions(TransactionQuery()).total == 0
    calls_before_cached_read = len(fake.calls)
    app.query_transactions(TransactionQuery(source_kind="investment"))
    assert len(fake.calls) == calls_before_cached_read
    fake.sync = [sync_page([transaction_payload("0")])]
    app.sync_transactions(identity)
    assert app.query_transactions(TransactionQuery()).total == 1
    assert app.query_transactions(TransactionQuery(source_kind="investment")).total == 501


def test_disconnect_removes_all_local_records(app, fake):
    identity = connect(app)
    fake.sync = [sync_page([transaction_payload()])]
    app.sync_transactions(identity)
    app.disconnect(identity)
    assert app.list_connections() == []
    assert app.list_accounts() == []
    assert app.query_transactions(TransactionQuery()).total == 0


def test_investment_pending_cash_flows_are_excluded_until_posted(app, fake):
    identity = connect(app)
    fake.investments = [
        {
            "investment_transaction_id": "pending-in",
            "account_id": "checking",
            "date": "2026-11-05",
            "name": "Pending deposit",
            "type": "cash",
            "subtype": "pending credit",
            "amount": -100,
            "iso_currency_code": "USD",
        },
        {
            "investment_transaction_id": "pending-out",
            "account_id": "checking",
            "date": "2026-11-05",
            "name": "Pending withdrawal",
            "type": "cash",
            "subtype": "pending debit",
            "amount": 50,
            "iso_currency_code": "USD",
        },
    ]
    app.refresh_investment_transactions(identity, date(2026, 11, 1), date(2026, 11, 30))
    assert app.query_transactions(TransactionQuery(source_kind="investment")).total == 0
    pending = app.query_transactions(
        TransactionQuery(source_kind="investment", include_pending=True)
    )
    assert pending.total == 2 and all(record.pending for record in pending.records)
    report = app.summarize_cashflow(
        CashflowQuery(start=date(2026, 11, 1), end=date(2026, 11, 30), source_kind="investment")
    )
    assert report.currencies == []
    fake.investments = [
        {
            **record,
            "investment_transaction_id": record["investment_transaction_id"].replace(
                "pending", "posted"
            ),
            "subtype": "deposit" if record["amount"] < 0 else "withdrawal",
        }
        for record in fake.investments
    ]
    app.refresh_investment_transactions(identity, date(2026, 11, 1), date(2026, 11, 30))
    posted = app.query_transactions(
        TransactionQuery(source_kind="investment", include_pending=True)
    )
    assert posted.total == 2 and all(not record.pending for record in posted.records)
    report = app.summarize_cashflow(
        CashflowQuery(start=date(2026, 11, 1), end=date(2026, 11, 30), source_kind="investment")
    )
    assert report.currencies[0].credits == 100 and report.currencies[0].debits == 50


def test_investment_refresh_keeps_returned_accounts_without_deactivating_others(app, fake):
    identity = connect(app)
    app.refresh_accounts(identity)
    fake.accounts = [
        {**account_payload("brokerage"), "name": "Brokerage", "mask": "4321", "type": "investment"}
    ]
    fake.investments = [
        {
            "investment_transaction_id": "trade",
            "account_id": "brokerage",
            "date": "2026-11-05",
            "name": "Buy fund",
            "type": "buy",
            "amount": 50,
            "iso_currency_code": "USD",
        }
    ]
    app.refresh_investment_transactions(identity, date(2026, 11, 1), date(2026, 11, 30))
    assert {account.id for account in app.list_accounts()} == {"checking", "brokerage"}
    transaction = app.query_transactions(TransactionQuery(source_kind="investment")).records[0]
    assert transaction.account_name == "Brokerage" and transaction.account_mask == "4321"


def test_uninitialized_reads_do_not_create_files(tmp_path):
    with pytest.raises(FinanceError, match="not initialized"):
        Finance(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_register_keeps_full_payload_account_context_and_debit_credit(app, fake):
    identity = connect(app)
    source = transaction_payload("purchase", "12.340001")
    source["new_provider_field"] = {
        "nested": [True, None, {"precise": Decimal("0.1234567890123456789")}]
    }
    source["counterparties"] = [{"name": "Corner Shop", "type": "merchant"}]
    fake.sync = [sync_page([source, transaction_payload("refund", "-3.14")])]
    app.sync_transactions(identity)
    debits = app.query_transactions(TransactionQuery(direction="debit", account_id="checking"))
    assert debits.total == 1
    purchase = debits.records[0]
    assert purchase.debit == Decimal("12.340001") and purchase.credit == 0
    assert purchase.account_name == "Daily checking" and purchase.account_mask == "1234"
    assert purchase.details is None
    credits = app.query_transactions(TransactionQuery(direction="credit"))
    assert credits.records[0].credit == Decimal("3.14") and credits.records[0].debit == 0
    complete = app.query_transactions(
        TransactionQuery(text="Cafe", direction="debit", include_details=True)
    ).records[0]
    assert complete.details == source
    assert complete.model_dump(mode="json")["debit"] == "12.340001"


def test_snapshots_retain_observations_and_partial_sync_keeps_other_accounts(app, fake):
    identity = connect(app)
    fake.accounts = [account_payload(), account_payload("brokerage")]
    app.refresh_accounts(identity)
    first = app.list_snapshots(kind="balances")[0]
    fake.accounts[0]["balances"]["current"] = 900
    app.refresh_accounts(identity)
    snapshots = app.list_snapshots(kind="balances")
    assert len(snapshots) == 2
    history = app.query_snapshots(SnapshotQuery(kind="balances", limit=1))
    assert history.total == 2 and history.next_offset == 1
    prior = app.query_snapshots(SnapshotQuery(kind="balances", end=first.fetched_at))
    assert [x.id for x in prior.records] == [first.id]
    assert app.get_snapshot(first.id).data.accounts[0].current == Decimal("1234.56")
    assert app.get_snapshot(snapshots[0].id).data.accounts[0].current == 900
    assert "NEVER-RETURN" not in app.get_snapshot(first.id).model_dump_json()
    assert (
        app.get_snapshot(first.id, include_details=True).details["accounts"][0]["account_number"]
        == "NEVER-RETURN-ACCOUNT-NUMBER"
    )
    fake.sync = [sync_page([transaction_payload()])]
    app.sync_transactions(identity)
    assert {x.id for x in app.list_accounts()} == {"checking", "brokerage"}
    assert len(app.list_snapshots(kind="balances")) == 3
