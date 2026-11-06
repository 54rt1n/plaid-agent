"""CLI command tree; normal use needs flags, not JSON input."""

import argparse
from datetime import date, datetime
from pathlib import Path


def configuration(parser, *, initial=False):
    parser.add_argument(
        "--environment", choices=["sandbox", "production"], default="sandbox" if initial else None
    )
    parser.add_argument(
        "--product",
        dest="products",
        action="append",
        choices=["transactions", "investments", "liabilities"],
    )
    parser.add_argument("--country", dest="countries", action="append")
    parser.add_argument("--history-days", type=int)
    parser.add_argument("--client-name")
    parser.add_argument("--link-host", help="Default Link listener address")
    parser.add_argument("--link-port", type=int, help="Default Link listener port")
    parser.add_argument("--link-certfile", help="TLS certificate chain in PEM format")
    parser.add_argument("--link-keyfile", help="TLS private key in PEM format")


def date_range(parser, *, required=False):
    parser.add_argument("--start", type=date.fromisoformat, required=required)
    parser.add_argument("--end", type=date.fromisoformat, required=required)


def connection_filter(parser):
    parser.add_argument("--connection", dest="connection_id")


def transaction_filters(parser):
    date_range(parser)
    connection_filter(parser)
    parser.add_argument("--account", dest="account_id")
    parser.add_argument("--text")
    parser.add_argument("--currency")
    parser.add_argument(
        "--kind", dest="source_kind", choices=["bank", "investment"], default="bank"
    )
    parser.add_argument("--direction", choices=["debit", "credit"])
    parser.add_argument("--category")
    parser.add_argument(
        "--include-details",
        action="store_true",
        help="Include the complete original transaction payload in JSON output",
    )
    parser.add_argument("--include-pending", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)


def subcommands(parser):
    return parser.add_subparsers(dest=f"command_{parser.prog}", required=True)


def command(parent, name, operation, **kwargs):
    parser = parent.add_parser(name, **kwargs)
    parser.set_defaults(operation=operation)
    return parser


def build_parser():
    parser = argparse.ArgumentParser(
        prog="plaid-agent", description="Local financial accounts and reporting"
    )
    root = parser.add_mutually_exclusive_group()
    root.add_argument("--data-dir", type=Path)
    root.add_argument("--profile", help="Hermes profile (requires the Hermes integration package)")
    parser.add_argument("--env-file", type=Path, help="Explicit private dotenv credential file")
    parser.add_argument(
        "--json", action="store_true", help="Print structured results instead of human output"
    )
    commands = subcommands(parser)
    configuration(command(commands, "init", "init"), initial=True)
    command(commands, "status", "status")

    config = subcommands(commands.add_parser("config"))
    command(config, "show", "config.show")
    configuration(command(config, "set", "config.set"))

    connections = subcommands(commands.add_parser("connections"))
    command(connections, "list", "connections.list")
    sandbox = command(connections, "create-sandbox", "connections.sandbox")
    sandbox.add_argument("--institution", default="ins_109508")
    remove = command(
        connections,
        "remove",
        "connections.remove",
        help="Disconnect from Plaid and delete local cached records",
    )
    remove.add_argument("connection_id")
    remove.add_argument("--confirm", action="store_true", required=True)

    accounts = subcommands(commands.add_parser("accounts"))
    connection_filter(command(accounts, "list", "accounts.list"))
    refresh = command(accounts, "refresh", "accounts.refresh")
    refresh.add_argument("connection_id")
    refresh.add_argument(
        "--realtime",
        action="store_true",
        help="Request a real-time balance extraction (may be billable)",
    )

    transactions = subcommands(commands.add_parser("transactions"))
    transaction_filters(command(transactions, "list", "transactions.list"))
    sync = command(transactions, "sync", "transactions.sync")
    sync.add_argument("connection_id", nargs="?")
    sync.add_argument(
        "--all",
        dest="all_connections",
        action="store_true",
        help="Sync every connection with bank transaction access",
    )
    coverage = command(
        transactions,
        "coverage",
        "transactions.coverage",
        help="Stored bank transaction counts and date ranges",
    )
    coverage.add_argument("--group-by", choices=["connection", "account"], default="connection")
    connection_filter(coverage)
    coverage.add_argument("--include-pending", action="store_true")
    coverage.add_argument("--limit", type=int, default=20)
    coverage.add_argument("--offset", type=int, default=0)
    summary = command(
        transactions,
        "summary",
        "transactions.summary",
        help="Gross inflows/outflows, separated by currency",
    )
    date_range(summary, required=True)
    connection_filter(summary)
    summary.add_argument("--account", dest="account_id")
    summary.add_argument(
        "--kind", dest="source_kind", choices=["bank", "investment"], default="bank"
    )
    summary.add_argument("--include-pending", action="store_true")
    summary.add_argument(
        "--group-by", choices=["category", "merchant", "account"], default="category"
    )

    investments = subcommands(commands.add_parser("investments"))
    holdings = subcommands(investments.add_parser("holdings"))
    connection_filter(command(holdings, "list", "holdings.list"))
    command(holdings, "refresh", "holdings.refresh").add_argument("connection_id")
    investment_transactions = subcommands(investments.add_parser("transactions"))
    investment_refresh = command(
        investment_transactions, "refresh", "investments.refresh-transactions"
    )
    investment_refresh.add_argument("connection_id")
    date_range(investment_refresh, required=True)

    liabilities = subcommands(commands.add_parser("liabilities"))
    connection_filter(command(liabilities, "list", "liabilities.list"))
    command(liabilities, "refresh", "liabilities.refresh").add_argument("connection_id")

    snapshots = subcommands(commands.add_parser("snapshots"))
    snapshot_list = command(snapshots, "list", "snapshots.list")
    connection_filter(snapshot_list)
    snapshot_list.add_argument("--kind", choices=["balances", "holdings", "liabilities"])
    snapshot_list.add_argument("--limit", type=int, default=100)
    snapshot_list.add_argument("--offset", type=int, default=0)
    snapshot_list.add_argument("--start", type=datetime.fromisoformat)
    snapshot_list.add_argument("--end", type=datetime.fromisoformat)
    snapshot_show = command(snapshots, "show", "snapshots.show")
    snapshot_show.add_argument("id")
    snapshot_show.add_argument("--include-details", action="store_true")

    connect = command(
        commands,
        "connect",
        "connect",
        help="Manage account connections until you choose Finish setup",
    )
    connect.add_argument("--host", help="Override the configured Link listener address")
    connect.add_argument("--port", type=int, help="Override the configured Link listener port")
    connect.add_argument("--certfile", help="Override the configured TLS certificate file")
    connect.add_argument("--keyfile", help="Override the configured TLS private key file")
    connect.add_argument(
        "--no-open", action="store_true", help="Print the private browser URL without opening it"
    )

    link = subcommands(commands.add_parser("link"))
    serve = command(
        link, "serve", "link.serve", help="Serve the Link website and API from one Python process"
    )
    serve.add_argument("--host", help="Override the configured Link listener address")
    serve.add_argument("--port", type=int, help="Override the configured Link listener port")
    serve.add_argument("--certfile", help="Override the configured TLS certificate file")
    serve.add_argument("--keyfile", help="Override the configured TLS private key file")
    serve.add_argument(
        "--open", action="store_true", help="Open the authenticated setup URL in your browser"
    )
    return parser
