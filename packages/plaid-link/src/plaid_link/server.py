"""One HTTP service for the website and its authenticated Link endpoints."""

import secrets
import time
import webbrowser
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from plaid_core.application import Finance
from plaid_core.client import PlaidClient
from plaid_core.config import Credentials, LinkSettings, read_config
from plaid_core.errors import FinanceError, ProviderError
from plaid_core.link import Link
from plaid_core.models import DTO, Connection, LinkSession, Status
from pydantic import Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

COOKIE = "plaid_session"


class Login(DTO):
    token: SecretStr


class Start(DTO):
    connection_id: str | None = None


class Finish(DTO):
    session_id: str = Field(min_length=1, max_length=100)
    public_token: SecretStr | None = None
    institution_name: str | None = Field(default=None, max_length=200)


def create_app(
    root: Path,
    *,
    setup_token: str,
    env_file: Path | None = None,
    transport=None,
    on_finished: Callable[[], None] | None = None,
) -> FastAPI:
    config = read_config(root)
    profile_env = root / ".env"
    env_file = env_file or (profile_env if profile_env.is_file() else None)
    settings = LinkSettings.from_environment(config.environment, env_file)
    sessions: dict[str, float] = {}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        sessions.clear()

    app = FastAPI(
        title="Plaid Agent Link", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    public_host = urlsplit(settings.origin).hostname
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[f"[{public_host}]" if ":" in public_host else public_host],
    )

    @app.middleware("http")
    async def response_policy(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != settings.origin:
                return JSONResponse({"error": "Request origin is not permitted"}, status_code=403)
            # Bound input before JSON parsing; no provider responses or tokens in logs.
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"error": "Invalid content length"}, status_code=400)
            if request.method == "POST" and "content-length" not in request.headers:
                return JSONResponse({"error": "Content length is required"}, status_code=411)
            if length < 0:
                return JSONResponse({"error": "Invalid content length"}, status_code=400)
            if length > 16384:
                return JSONResponse({"error": "Request is too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.plaid.com; "
            "style-src 'self' 'unsafe-inline'; frame-src https://cdn.plaid.com; "
            f"connect-src 'self' https://{config.environment}.plaid.com; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        # FastAPI's default body includes rejected input, which may contain tokens.
        return JSONResponse({"error": "Invalid request fields"}, status_code=422)

    @app.exception_handler(FinanceError)
    async def finance_error(_request, exc):
        return JSONResponse(
            {"error": str(exc)}, status_code=502 if isinstance(exc, ProviderError) else 400
        )

    async def authenticated(request: Request):
        supplied = request.cookies.get(COOKIE, "")
        expires = sessions.get(supplied, 0)
        if expires <= time.monotonic():
            sessions.pop(supplied, None)
            raise HTTPException(
                401, detail="Run the same connect command again to open a signed-in browser session"
            )

    def finance():
        client = None
        if transport is not None:
            client = PlaidClient(
                config, Credentials.from_environment(config.environment, env_file), transport
            )
        with Finance(root, env_file=env_file, client=client, config=config) as service:
            try:
                yield service
            finally:
                if client:
                    client.close()

    @app.post("/api/session", status_code=204)
    async def login(body: Login, response: Response):
        if not secrets.compare_digest(body.token.get_secret_value().encode(), setup_token.encode()):
            raise HTTPException(401, detail="Invalid setup token")
        current = time.monotonic()
        for key in [key for key, expiry in sessions.items() if expiry <= current]:
            del sessions[key]
        token = secrets.token_urlsafe(32)
        sessions[token] = current + 3600
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=settings.origin.startswith("https://"),
            samesite="lax",
            max_age=3600,
            path="/",
        )

    @app.delete("/api/session", status_code=204, dependencies=[Depends(authenticated)])
    async def logout(request: Request, response: Response):
        sessions.pop(request.cookies.get(COOKIE), None)
        response.delete_cookie(COOKIE, path="/")

    @app.get("/api/status", response_model=Status, dependencies=[Depends(authenticated)])
    def status(service: Annotated[Finance, Depends(finance)]):
        return service.status()

    @app.post(
        "/api/link/sessions",
        response_model=LinkSession,
        dependencies=[Depends(authenticated)],
    )
    def start(body: Start, service: Annotated[Finance, Depends(finance)]):
        return Link(service, settings=settings).create_session(body.connection_id)

    @app.post(
        "/api/link/complete", response_model=Connection, dependencies=[Depends(authenticated)]
    )
    def complete(body: Finish, service: Annotated[Finance, Depends(finance)]):
        connection = Link(service).complete(
            body.session_id,
            body.public_token.get_secret_value() if body.public_token else None,
            institution_name=body.institution_name,
            origin=settings.origin,
        )
        return connection

    @app.post("/api/setup/finish", status_code=204, dependencies=[Depends(authenticated)])
    def finish_setup(background: BackgroundTasks):
        if on_finished is not None:
            background.add_task(on_finished)

    assets = Path(str(files("plaid_link").joinpath("static")))
    if (assets / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")

    @app.get("/")
    @app.get("/oauth-return")
    def website():
        index = assets / "index.html"
        if not index.is_file():
            raise HTTPException(
                503, detail="Website has not been built; run npm run build in packages/plaid-web"
            )
        return FileResponse(index)

    return app


def serve(
    root: Path,
    *,
    env_file: Path | None,
    host: str,
    port: int,
    open_browser: bool,
    certfile: str | None = None,
    keyfile: str | None = None,
):
    from .connect import running_server

    with running_server(
        root, env_file=env_file, host=host, port=port, certfile=certfile, keyfile=keyfile
    ) as (
        runtime,
        _server,
        thread,
    ):
        print(f"Open this private setup URL in your browser:\n{runtime.url}", flush=True)
        if open_browser:
            webbrowser.open(runtime.url)
        try:
            while thread.is_alive():
                thread.join(timeout=0.5)
        except KeyboardInterrupt:
            pass
