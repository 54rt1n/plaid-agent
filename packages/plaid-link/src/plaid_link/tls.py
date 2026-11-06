"""Certificate loading for the local Link listener and its authenticated reuse probe."""

import ssl
from pathlib import Path

from plaid_core.config import Config
from plaid_core.errors import FinanceError


def certificate_files(
    config: Config, certfile: str | None, keyfile: str | None
) -> tuple[str | None, str | None]:
    if certfile is None and keyfile is None:
        certfile, keyfile = config.link_certfile, config.link_keyfile
    if bool(certfile) != bool(keyfile):
        raise FinanceError("Supply both --certfile and --keyfile for HTTPS")
    if certfile is None:
        return None, None
    certfile = str(Path(certfile).expanduser().resolve())
    keyfile = str(Path(keyfile).expanduser().resolve())
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile, password="")
    except (OSError, ssl.SSLError) as exc:
        raise FinanceError(
            "Cannot load TLS certificate and key; check the files and matching key"
        ) from exc
    return certfile, keyfile


def listener_trust(certfile: str) -> ssl.SSLContext:
    # Trust this listener's configured certificate, including an internal-CA leaf.
    # Hostname validation remains enabled; a different listener is never trusted.
    context = ssl.create_default_context(cafile=certfile)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return context
