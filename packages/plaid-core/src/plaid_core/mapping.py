"""Explicit projections from Plaid responses to public contracts."""

from datetime import datetime

from .models import APR, Account, Holding, InvestmentDetail, Liabilities, Liability, Transaction


def currency(payload: dict) -> str:
    if payload.get("iso_currency_code"):
        return payload["iso_currency_code"]
    if payload.get("unofficial_currency_code"):
        return "unofficial:" + payload["unofficial_currency_code"]
    return "unknown"


def account(payload: dict, connection_id: str, fetched_at: datetime) -> Account:
    balances = payload["balances"]
    return Account(
        id=payload["account_id"],
        connection_id=connection_id,
        name=payload["name"],
        official_name=payload.get("official_name"),
        mask=payload.get("mask"),
        type=payload["type"],
        subtype=payload.get("subtype"),
        currency=currency(balances),
        current=balances.get("current"),
        available=balances.get("available"),
        limit=balances.get("limit"),
        provider_updated_at=balances.get("last_updated_datetime"),
        fetched_at=fetched_at,
    )


def transaction(payload: dict, connection_id: str, fetched_at: datetime) -> Transaction:
    category = payload.get("personal_finance_category") or {}
    return Transaction(
        id=payload["transaction_id"],
        connection_id=connection_id,
        account_id=payload["account_id"],
        date=payload["date"],
        authorized_date=payload.get("authorized_date"),
        name=payload["name"],
        merchant=payload.get("merchant_name"),
        amount=payload["amount"],
        currency=currency(payload),
        pending=payload["pending"],
        pending_transaction_id=payload.get("pending_transaction_id"),
        category=category.get("primary"),
        original_description=payload.get("original_description"),
        payment_channel=payload.get("payment_channel"),
        fetched_at=fetched_at,
    )


def holding(payload: dict, securities: dict) -> Holding:
    security = securities.get(payload["security_id"], {})
    return Holding(
        account_id=payload["account_id"],
        security_id=payload["security_id"],
        name=security.get("name"),
        ticker=security.get("ticker_symbol"),
        quantity=payload["quantity"],
        price=payload.get("institution_price"),
        value=payload.get("institution_value"),
        cost_basis=payload.get("cost_basis"),
        currency=currency(payload),
    )


def investment_transaction(payload: dict, connection_id: str, fetched_at: datetime) -> Transaction:
    return Transaction(
        id=payload["investment_transaction_id"],
        connection_id=connection_id,
        source_kind="investment",
        account_id=payload["account_id"],
        date=payload["date"],
        name=payload["name"],
        amount=payload["amount"],
        currency=currency(payload),
        pending=payload.get("subtype") in {"pending credit", "pending debit"},
        fetched_at=fetched_at,
        investment=InvestmentDetail(
            security_id=payload.get("security_id"),
            type=payload["type"],
            subtype=payload.get("subtype"),
            fees=payload.get("fees"),
            price=payload.get("price"),
            quantity=payload.get("quantity"),
        ),
    )


def liabilities(payload: dict, connection_id: str, fetched_at: datetime) -> Liabilities:
    records = []
    for kind, sources in payload["liabilities"].items():
        for source in sources or []:
            values = {key: source[key] for key in Liability.model_fields if key in source}
            values["aprs"] = [
                APR(**{key: apr[key] for key in APR.model_fields if key in apr})
                for apr in source.get("aprs") or []
            ]
            if kind == "mortgage":
                values["interest_rate_percentage"] = (source.get("interest_rate") or {}).get(
                    "percentage"
                )
            records.append(Liability(type=kind, **values))
    return Liabilities(
        connection_id=connection_id,
        accounts=[account(x, connection_id, fetched_at) for x in payload["accounts"]],
        records=records,
        fetched_at=fetched_at,
    )
