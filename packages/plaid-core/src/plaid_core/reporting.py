"""Cash movement reporting without implicit income or transfer classification."""

from collections import defaultdict
from decimal import Decimal

from .errors import FinanceError
from .models import (
    Account,
    Cashflow,
    CashflowQuery,
    Connection,
    Coverage,
    CoverageQuery,
    CoverageRow,
    CurrencyFlow,
    FlowGroup,
    Transaction,
    TransactionQuery,
)


def coverage(
    records: list[Transaction],
    connections: list[Connection],
    accounts: list[Account],
    query: CoverageQuery,
) -> Coverage:
    owners = {item.id: item for item in connections if query.connection_id in {None, item.id}}
    if query.connection_id is not None and not owners:
        raise FinanceError("Connection not found in this environment")
    labels = {(item.connection_id, item.id): item.name for item in accounts}
    grouped: dict[tuple[str, str | None], list[Transaction]] = (
        {(identity, None): [] for identity in owners}
        if query.group_by == "connection"
        else {
            (item.connection_id, item.id): [] for item in accounts if item.connection_id in owners
        }
    )
    for record in records:
        if (
            record.connection_id not in owners
            or record.source_kind != "bank"
            or (record.pending and not query.include_pending)
        ):
            continue
        key = (record.connection_id, record.account_id if query.group_by == "account" else None)
        grouped.setdefault(key, []).append(record)
    rows = [
        CoverageRow(
            connection_id=identity,
            institution=owners[identity].institution,
            account_id=account_id,
            account_name=labels.get((identity, account_id)),
            count=len(items),
            earliest_date=min((item.date for item in items), default=None),
            latest_date=max((item.date for item in items), default=None),
        )
        for (identity, account_id), items in sorted(grouped.items())
    ]
    stop = query.offset + query.limit
    return Coverage(
        records=rows[query.offset : stop],
        group_by=query.group_by,
        include_pending=query.include_pending,
        total=len(rows),
        offset=query.offset,
        next_offset=stop if stop < len(rows) else None,
    )


def matching(records: list[Transaction], query: TransactionQuery) -> list[Transaction]:
    result = [
        record
        for record in records
        if (query.start is None or record.date >= query.start)
        and record.source_kind == query.source_kind
        and (query.end is None or record.date <= query.end)
        and (query.connection_id is None or record.connection_id == query.connection_id)
        and (query.account_id is None or record.account_id == query.account_id)
        and (query.currency is None or record.currency == query.currency)
        and (query.direction is None or record.direction == query.direction)
        and (query.category is None or record.category == query.category)
        and (query.include_pending or not record.pending)
        and (
            not query.text
            or query.text.casefold()
            in (
                record.name
                + " "
                + (record.merchant or "")
                + " "
                + (record.original_description or "")
            ).casefold()
        )
    ]
    return sorted(result, key=lambda x: (x.date, x.id), reverse=True)


def cashflow(records: list[Transaction], query: CashflowQuery) -> Cashflow:
    records = matching(records, TransactionQuery(**query.model_dump(exclude={"group_by"})))
    currencies = defaultdict(lambda: defaultdict(list))
    for record in records:
        if record.currency == "unknown":
            continue
        label = {
            "category": record.category or "Uncategorized",
            "merchant": record.merchant or record.name,
            "account": (record.connection_id, record.account_id),
        }[query.group_by]
        currencies[record.currency][label].append(record)
    totals = []
    for code, values in sorted(currencies.items()):
        groups = [
            FlowGroup(
                name=(entries[0].account_name or entries[0].account_id)
                if query.group_by == "account"
                else name,
                account_id=entries[0].account_id if query.group_by == "account" else None,
                connection_id=entries[0].connection_id if query.group_by == "account" else None,
                credits=sum((x.credit for x in entries), Decimal(0)),
                debits=sum((x.debit for x in entries), Decimal(0)),
                count=len(entries),
            )
            for name, entries in sorted(values.items())
        ]
        credits = sum((x.credits for x in groups), Decimal(0))
        debits = sum((x.debits for x in groups), Decimal(0))
        totals.append(
            CurrencyFlow(
                currency=code,
                credits=credits,
                debits=debits,
                net=credits - debits,
                count=sum(x.count for x in groups),
                groups=groups,
            )
        )
    return Cashflow(
        start=query.start,
        end=query.end,
        include_pending=query.include_pending,
        currencies=totals,
        unknown_currency_count=sum(record.currency == "unknown" for record in records),
        oldest_fetch=min((x.fetched_at for x in records), default=None),
        newest_fetch=max((x.fetched_at for x in records), default=None),
    )
