"""Public application contracts. Provider payloads never cross this boundary unchanged."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Money = Annotated[Decimal, Field(allow_inf_nan=False)]
Product = Literal["transactions", "investments", "liabilities"]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Connection(DTO):
    id: str
    institution: str | None = None
    products: tuple[Product, ...]
    connected_at: datetime
    last_transaction_sync: datetime | None = None


class Account(DTO):
    id: str
    connection_id: str
    name: str
    official_name: str | None = None
    mask: str | None = None
    type: str
    subtype: str | None = None
    currency: str
    current: Money | None = None
    available: Money | None = None
    limit: Money | None = None
    fetched_at: datetime
    provider_updated_at: datetime | None = None


class InvestmentDetail(DTO):
    security_id: str | None = None
    type: str
    subtype: str | None = None
    fees: Money | None = None
    price: Money | None = None
    quantity: Money | None = None


class Transaction(DTO):
    id: str
    source_kind: Literal["bank", "investment"] = "bank"
    connection_id: str
    account_id: str
    account_name: str | None = None
    account_mask: str | None = None
    institution: str | None = None
    date: date
    authorized_date: date | None = None
    name: str
    merchant: str | None = None
    amount: Money
    currency: str
    pending: bool
    pending_transaction_id: str | None = None
    category: str | None = None
    original_description: str | None = None
    payment_channel: str | None = None
    fetched_at: datetime
    details: dict[str, Any] | None = None
    investment: InvestmentDetail | None = None

    direction: Literal["debit", "credit", "neutral"] = "neutral"
    debit: Money = Decimal(0)
    credit: Money = Decimal(0)

    @model_validator(mode="before")
    @classmethod
    def debit_credit_from_amount(cls, value):
        if not isinstance(value, dict) or "amount" not in value:
            return value
        try:
            amount = Decimal(str(value["amount"]))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("amount must be a finite decimal") from exc
        if not amount.is_finite():
            raise ValueError("amount must be a finite decimal")
        return {
            **value,
            "debit": max(amount, Decimal(0)),
            "credit": max(-amount, Decimal(0)),
            "direction": "debit" if amount > 0 else "credit" if amount < 0 else "neutral",
        }


class TransactionQuery(DTO):
    start: date | None = None
    end: date | None = None
    connection_id: str | None = None
    account_id: str | None = None
    text: str | None = None
    currency: str | None = None
    source_kind: Literal["bank", "investment"] = "bank"
    direction: Literal["debit", "credit"] | None = None
    category: str | None = None
    include_details: bool = False
    include_pending: bool = False
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


class Transactions(DTO):
    records: list[Transaction]
    total: int
    limit: int
    offset: int
    next_offset: int | None


class CoverageQuery(DTO):
    group_by: Literal["connection", "account"] = "connection"
    connection_id: str | None = None
    include_pending: bool = False
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class CoverageRow(DTO):
    connection_id: str
    institution: str | None = None
    account_id: str | None = None
    account_name: str | None = None
    count: int
    earliest_date: date | None = None
    latest_date: date | None = None


class Coverage(DTO):
    records: list[CoverageRow]
    group_by: Literal["connection", "account"]
    include_pending: bool
    total: int
    offset: int
    next_offset: int | None
    meaning: str = "Stored bank transaction coverage; not proof of complete institution history."


class CashflowQuery(DTO):
    start: date
    end: date
    connection_id: str | None = None
    account_id: str | None = None
    source_kind: Literal["bank", "investment"] = "bank"
    include_pending: bool = False
    group_by: Literal["category", "merchant", "account"] = "category"

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


class FlowGroup(DTO):
    name: str
    account_id: str | None = None
    connection_id: str | None = None
    credits: Money
    debits: Money
    count: int


class CurrencyFlow(DTO):
    currency: str
    credits: Money
    debits: Money
    net: Money
    count: int
    groups: list[FlowGroup]


class Cashflow(DTO):
    start: date
    end: date
    include_pending: bool
    currencies: list[CurrencyFlow]
    unknown_currency_count: int = 0
    oldest_fetch: datetime | None = None
    newest_fetch: datetime | None = None
    meaning: str = "Gross account flows; transfers, refunds and payments are not classified as income or expenses."


class AccountsSnapshot(DTO):
    connection_id: str
    accounts: list[Account]
    fetched_at: datetime
    complete: bool = True


class SnapshotInfo(DTO):
    id: str
    connection_id: str
    kind: Literal["balances", "holdings", "liabilities"]
    fetched_at: datetime


class SnapshotQuery(DTO):
    connection_id: str | None = None
    kind: Literal["balances", "holdings", "liabilities"] | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def ordered_times(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


class SyncResult(DTO):
    connection_id: str
    added: int
    modified: int
    removed: int
    fetched_at: datetime
    # Cursor is deliberately not a public result.


class ConnectionSyncOutcome(DTO):
    connection_id: str
    institution: str | None = None
    outcome: Literal["success", "failed", "skipped"]
    result: SyncResult | None = None
    error: str | None = None
    reason: str | None = None


class SyncAllResult(DTO):
    outcome: Literal["success", "partial", "failed"]
    eligible_connections: int
    connections: list[ConnectionSyncOutcome]


class Holding(DTO):
    account_id: str
    security_id: str
    name: str | None = None
    ticker: str | None = None
    quantity: Money
    price: Money | None = None
    value: Money | None = None
    cost_basis: Money | None = None
    currency: str


class Holdings(DTO):
    connection_id: str
    accounts: list[Account]
    records: list[Holding]
    fetched_at: datetime


class InvestmentRefresh(DTO):
    connection_id: str
    start: date
    end: date
    count: int
    fetched_at: datetime


class APR(DTO):
    apr_percentage: Money
    apr_type: str
    balance_subject_to_apr: Money | None = None
    interest_charge_amount: Money | None = None


class Liability(DTO):
    account_id: str
    type: str
    next_payment_due_date: date | None = None
    minimum_payment_amount: Money | None = None
    next_monthly_payment: Money | None = None
    last_payment_amount: Money | None = None
    last_payment_date: date | None = None
    last_statement_balance: Money | None = None
    last_statement_issue_date: date | None = None
    interest_rate_percentage: Money | None = None
    origination_principal_amount: Money | None = None
    outstanding_interest_amount: Money | None = None
    is_overdue: bool | None = None
    aprs: list[APR] = Field(default_factory=list)
    # Deliberately omit account numbers and address/servicer blocks.


class Liabilities(DTO):
    connection_id: str
    accounts: list[Account]
    records: list[Liability]
    fetched_at: datetime


class SnapshotRecord(SnapshotInfo):
    data: AccountsSnapshot | Holdings | Liabilities
    details: dict[str, Any] | None = None


class Snapshots(DTO):
    records: list[SnapshotRecord]
    total: int
    offset: int
    next_offset: int | None


class ConnectionReadiness(DTO):
    connection_id: str
    cached_accounts: int
    cached_posted_transactions: int
    transaction_cache: Literal["not_enabled", "not_synced", "cached_records", "no_cached_records"]
    history_loading: Literal["unknown"] = "unknown"


class Status(DTO):
    environment: Literal["sandbox", "production"]
    initialized: bool
    products: tuple[Product, ...]
    connections: list[Connection]
    data_dir: str | None = None
    readiness: list[ConnectionReadiness] = Field(default_factory=list)
    coverage: Coverage | None = None


class LinkSession(DTO):
    id: str
    link_token: str = Field(repr=False)
    expiration: datetime
    mode: Literal["connect", "update"]


class LinkState(DTO):
    id: str
    connection_id: str | None
    expires: AwareDatetime
    mode: Literal["connect", "update"]
    products: tuple[Product, ...]
    token: str = Field(repr=False)
    origin: str | None = None
    status: Literal["pending", "processing", "completed"] = "pending"
    result_connection_id: str | None = None
