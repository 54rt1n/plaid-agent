import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import simplejson
from plaid_core.application import Finance, initialize_application
from plaid_core.client import PlaidClient
from plaid_core.config import Config, Credentials


def account_payload(identity="checking", currency="USD"):
    return {
        "account_id": identity,
        "name": "Daily checking",
        "mask": "1234",
        "type": "depository",
        "subtype": "checking",
        "balances": {"current": 1234.56, "available": 1200, "iso_currency_code": currency},
        "account_number": "NEVER-RETURN-ACCOUNT-NUMBER",
    }


def transaction_payload(identity="coffee", amount="4.25", currency="USD", pending=False):
    return {
        "transaction_id": identity,
        "account_id": "checking",
        "name": "Cafe",
        "merchant_name": "Cafe",
        "date": "2026-11-05",
        "amount": Decimal(amount),
        "iso_currency_code": currency,
        "pending": pending,
        "personal_finance_category": {"primary": "FOOD_AND_DRINK"},
        "location": {"address": "NEVER-RETURN-ADDRESS"},
    }


def sync_page(added=(), modified=(), removed=(), *, cursor="complete", more=False):
    return {
        "added": list(added),
        "modified": list(modified),
        "removed": [{"transaction_id": x} for x in removed],
        "next_cursor": cursor,
        "has_more": more,
        "accounts": [account_payload()],
    }


class PlaidFixture:
    def __init__(self):
        self.sync = []
        self.calls = []
        self.holdings = []
        self.liabilities = {"credit": [], "mortgage": [], "student": []}
        self.investments = []
        self.accounts = [account_payload()]
        self.exchange_count = 0
        self.exchange_failure = None
        self.items = {}
        self.expected_secret = "test-sandbox-secret"

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append((request.url.path, body))
        assert body["client_id"] == "test-client"
        assert body["secret"] == self.expected_secret
        path = request.url.path
        if path == "/sandbox/public_token/create":
            response = {"public_token": "test-public"}
        elif path == "/item/public_token/exchange":
            if isinstance(self.exchange_failure, Exception):
                raise self.exchange_failure
            if self.exchange_failure is not None:
                return self.exchange_failure
            self.exchange_count += 1
            response = {
                "item_id": "connection-one"
                if self.exchange_count == 1
                else f"connection-{self.exchange_count}",
                "access_token": "test-access-token"
                if self.exchange_count == 1
                else f"test-access-token-{self.exchange_count}",
            }
            self.items[response["access_token"]] = response["item_id"]
        elif path == "/item/get":
            response = {"item": {"item_id": self.items[body["access_token"]], "error": None}}
        elif path == "/item/remove":
            response = {"removed": True}
        elif path == "/link/token/create":
            response = {"link_token": "test-link-token", "expiration": "2030-01-01T00:00:00Z"}
        elif path in {"/accounts/get", "/accounts/balance/get"}:
            response = {"accounts": self.accounts}
        elif path == "/transactions/sync":
            response = self.sync.pop(0)
        elif path == "/investments/holdings/get":
            response = {
                "accounts": self.accounts,
                "holdings": self.holdings,
                "securities": [
                    {"security_id": "fund", "name": "Example Fund", "ticker_symbol": "EXAM"}
                ],
            }
        elif path == "/investments/transactions/get":
            count, offset = body["options"]["count"], body["options"]["offset"]
            response = {
                "investment_transactions": self.investments[offset : offset + count],
                "total_investment_transactions": len(self.investments),
                "accounts": self.accounts,
            }
        elif path == "/liabilities/get":
            response = {"accounts": [account_payload()], "liabilities": self.liabilities}
        else:
            raise AssertionError(f"Unexpected endpoint {path}")
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, content=simplejson.dumps(response, use_decimal=True))


@pytest.fixture
def fake():
    return PlaidFixture()


@pytest.fixture
def app(tmp_path, fake, monkeypatch):
    monkeypatch.delenv("OAUTH_CALLBACK_URI", raising=False)
    config = Config(products=("transactions", "investments", "liabilities"))
    initialize_application(tmp_path, config)
    client = PlaidClient(
        config,
        Credentials(client_id="test-client", secret="test-sandbox-secret"),
        httpx.MockTransport(fake),
    )
    service = Finance(tmp_path, client=client)
    yield service
    service.close()
    client.close()


@pytest.fixture
def timestamp():
    return datetime(2026, 11, 5, 12, tzinfo=UTC)
