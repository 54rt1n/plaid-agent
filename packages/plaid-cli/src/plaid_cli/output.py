"""Human output is deliberately separate from the JSON contracts."""

from decimal import Decimal

from plaid_core.config import Config
from plaid_core.models import (
    Account,
    AccountsSnapshot,
    Cashflow,
    Connection,
    Coverage,
    Holdings,
    InvestmentRefresh,
    Liabilities,
    SnapshotInfo,
    SnapshotRecord,
    Snapshots,
    Status,
    SyncAllResult,
    SyncResult,
    Transaction,
    Transactions,
)
from pydantic import BaseModel, TypeAdapter


def json_output(value) -> str:
    return (
        TypeAdapter(type(value)).dump_json(value).decode()
        if isinstance(value, BaseModel)
        else TypeAdapter(object).dump_json(value).decode()
    )


def number(value: Decimal) -> str:
    precision = max(2, -value.as_tuple().exponent)
    return f"{value:,.{precision}f}"


def amount(value: Decimal | None, currency: str) -> str:
    return f"{number(value)} {currency}" if value is not None else "Unavailable"


def human(value) -> str:
    if isinstance(value, list):
        return "\n".join(human(x) for x in value) or "No records."
    if isinstance(value, Status):
        lines = [
            f"Plaid Agent · {value.environment}",
            f"Products: {', '.join(value.products)}",
            f"Connections: {len(value.connections)}",
        ]
        if value.data_dir:
            lines.append(f"Data directory: {value.data_dir}")
        names = {item.id: item.institution or item.id for item in value.connections}
        states = {
            "not_enabled": "bank transaction access not enabled",
            "not_synced": "connected, not yet synced",
            "cached_records": "cached transactions available",
            "no_cached_records": "no cached posted transactions; history loading unknown",
        }
        for item in value.readiness:
            lines.append(
                f"{names[item.connection_id]}: {states[item.transaction_cache]} · {item.cached_accounts} cached accounts"
            )
        if value.coverage:
            lines.append(human(value.coverage))
        return "\n".join(lines)
    if isinstance(value, Coverage):
        lines = [
            "Stored bank transaction coverage · "
            + ("pending included" if value.include_pending else "posted only")
        ]
        for row in value.records:
            name = f"{row.institution or 'Connection'} · {row.connection_id}"
            if row.account_id:
                name += f" / {row.account_name or 'Account'} · {row.account_id}"
            span = (
                f"{row.earliest_date} through {row.latest_date}"
                if row.earliest_date
                else "no stored dates"
            )
            lines.append(f"{name}: {row.count} · {span}")
        lines.append("Stored coverage does not establish complete institution history.")
        if value.next_offset is not None:
            lines.append(f"More groups available at offset {value.next_offset}.")
        return "\n".join(lines)
    if isinstance(value, SyncAllResult):
        lines = [
            f"Transaction sync: {value.outcome}"
            if value.eligible_connections
            else "No eligible connections for bank transaction sync."
        ]
        for row in value.connections:
            name = f"{row.institution or 'Connection'} · {row.connection_id}"
            if row.result:
                lines.append(
                    f"{name}: {row.result.added} added, {row.result.modified} changed, {row.result.removed} removed · fetched {row.result.fetched_at}"
                )
            else:
                lines.append(f"{name}: {row.outcome} · {row.error or row.reason}")
        return "\n".join(lines)
    if isinstance(value, Config):
        return f"Environment: {value.environment}\nProducts: {', '.join(value.products)}\nLink listener: {value.link_host}:{value.link_port}\nInitial transaction history requested: {value.history_days} days"
    if isinstance(value, Connection):
        return f"{value.institution or 'Connection'} · {value.id}\n  Last transaction sync: {value.last_transaction_sync or 'Never'}"
    if isinstance(value, Account):
        mask = f" · ••{value.mask}" if value.mask else ""
        updated = (
            value.provider_updated_at.strftime("%b %d, %Y %H:%M %Z")
            if value.provider_updated_at
            else "not supplied"
        )
        return f"{value.name}{mask}\n  Account: {value.id} · Connection: {value.connection_id}\n  Current: {amount(value.current, value.currency)} · Available: {amount(value.available, value.currency)}\n  Fetched from Plaid: {value.fetched_at:%b %d, %Y %H:%M %Z}\n  Provider balance update: {updated}"
    if isinstance(value, Transaction):
        pending = " · pending" if value.pending else ""
        account = f" · {value.account_name or value.account_id}" + (
            f" ••{value.account_mask}" if value.account_mask else ""
        )
        return f"{value.date:%b %d, %Y}  {value.direction.title():6} {amount(abs(value.amount), value.currency):>18}  {value.merchant or value.name}{account}{pending}"
    if isinstance(value, Transactions):
        fetched = [record.fetched_at for record in value.records]
        freshness = (
            f"\nCached records fetched between {min(fetched):%b %d, %Y %H:%M %Z} and {max(fetched):%b %d, %Y %H:%M %Z}."
            if fetched
            else "\nNo cached transactions on this page."
        )
        return (
            human(value.records)
            + f"\nShowing {len(value.records)} of {value.total}."
            + (f" Next: --offset {value.next_offset}" if value.next_offset is not None else "")
            + freshness
        )
    if isinstance(value, SyncResult):
        return f"Synchronized {value.connection_id}: {value.added} added, {value.modified} changed, {value.removed} removed."
    if isinstance(value, Cashflow):
        lines = [f"Cashflow · {value.start:%b %d, %Y}–{value.end:%b %d, %Y}", value.meaning]
        for currency in value.currencies:
            lines.append(
                f"\n{currency.currency}: Credits {amount(currency.credits, currency.currency)} · Debits {amount(currency.debits, currency.currency)} · Net {amount(currency.net, currency.currency)}"
            )
            for group in currency.groups:
                source = (
                    f" · account {group.account_id} · connection {group.connection_id}"
                    if group.account_id
                    else ""
                )
                lines.append(
                    f"  {group.name}{source}: credits {number(group.credits)} · debits {number(group.debits)}"
                )
        if not value.currencies:
            lines.append(
                "No totals with a known currency."
                if value.unknown_currency_count
                else "No matching transactions."
            )
        if value.unknown_currency_count:
            lines.append(
                f"{value.unknown_currency_count} transactions excluded from totals because their currency is unavailable."
            )
        if value.oldest_fetch:
            lines.append(
                f"\nCached records fetched between {value.oldest_fetch:%b %d, %Y %H:%M %Z} and {value.newest_fetch:%b %d, %Y %H:%M %Z}."
            )
        return "\n".join(lines)
    if isinstance(value, Holdings):
        accounts = {account.id: account for account in value.accounts}
        lines = [
            f"Holdings · connection {value.connection_id} · fetched {value.fetched_at:%b %d, %Y %H:%M %Z}"
        ]
        for identity in dict.fromkeys(record.account_id for record in value.records):
            account = accounts.get(identity)
            mask = f" ••{account.mask}" if account and account.mask else ""
            lines.append(f"\n{account.name if account else 'Account'}{mask} · account {identity}")
            for record in value.records:
                if record.account_id == identity:
                    lines.append(
                        f"  {record.ticker or record.name or record.security_id}: {number(record.quantity)} · {amount(record.value, record.currency)}"
                    )
        if not value.records:
            lines.append("No holdings reported in this snapshot.")
        return "\n".join(lines)
    if isinstance(value, Liabilities):
        accounts = {x.id: x for x in value.accounts}
        lines = [
            f"Liabilities · connection {value.connection_id} · fetched {value.fetched_at:%b %d, %Y %H:%M %Z}"
        ]
        for liability in value.records:
            account = accounts.get(liability.account_id)
            name = account.name if account else liability.type.title()
            if account and account.mask:
                name += f" ••{account.mask}"
            currency = account.currency if account else "unknown"
            due = liability.next_payment_due_date
            payment = (
                liability.minimum_payment_amount
                if liability.minimum_payment_amount is not None
                else liability.next_monthly_payment
            )
            lines.append(
                f"  {name} · account {liability.account_id}: payment {amount(payment, currency)}"
                + (f" · due {due:%b %d, %Y}" if due else "")
            )
        return "\n".join(lines)
    if isinstance(value, InvestmentRefresh):
        return f"Cached {value.count} investment transactions for {value.start:%b %d, %Y}–{value.end:%b %d, %Y}."
    if isinstance(value, AccountsSnapshot):
        return human(value.accounts)
    if isinstance(value, SnapshotRecord):
        return f"Snapshot · {value.fetched_at:%b %d, %Y %H:%M %Z}\n" + human(value.data)
    if isinstance(value, Snapshots):
        return human(
            [
                SnapshotInfo(
                    id=x.id, connection_id=x.connection_id, kind=x.kind, fetched_at=x.fetched_at
                )
                for x in value.records
            ]
        ) + (f"\nNext: --offset {value.next_offset}" if value.next_offset is not None else "")
    if isinstance(value, SnapshotInfo):
        return f"{value.fetched_at:%b %d, %Y %H:%M %Z}  {value.kind.title()} · {value.connection_id} · {value.id}"
    if isinstance(value, str):
        return value
    raise TypeError(f"No human renderer for {type(value).__name__}")
