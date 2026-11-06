"""Application configuration and explicit initialization."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from ipaddress import IPv6Address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from .errors import FinanceError
from .models import DTO, Product


class Config(DTO):
    environment: Literal["sandbox", "production"] = "sandbox"
    products: tuple[Product, ...] = ("transactions",)
    countries: tuple[str, ...] = ("US",)
    history_days: int = Field(default=730, ge=1, le=730)
    client_name: str = "Personal Plaid Agent"
    link_host: str = Field(default="127.0.0.1", min_length=1)
    link_port: int = Field(default=8765, ge=1, le=65535)
    link_certfile: str | None = None
    link_keyfile: str | None = None
    user_id: str = Field(default_factory=lambda: str(uuid4()))

    @field_validator("link_certfile", "link_keyfile")
    @classmethod
    def certificate_path(cls, value):
        if value is None:
            return None
        if not value.strip():
            raise ValueError("supply a certificate or key file path")
        return str(Path(value).expanduser().resolve())

    @model_validator(mode="after")
    def certificate_pair(self):
        if bool(self.link_certfile) != bool(self.link_keyfile):
            raise ValueError("supply both link_certfile and link_keyfile")
        return self

    @field_validator("products", "countries")
    @classmethod
    def not_empty(cls, value):
        if not value or len(value) != len(set(value)):
            raise ValueError("supply at least one distinct value")
        return value

    @field_validator("countries")
    @classmethod
    def valid_countries(cls, value):
        if any(len(x) != 2 or not x.isascii() or not x.isalpha() or x != x.upper() for x in value):
            raise ValueError("countries must be uppercase ISO country codes")
        return value


class Credentials(DTO):
    client_id: SecretStr
    secret: SecretStr

    @classmethod
    def from_environment(
        cls, environment: Literal["sandbox", "production"], env_file: Path | None = None
    ):
        values = environment_values(env_file)
        client_id = (values.get("PLAID_CLIENT_ID") or "").strip()
        secret_name = "PLAID_SANDBOX_SECRET" if environment == "sandbox" else "PLAID_CLIENT_SECRET"
        secret = (values.get(secret_name) or "").strip()
        if not client_id or not secret:
            raise FinanceError(
                f"Set PLAID_CLIENT_ID and {secret_name} for {environment} Plaid requests"
            )
        return cls(client_id=SecretStr(client_id), secret=SecretStr(secret))


def environment_values(env_file: Path | None) -> dict:
    values = {}
    if env_file is not None:
        if not env_file.is_file() or env_file.stat().st_mode & 0o077:
            raise FinanceError("Credential file must exist and be private (chmod 600)")
        values = dotenv_values(env_file, interpolate=False)
    values.update(os.environ)
    return values


class LinkSettings(DTO):
    callback_uri: str

    @field_validator("callback_uri")
    @classmethod
    def valid_callback(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/oauth-return"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "OAUTH_CALLBACK_URI must be an HTTP(S) URL ending in /oauth-return without credentials, query or fragment"
            )
        host = parsed.hostname
        host = (
            f"[{IPv6Address(host).compressed}]"
            if ":" in host
            else host.encode("idna").decode("ascii").lower()
        )
        port = parsed.port
        if port == 0:
            raise ValueError("OAUTH_CALLBACK_URI port must be between 1 and 65535")
        suffix = (
            f":{port}"
            if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]
            else ""
        )
        return f"{parsed.scheme}://{host}{suffix}/oauth-return"

    @property
    def origin(self) -> str:
        return self.callback_uri.removesuffix("/oauth-return")

    @classmethod
    def from_environment(
        cls, environment: Literal["sandbox", "production"], env_file: Path | None = None
    ):
        callback = (environment_values(env_file).get("OAUTH_CALLBACK_URI") or "").strip()
        if not callback:
            if environment == "production":
                raise FinanceError(
                    "Set OAUTH_CALLBACK_URI to the registered HTTPS /oauth-return URL"
                )
            callback = "http://127.0.0.1:8765/oauth-return"
        result = cls(callback_uri=callback)
        if environment == "production" and not result.origin.startswith("https://"):
            raise FinanceError("Production OAUTH_CALLBACK_URI requires HTTPS")
        return result


def default_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "plaid-agent"


def read_config(root: Path) -> Config:
    path = root / "config.toml"
    if not path.is_file():
        raise FinanceError(f"Application is not initialized at {root}; run plaid-agent init")
    try:
        values = tomllib.loads(path.read_text())
        # Old roots stored the Link origin here; callback configuration now lives in .env.
        values.pop("origin", None)
        return Config.model_validate(values)
    except ValidationError as exc:
        reasons = "; ".join(
            ".".join(map(str, item["loc"])) + " (" + item["type"] + ")"
            for item in exc.errors(include_input=False, include_context=False)
        )
        raise FinanceError(f"Invalid config {path}: {reasons}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise FinanceError(f"Invalid TOML in {path}") from exc
    except OSError as exc:
        raise FinanceError(f"Cannot read config {path}: {exc.strerror or 'I/O error'}") from exc


def initialize(root: Path, config: Config) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = "\n".join(
        f"{key} = {json.dumps(value)}"
        for key, value in config.model_dump(mode="json", exclude_none=True).items()
    )
    try:
        fd = os.open(root / "config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise FinanceError(
            "config.toml already exists; initialization will not overwrite it"
        ) from exc
    with os.fdopen(fd, "w") as stream:
        stream.write(content + "\n")


def update_config(root: Path, config: Config) -> None:
    """Replace configuration only after its selected environment is usable."""
    from .store import initialize_store

    read_config(root)
    initialize_store(root / config.environment)
    content = "\n".join(
        f"{key} = {json.dumps(value)}"
        for key, value in config.model_dump(mode="json", exclude_none=True).items()
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content + "\n")
        os.replace(temporary, root / "config.toml")
    finally:
        Path(temporary).unlink(missing_ok=True)
