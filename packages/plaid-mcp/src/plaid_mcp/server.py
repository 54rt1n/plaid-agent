"""Financial queries and refresh operations; management and Link remain outside MCP."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Callable
from datetime import date
from functools import wraps
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from plaid_core.application import Finance
from plaid_core.config import default_data_dir
from plaid_core.errors import FinanceError
from plaid_core.models import (
    Account,
    Cashflow,
    CashflowQuery,
    Connection,
    Coverage,
    CoverageQuery,
    Holdings,
    InvestmentRefresh,
    Liabilities,
    SnapshotQuery,
    Snapshots,
    Status,
    SyncAllResult,
    SyncResult,
    TransactionQuery,
    Transactions,
)
from pydantic import ConfigDict, RootModel, ValidationError

MAX_READ_BYTES = 4096


class SyncResponse(RootModel[SyncResult | SyncAllResult]):
    """Keep the existing single-connection JSON shape for either sync input."""

    model_config = ConfigDict(json_schema_extra={"type": "object"})


def fit_page(page: Transactions | Coverage | Snapshots):
    while len(page.model_dump_json().encode()) > MAX_READ_BYTES:
        if len(page.records) <= 1:
            raise ToolError(
                "Result exceeds the readable tool size. Narrow the query or use the CLI for full details."
            )
        records = page.records[: max(1, len(page.records) // 2)]
        updates = {"records": records, "next_offset": page.offset + len(records)}
        if isinstance(page, Transactions):
            updates["limit"] = len(records)
        page = page.model_copy(update=updates)
    return page


def create_server(root: Path, *, env_file: Path | None = None) -> MCPServer:
    server = MCPServer("plaid-agent", log_level="WARNING")

    def tool(fn: Callable):
        @wraps(fn)
        def invoke(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except FinanceError as exc:
                raise ToolError(str(exc)) from None
            except ValidationError:
                raise ToolError("Invalid financial query arguments") from None
            except (OSError, sqlite3.Error):
                raise ToolError("Cannot read or update local financial storage") from None

        server.tool()(invoke)
        return invoke

    def call(method, *args, **kwargs):
        with Finance(root, env_file=env_file) as app:
            return getattr(app, method)(*args, **kwargs)

    @tool
    def finance_status(coverage_query: CoverageQuery | None = None) -> Status:
        """Read owning data directory, cached-data readiness and compact bank coverage. No provider calls. History loading is unknown; stored ranges do not prove complete bank history."""
        status = call("status", coverage_query)
        if status.coverage:
            status = status.model_copy(update={"coverage": fit_page(status.coverage)})
        while len(status.model_dump_json().encode()) > MAX_READ_BYTES:
            page = status.coverage
            if page is None or len(page.records) <= 1:
                raise ToolError(
                    "Status exceeds the readable tool size; use the CLI for the complete status."
                )
            records = page.records[: max(1, len(page.records) // 2)]
            status = status.model_copy(
                update={
                    "coverage": page.model_copy(
                        update={"records": records, "next_offset": page.offset + len(records)}
                    )
                }
            )
        return status

    @tool
    def list_connections() -> list[Connection]:
        """List linked institutions and connection IDs to use for refresh and sync operations."""
        return call("list_connections")

    @tool
    def list_accounts(connection_id: str | None = None) -> list[Account]:
        """Read cached accounts/balances with fetched_at and nullable provider_updated_at. An empty list means no cached accounts, not no bank accounts. Check finance_status for sync readiness. These are not guaranteed live balances."""
        return call("list_accounts", connection_id)

    @tool
    def refresh_accounts(connection_id: str, realtime: bool = False) -> list[Account]:
        """Fetch and cache account balances. realtime=true requests a potentially billable bank extraction."""
        return call("refresh_accounts", connection_id, realtime=realtime)

    @tool
    def sync_transactions(
        connection_id: str | None = None, all_connections: bool = False
    ) -> SyncResponse:
        """Sync bank transactions for one connection OR all_connections=true. All returns per-connection successes/errors/skips and preserves partial successes. No other products or real-time bank refresh."""
        if (connection_id is not None) == all_connections:
            raise ToolError("Supply either connection_id or all_connections=true, exclusively")
        return SyncResponse(
            call("sync_all_transactions")
            if all_connections
            else call("sync_transactions", connection_id)
        )

    @tool
    def query_transactions(query: TransactionQuery) -> Transactions:
        """Search cached transactions with dates/account/merchant/currency filters. Posted only by default; follow next_offset for further pages."""
        return fit_page(call("query_transactions", query))

    @tool
    def summarize_cashflow(query: CashflowQuery) -> Cashflow:
        """Total gross account inflows/outflows by currency and category/merchant/account. Transfers and refunds are not classified as income/expenses; pending excluded by default."""
        return call("summarize_cashflow", query)

    @tool
    def list_holdings(connection_id: str | None = None) -> list[Holdings]:
        """Read cached investment holdings, quantities and prices with fetch timestamps."""
        return call("list_holdings", connection_id)

    @tool
    def refresh_holdings(connection_id: str) -> Holdings:
        """Retrieve Plaid investment holdings and cache the validated snapshot; does not force an institution refresh."""
        return call("refresh_holdings", connection_id)

    @tool
    def refresh_investment_transactions(
        connection_id: str, start: date, end: date
    ) -> InvestmentRefresh:
        """Fetch all investment transactions in a date range and replace that cached range atomically. Read them with query_transactions source_kind=investment."""
        return call("refresh_investment_transactions", connection_id, start, end)

    @tool
    def list_liabilities(connection_id: str | None = None) -> list[Liabilities]:
        """Read cached credit/loan payments, due dates and interest information with fetch timestamps."""
        return call("list_liabilities", connection_id)

    @tool
    def refresh_liabilities(connection_id: str) -> Liabilities:
        """Retrieve and cache loan and credit liabilities. Account numbers and addresses are omitted."""
        return call("refresh_liabilities", connection_id)

    @tool
    def query_snapshots(query: SnapshotQuery) -> Snapshots:
        """Read historical balance/holding/liability snapshots by connection, kind and fetch-time range. Follow next_offset for further observations; fetch time is not bank update time."""
        return fit_page(call("query_snapshots", query))

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    create_server(args.data_dir.expanduser().resolve(), env_file=args.env_file).run(
        transport="stdio"
    )


if __name__ == "__main__":
    main()
