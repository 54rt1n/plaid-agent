"""CLI entry point. All financial behavior belongs to plaid-core."""

from __future__ import annotations

import sqlite3
import sys
from importlib.metadata import entry_points
from pathlib import Path

from plaid_core.application import Finance, initialize_application
from plaid_core.config import Config, default_data_dir, read_config, update_config
from plaid_core.errors import FinanceError
from plaid_core.link import Link
from plaid_core.models import (
    CashflowQuery,
    CoverageQuery,
    SnapshotQuery,
    SyncAllResult,
    TransactionQuery,
)
from pydantic import ValidationError

from .arguments import build_parser
from .output import human, json_output


def resolve_data(args) -> Path:
    if args.profile:
        adapters = list(entry_points(group="plaid_agent.profile_adapters"))
        if len(adapters) != 1:
            raise FinanceError("Install hermes-plaid-agent's integration package to use --profile")
        return Path(adapters[0].load()(args.profile))
    return (args.data_dir or default_data_dir()).expanduser().resolve()


def config_values(args):
    return {
        key: getattr(args, key)
        for key in Config.model_fields
        if getattr(args, key, None) is not None
    }


def dispatch(args, root):
    operation = args.operation
    if operation == "transactions.sync" and bool(args.connection_id) == args.all_connections:
        raise FinanceError("Supply either CONNECTION_ID or --all, exclusively")
    if getattr(args, "include_details", False) and not args.json:
        raise FinanceError(
            "Use --json with --include-details to read the complete provider payload"
        )
    if operation == "init":
        initialize_application(root, Config(**config_values(args)))
    if operation == "config.show":
        return read_config(root)
    if operation == "config.set":
        values = {**read_config(root).model_dump(), **config_values(args)}
        config = Config.model_validate(values)
        update_config(root, config)
        return config
    if operation == "connect":
        from plaid_link.connect import connect

        return connect(
            root,
            env_file=args.env_file,
            host=args.host,
            port=args.port,
            certfile=args.certfile,
            keyfile=args.keyfile,
            open_browser=not args.no_open,
        )
    if operation == "link.serve":
        from plaid_link.server import serve

        config = read_config(root)
        serve(
            root,
            env_file=args.env_file,
            host=args.host or config.link_host,
            port=args.port if args.port is not None else config.link_port,
            certfile=args.certfile,
            keyfile=args.keyfile,
            open_browser=args.open,
        )
        return None
    with Finance(root, env_file=args.env_file) as app:
        handlers = {
            "init": app.status,
            "status": app.status,
            "connections.list": app.list_connections,
            "connections.sandbox": lambda: Link(app).sandbox_connection(args.institution),
            "accounts.list": lambda: app.list_accounts(args.connection_id),
            "accounts.refresh": lambda: app.refresh_accounts(
                args.connection_id, realtime=args.realtime
            ),
            "transactions.sync": lambda: (
                app.sync_all_transactions()
                if args.all_connections
                else app.sync_transactions(args.connection_id)
            ),
            "transactions.coverage": lambda: app.transaction_coverage(
                CoverageQuery(**{key: getattr(args, key) for key in CoverageQuery.model_fields})
            ),
            "transactions.list": lambda: app.query_transactions(
                TransactionQuery(
                    **{key: getattr(args, key) for key in TransactionQuery.model_fields}
                )
            ),
            "transactions.summary": lambda: app.summarize_cashflow(
                CashflowQuery(**{key: getattr(args, key) for key in CashflowQuery.model_fields})
            ),
            "holdings.list": lambda: app.list_holdings(args.connection_id),
            "holdings.refresh": lambda: app.refresh_holdings(args.connection_id),
            "liabilities.list": lambda: app.list_liabilities(args.connection_id),
            "liabilities.refresh": lambda: app.refresh_liabilities(args.connection_id),
            "snapshots.list": lambda: app.query_snapshots(
                SnapshotQuery(
                    connection_id=args.connection_id,
                    kind=args.kind,
                    start=args.start,
                    end=args.end,
                    limit=args.limit,
                    offset=args.offset,
                )
            ),
            "snapshots.show": lambda: app.get_snapshot(
                args.id, include_details=args.include_details
            ),
            "investments.refresh-transactions": lambda: app.refresh_investment_transactions(
                args.connection_id, args.start, args.end
            ),
        }
        if operation == "connections.remove":
            app.disconnect(args.connection_id)
            return "Connection disconnected and local cached records deleted."
        return handlers[operation]()


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = dispatch(args, resolve_data(args))
        if result is not None:
            if args.operation == "accounts.list" and not result and not args.json:
                print(
                    "No cached accounts. Check status for connections that have not yet been synced."
                )
            else:
                print(json_output(result) if args.json else human(result))
        if isinstance(result, SyncAllResult) and result.outcome != "success":
            raise SystemExit(1)
    except (FinanceError, ValidationError, OSError, sqlite3.Error) as exc:
        if isinstance(exc, ValidationError):
            message = "Invalid arguments: " + "; ".join(
                ".".join(map(str, e["loc"])) + ": " + e["msg"]
                for e in exc.errors(include_input=False, include_context=False)
            )
        elif isinstance(exc, FinanceError):
            message = str(exc)
        else:
            message = (
                "Local file or database operation failed; check the data directory and permissions"
            )
        print(
            json_output({"error": message}) if args.json else f"Error: {message}", file=sys.stderr
        )
        raise SystemExit(1) from None
