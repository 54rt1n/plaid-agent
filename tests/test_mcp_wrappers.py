import json
import sys
from datetime import timedelta

import httpx
import pytest
from conftest import sync_page, transaction_payload
from mcp.server.mcpserver.exceptions import ToolError
from plaid_cli import main as cli
from plaid_core.application import Finance
from plaid_core.models import Account, AccountsSnapshot, Connection, Transaction
from plaid_mcp import server as mcp


@pytest.mark.asyncio
async def test_mcp_sync_all_and_coverage_preserve_single_result_shape(
    app, fake, tmp_path, timestamp, monkeypatch
):
    for identity in ["first", "second"]:
        app.store.add_connection(
            Connection(id=identity, products=("transactions",), connected_at=timestamp),
            "test-access-token",
        )
    monkeypatch.setattr(
        mcp, "Finance", lambda *args, **kwargs: Finance(tmp_path, client=app.client)
    )
    server = mcp.create_server(tmp_path)
    tools = await server.list_tools()
    assert len(tools) == 13
    # MCP output schemas must declare an object root, including the sync union.
    for tool in tools:
        if tool.output_schema is not None:
            assert tool.output_schema["type"] == "object", tool.name
    fake.sync = [sync_page([transaction_payload("first")])]
    single_result = await server.call_tool("sync_transactions", {"connection_id": "first"})
    single = single_result.structured_content
    assert single["connection_id"] == "first" and single["added"] == 1
    assert "result" not in single
    fake.sync = [sync_page(), httpx.Response(400, json={"error_code": "ITEM_LOGIN_REQUIRED"})]
    combined_result = await server.call_tool("sync_transactions", {"all_connections": True})
    combined = combined_result.structured_content
    assert combined["outcome"] == "partial" and combined["eligible_connections"] == 2
    status_result = await server.call_tool("finance_status", {})
    status = status_result.structured_content
    assert status["data_dir"] == str(tmp_path)
    assert sum(row["count"] for row in status["coverage"]["records"]) == 1
    with pytest.raises(ToolError, match="Connection not found"):
        await server.call_tool("finance_status", {"coverage_query": {"connection_id": "typo"}})
    for args in [{}, {"connection_id": "first", "all_connections": True}]:
        with pytest.raises(ToolError, match="exclusively"):
            await server.call_tool("sync_transactions", args)


@pytest.mark.asyncio
async def test_mcp_large_reads_keep_complete_pages_and_refuse_oversize_record(
    app, tmp_path, timestamp, monkeypatch
):
    app.store.add_connection(
        Connection(id="bank", products=("transactions",), connected_at=timestamp), "unused"
    )
    records = [
        Transaction(
            id=str(index),
            connection_id="bank",
            account_id="a",
            name="Long merchant " + "x" * 3000,
            date="2026-11-05",
            amount="1",
            currency="USD",
            pending=False,
            fetched_at=timestamp,
        )
        for index in range(12)
    ]
    app.store.commit_sync(
        app.store.connection("bank"),
        "cursor",
        [([(item, {}) for item in records], [])],
        [],
        timestamp,
    )
    server = mcp.create_server(tmp_path)
    offset = 0
    seen = set()
    while offset is not None:
        page_result = await server.call_tool(
            "query_transactions", {"query": {"limit": 100, "offset": offset}}
        )
        page = page_result.structured_content
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 9000
        assert page["total"] == 12 and page["records"]
        seen.update(item["id"] for item in page["records"])
        offset = page["next_offset"]
    assert len(seen) == 12
    giant = records[0].model_copy(update={"id": "giant", "name": "x" * 20000})
    app.store.commit_sync(
        app.store.connection("bank"), "next", [([(giant, {})], [])], [], timestamp
    )
    with pytest.raises(ToolError, match="size"):
        await server.call_tool("query_transactions", {"query": {"text": "x" * 20000}})


def test_cli_partial_sync_returns_report_and_nonzero_exit(
    app, fake, tmp_path, timestamp, monkeypatch, capsys
):
    for identity in ["first", "second"]:
        app.store.add_connection(
            Connection(id=identity, products=("transactions",), connected_at=timestamp),
            "test-access-token",
        )
    fake.sync = [
        sync_page([transaction_payload()]),
        httpx.Response(400, json={"error_code": "ITEM_LOGIN_REQUIRED"}),
    ]
    monkeypatch.setattr(
        cli, "Finance", lambda *args, **kwargs: Finance(tmp_path, client=app.client)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["plaid-agent", "--data-dir", str(tmp_path), "--json", "transactions", "sync", "--all"],
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "partial" and len(report["connections"]) == 2


@pytest.mark.asyncio
async def test_mcp_snapshot_pages_fit_and_large_single_snapshot_uses_cli(app, tmp_path, timestamp):
    app.store.add_connection(
        Connection(id="bank", products=("transactions",), connected_at=timestamp), "unused"
    )
    account = Account(
        id="a",
        connection_id="bank",
        name="Checking",
        type="depository",
        currency="USD",
        fetched_at=timestamp,
    )
    for _ in range(12):
        app.store.add_snapshot(
            "balances",
            AccountsSnapshot(connection_id="bank", accounts=[account], fetched_at=timestamp),
            {},
        )
    server = mcp.create_server(tmp_path)
    offset, seen = 0, set()
    while offset is not None:
        page_result = await server.call_tool(
            "query_snapshots", {"query": {"limit": 100, "offset": offset}}
        )
        page = page_result.structured_content
        assert page["total"] == 12
        seen.update(row["id"] for row in page["records"])
        offset = page["next_offset"]
    assert len(seen) == 12
    many = [account.model_copy(update={"id": f"account-{index}"}) for index in range(50)]
    app.store.add_snapshot(
        "balances",
        AccountsSnapshot(
            connection_id="bank", accounts=many, fetched_at=timestamp + timedelta(seconds=1)
        ),
        {},
    )
    with pytest.raises(ToolError, match="CLI"):
        await server.call_tool("query_snapshots", {"query": {"limit": 1}})
