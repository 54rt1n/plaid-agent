"""Narrow Plaid HTTP boundary, with credentials kept out of error messages."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

import httpx

from .config import Config, Credentials
from .errors import ProviderError

ENDPOINTS = {
    "/link/token/create",
    "/item/public_token/exchange",
    "/item/get",
    "/item/remove",
    "/accounts/get",
    "/accounts/balance/get",
    "/transactions/sync",
    "/investments/holdings/get",
    "/investments/transactions/get",
    "/liabilities/get",
    "/sandbox/public_token/create",
}


class PlaidClient:
    def __init__(self, config: Config, credentials: Credentials, transport=None):
        self.credentials = credentials
        self.http = httpx.Client(
            base_url=f"https://{config.environment}.plaid.com",
            headers={"Plaid-Version": "2020-09-14"},
            timeout=45,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.http.close()

    def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if endpoint not in ENDPOINTS:
            raise ValueError("Unsupported Plaid endpoint")
        body = {
            **payload,
            "client_id": self.credentials.client_id.get_secret_value(),
            "secret": self.credentials.secret.get_secret_value(),
        }
        try:
            response = self.http.post(endpoint, json=body)
        except httpx.HTTPError as exc:
            raise ProviderError("NETWORK_ERROR") from exc
        try:
            result = json.loads(response.text, parse_float=Decimal)
            if not isinstance(result, dict):
                raise ValueError("Expected object")
        except (ValueError, UnicodeError) as exc:
            raise ProviderError("INVALID_RESPONSE") from exc
        if response.is_error or response.is_redirect or "error_code" in result:
            code = str(result.get("error_code", "HTTP_ERROR"))
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                code = "PROVIDER_ERROR"
            raise ProviderError(
                code, outcome_unknown=response.status_code >= 500 or response.is_redirect
            )
        return result
