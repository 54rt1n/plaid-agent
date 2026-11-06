"""Browser connection command and ownership of temporary Link listeners."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import uvicorn
from plaid_core.application import Finance
from plaid_core.config import Config, LinkSettings, read_config
from plaid_core.errors import FinanceError
from plaid_core.models import DTO, Connection
from pydantic import Field, SecretStr

from .server import COOKIE, create_app
from .tls import certificate_files, listener_trust


class RunningLink(DTO):
    origin: str
    host: str
    port: int
    environment: str
    token: SecretStr = Field(repr=False)
    finished_setup: str | None = None
    scheme: Literal["http", "https"] = "http"
    certfile: str | None = None

    @property
    def url(self) -> str:
        return self.origin + "/#setup=" + self.token.get_secret_value()


def _existing(
    root: Path, config: Config, settings: LinkSettings, certfile: str | None = None
) -> RunningLink | None:
    path = root / "link-server.json"
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise FinanceError("Link runtime file must be private (chmod 600)")
    values = json.loads(path.read_text())
    # Earlier stopped listeners left these completion fields in their runtime record.
    values.pop("completed_session", None)
    values.pop("connection_id", None)
    runtime = RunningLink.model_validate(values)
    if runtime.origin != settings.origin or runtime.environment != config.environment:
        raise FinanceError(
            "Stop the running Link server before changing its environment or callback"
        )
    scheme = "https" if certfile else "http"
    if runtime.scheme != scheme or runtime.certfile != certfile:
        raise FinanceError("Stop the running Link server before changing its TLS configuration")
    local_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(runtime.host, runtime.host)
    host = local_host
    host = f"[{host}]" if ":" in host else host
    headers = {"host": urlsplit(settings.origin).netloc, "origin": settings.origin}
    extensions = {"sni_hostname": urlsplit(settings.origin).hostname}
    try:
        with httpx.Client(
            base_url=f"{scheme}://{host}:{runtime.port}",
            headers=headers,
            timeout=2,
            trust_env=False,
            verify=listener_trust(certfile) if certfile else True,
        ) as client:
            response = client.post(
                "/api/session",
                json={"token": runtime.token.get_secret_value()},
                extensions=extensions,
            )
            if response.status_code != 204:
                raise FinanceError(
                    "The listener does not match the saved Link server; stop it before reconnecting"
                )
            client.headers["cookie"] = f"{COOKIE}={client.cookies.get(COOKIE)}"
            status = client.get("/api/status", extensions=extensions)
            if status.status_code != 200 or status.json().get("environment") != config.environment:
                raise FinanceError("Cannot verify the running Link server for this data directory")
            client.delete("/api/session", extensions=extensions)
    except httpx.ConnectError as exc:
        try:
            with socket.create_connection((local_host, runtime.port), timeout=2):
                pass
        except ConnectionRefusedError:
            path.unlink()
            return None
        except OSError:
            raise FinanceError("Cannot verify the saved Link listener") from exc
        raise FinanceError("Cannot verify the Link listener's TLS certificate or identity") from exc
    except httpx.HTTPError as exc:
        raise FinanceError("The running Link server is not responding") from exc
    return runtime


@contextmanager
def running_server(
    root: Path,
    *,
    env_file: Path | None,
    host: str,
    port: int,
    certfile: str | None = None,
    keyfile: str | None = None,
    on_finished: Callable[[], None] | None = None,
):
    if not host or not 1 <= port <= 65535:
        raise FinanceError("Link requires a host and a port between 1 and 65535")
    config = read_config(root)
    with Finance(root, env_file=env_file) as finance:
        settings = LinkSettings.from_environment(config.environment, finance.env_file)
    certfile, keyfile = certificate_files(config, certfile, keyfile)
    if certfile and not settings.origin.startswith("https://"):
        raise FinanceError("An HTTPS listener requires an HTTPS OAUTH_CALLBACK_URI")
    runtime = RunningLink(
        origin=settings.origin,
        host=host,
        port=port,
        environment=config.environment,
        token=secrets.token_urlsafe(32),
        scheme="https" if certfile else "http",
        certfile=certfile,
    )
    address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    listener = socket.socket(address[0], address[1], address[2])
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    path = root / "link-server.json"
    published = False
    server = None
    thread = None
    try:
        try:
            listener.bind(address[4])
        except OSError as exc:
            raise FinanceError(
                "Link listener is unavailable; stop the existing server or choose another --port"
            ) from exc

        def finished():
            updated = runtime.model_copy(update={"finished_setup": str(uuid4())})
            values = updated.model_dump(mode="json")
            values["token"] = runtime.token.get_secret_value()
            descriptor, temporary = tempfile.mkstemp(prefix=".link-server-", dir=root)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(values, stream)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)

            if on_finished is not None:
                on_finished()

        app = create_app(
            root,
            setup_token=runtime.token.get_secret_value(),
            env_file=env_file,
            on_finished=finished,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                proxy_headers=False,
                log_level="warning",
                timeout_graceful_shutdown=5,
                ssl_certfile=certfile,
                ssl_keyfile=keyfile,
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise FinanceError("Link server could not start")
            time.sleep(0.02)
        values = runtime.model_dump(mode="json")
        values["token"] = runtime.token.get_secret_value()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise FinanceError("Another Link server owns this data directory") from exc
        published = True
        with os.fdopen(descriptor, "w") as stream:
            json.dump(values, stream)
        yield runtime, server, thread
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        listener.close()
        if published:
            path.unlink(missing_ok=True)


def _open(runtime: RunningLink, *, open_browser: bool) -> None:
    print(f"Connect an institution in your browser:\n{runtime.url}", file=sys.stderr, flush=True)
    if open_browser and not webbrowser.open(runtime.url):
        print("Open the private URL above in your browser to continue.", file=sys.stderr)


def connect(
    root: Path,
    *,
    env_file: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    certfile: str | None = None,
    keyfile: str | None = None,
    open_browser: bool = True,
) -> list[Connection]:
    config = read_config(root)
    with Finance(root, env_file=env_file) as finance:
        settings = LinkSettings.from_environment(config.environment, finance.env_file)
        certfile, keyfile = certificate_files(config, certfile, keyfile)
        existing = _existing(root, config, settings, certfile)
        if existing is not None:
            _open(existing, open_browser=open_browser)
            try:
                while True:
                    path = root / "link-server.json"
                    if not path.exists():
                        raise FinanceError("The existing Link server stopped before completion")
                    current = RunningLink.model_validate_json(path.read_text())
                    if current.token != existing.token:
                        raise FinanceError("The existing Link server restarted; run connect again")
                    if current.finished_setup != existing.finished_setup:
                        return finance.list_connections()
                    time.sleep(0.2)
            except KeyboardInterrupt:
                raise FinanceError(
                    "Connection cancelled; the existing server is still running"
                ) from None
        completed = threading.Event()
        try:
            with running_server(
                root,
                env_file=env_file,
                host=host or config.link_host,
                port=port if port is not None else config.link_port,
                certfile=certfile,
                keyfile=keyfile,
                on_finished=completed.set,
            ) as (runtime, _server, thread):
                _open(runtime, open_browser=open_browser)
                while not completed.wait(0.2):
                    if not thread.is_alive():
                        raise FinanceError("Link server stopped before setup was finished")
                return finance.list_connections()
        except KeyboardInterrupt:
            raise FinanceError("Connection cancelled; temporary server stopped") from None
