import asyncio
import json
import os
import subprocess
import sys

from conftest import account_payload, sync_page, transaction_payload
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from plaid_cli.output import human
from plaid_core.link import Link
from plaid_core.models import Connection, Transaction


def cli(root, *args, ok=True):
    result = subprocess.run(
        [sys.executable, "-m", "plaid_cli", "--data-dir", str(root), *args],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (result.returncode == 0) == ok, result.stderr
    return result


def test_cli_has_human_and_json_modes_and_environment_management(tmp_path):
    output = cli(tmp_path, "init").stdout
    assert "sandbox" in output and not output.startswith("{")
    with (tmp_path / "config.toml").open("a") as config:
        config.write('origin = "http://127.0.0.1:8765"\n')
    status = json.loads(cli(tmp_path, "--json", "status").stdout)
    assert status["connections"] == []
    assert "No cached accounts" in cli(tmp_path, "accounts", "list").stdout
    assert json.loads(cli(tmp_path, "--json", "transactions", "list").stdout)["total"] == 0
    summary = cli(
        tmp_path, "transactions", "summary", "--start", "2026-11-01", "--end", "2026-11-30"
    ).stdout
    assert "Gross account flows" in summary
    assert "No matching transactions" in summary
    cli(tmp_path, "config", "set", "--environment", "production")
    assert json.loads(cli(tmp_path, "--json", "status").stdout)["environment"] == "production"
    cli(tmp_path, "config", "set", "--environment", "sandbox")
    assert json.loads(cli(tmp_path, "--json", "status").stdout)["environment"] == "sandbox"
    invalid = cli(
        tmp_path,
        "--json",
        "transactions",
        "summary",
        "--start",
        "2026-12-01",
        "--end",
        "2026-11-01",
        ok=False,
    )
    assert "start must be on or before end" in json.loads(invalid.stderr)["error"]


def test_cli_validation_does_not_create_or_overwrite_state(tmp_path):
    missing = cli(tmp_path, "--json", "status", ok=False)
    assert "not initialized" in json.loads(missing.stderr)["error"]
    assert list(tmp_path.iterdir()) == []
    cli(tmp_path, "init")
    original = (tmp_path / "config.toml").read_bytes()
    cli(tmp_path, "init", "--environment", "production", ok=False)
    assert (tmp_path / "config.toml").read_bytes() == original
    cli(tmp_path, "connections", "remove", "unknown", ok=False)
    assert not (tmp_path / "production").exists()


def test_human_transaction_amount_keeps_subcent_precision(timestamp):
    record = Transaction(
        id="precise",
        connection_id="test",
        account_id="checking",
        date="2026-11-05",
        name="Fractional units",
        amount="0.00012345",
        currency="unofficial:BTC",
        pending=False,
        fetched_at=timestamp,
    )
    assert "0.00012345 unofficial:BTC" in human(record)


def test_human_cli_retains_account_ownership_and_cache_freshness(app, fake, tmp_path):
    identity = Link(app).sandbox_connection("ins_test").id
    fake.accounts = [
        {**account_payload("brokerage"), "name": "Brokerage"},
        {**account_payload("ira"), "name": "IRA"},
    ]
    app.refresh_accounts(identity)
    fake.holdings = [
        {
            "account_id": account["account_id"],
            "security_id": "fund",
            "quantity": 1,
            "institution_value": 100,
            "iso_currency_code": "USD",
        }
        for account in fake.accounts
    ]
    app.refresh_holdings(identity)
    fake.sync = [sync_page([{**transaction_payload(), "account_id": "brokerage"}])]
    app.sync_transactions(identity)
    accounts = cli(tmp_path, "accounts", "list").stdout
    assert "Account: brokerage" in accounts and "Account: ira" in accounts and identity in accounts
    holdings = cli(tmp_path, "investments", "holdings", "list").stdout
    assert (
        "Brokerage" in holdings
        and "IRA" in holdings
        and "account brokerage" in holdings
        and "account ira" in holdings
    )
    assert holdings.index("Brokerage") < holdings.index("EXAM") < holdings.index("IRA")
    transactions = cli(tmp_path, "transactions", "list").stdout
    assert "Brokerage" in transactions and "Cached records fetched" in transactions
    summary = cli(
        tmp_path,
        "transactions",
        "summary",
        "--start",
        "2026-11-01",
        "--end",
        "2026-11-30",
        "--group-by",
        "account",
    ).stdout
    assert "Brokerage" in summary and "account brokerage" in summary and identity in summary


def test_mcp_real_process_typed_queries_and_restarts(app, tmp_path, timestamp):
    app.store.add_connection(
        Connection(id="fixture", products=("transactions",), connected_at=timestamp), "fake-token"
    )
    record = Transaction(
        id="fixture-row",
        connection_id="fixture",
        account_id="checking",
        date="2026-11-05",
        name="Fixture",
        amount="12.34",
        currency="USD",
        pending=False,
        fetched_at=timestamp,
    )
    app.store.commit_sync(
        app.store.connection("fixture"),
        "fixture-cursor",
        [([(record, {"original_field": "retained"})], [])],
        [],
        timestamp,
    )

    async def session():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-m", "plaid_mcp.server", "--data-dir", str(tmp_path)],
            env={key: value for key, value in os.environ.items() if not key.startswith("PLAID_")},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert "summarize_cashflow" in tools and "query_transactions" in tools
                assert not {"create_link_token", "disconnect", "remove_item"} & tools.keys()
                for name, arguments in [
                    ("finance_status", {}),
                    ("list_connections", {}),
                    ("list_accounts", {}),
                    ("query_transactions", {"query": {"limit": 2}}),
                    ("summarize_cashflow", {"query": {"start": "2026-11-01", "end": "2026-11-30"}}),
                    ("list_holdings", {}),
                    ("list_liabilities", {}),
                ]:
                    result = await client.call_tool(name, arguments)
                    assert not result.isError, result
                    assert result.structuredContent is not None
                    assert "fake-token" not in str(result)
                invalid = await client.call_tool("query_transactions", {"query": {"limit": 0}})
                assert invalid.isError
                status = await client.call_tool("finance_status", {})
                assert status.structuredContent["data_dir"] == str(tmp_path)
                assert status.structuredContent["coverage"]["records"][0]["count"] == 1
                combined = await client.call_tool("sync_transactions", {"all_connections": True})
                assert not combined.isError
                assert combined.structuredContent["outcome"] == "failed"
                assert (
                    "Set PLAID_CLIENT_ID" in combined.structuredContent["connections"][0]["error"]
                )
                connections = await client.call_tool("list_connections", {})
                assert "fixture" in str(connections)

    asyncio.run(session())
    asyncio.run(session())
