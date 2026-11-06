"""Connection lifecycle; secrets are available only to management callers."""

from uuid import uuid4

from .application import Finance, now
from .config import LinkSettings
from .errors import FinanceError, ProviderError
from .models import Connection, LinkSession, LinkState, Product


class Link:
    def __init__(self, finance: Finance, *, settings: LinkSettings | None = None):
        self.finance = finance
        self.settings = settings

    def create_session(self, connection_id: str | None = None) -> LinkSession:
        config = self.finance.config
        settings = self.settings or LinkSettings.from_environment(
            config.environment, self.finance.env_file
        )
        if config.environment == "production" and not settings.origin.startswith("https://"):
            raise FinanceError("Production OAUTH_CALLBACK_URI requires HTTPS")
        request = {
            "user": {"client_user_id": config.user_id},
            "client_name": config.client_name,
            "country_codes": list(config.countries),
            "language": "en",
            "redirect_uri": settings.callback_uri,
        }
        if connection_id:
            request["access_token"] = self.finance.store.connection(connection_id).token
        else:
            request["products"] = list(config.products)
            if "transactions" in config.products:
                request["transactions"] = {"days_requested": config.history_days}
        payload = self.finance.client.post("/link/token/create", request)
        try:
            public = LinkSession(
                id=str(uuid4()),
                link_token=payload["link_token"],
                expiration=payload["expiration"],
                mode="update" if connection_id else "connect",
            )
        except (KeyError, ValueError) as exc:
            raise ProviderError("INVALID_RESPONSE") from exc
        self.finance.store.save_link_session(
            LinkState(
                id=public.id,
                connection_id=connection_id,
                expires=public.expiration,
                mode=public.mode,
                products=self.finance.store.connection(connection_id).public.products
                if connection_id
                else config.products,
                token=public.link_token,
                origin=settings.origin,
            )
        )
        return public

    def complete(
        self,
        session_id: str,
        public_token: str | None = None,
        *,
        institution_name: str | None = None,
        origin: str | None = None,
    ) -> Connection:
        session = self.finance.store.link_session(session_id)
        if origin is not None and session.origin is not None and origin != session.origin:
            raise FinanceError("Complete this Link session from the site where it was started")
        if session.status == "pending":
            # Validate local credentials before taking the irreversible-exchange claim.
            client = self.finance.client
            session = self.finance.store.claim_link_session(session_id)
        if session.status == "completed":
            return self.finance.store.connection(session.result_connection_id).public
        if session.status == "processing":
            raise FinanceError(
                "Completion is in progress or its outcome is unknown. Check linked connections before starting a new Link flow."
            )
        if session.mode == "connect" and not public_token:
            self.finance.store.reset_link_session(session_id)
            raise FinanceError("A public token is required to complete a new connection")
        try:
            if session.mode == "update":
                connection = self.finance.store.connection(session.connection_id)
                payload = client.post("/item/get", {"access_token": connection.token})
                item = payload.get("item")
                if not isinstance(item, dict) or item.get("item_id") != connection.public.id:
                    raise ProviderError("INVALID_RESPONSE")
                if item.get("error"):
                    self.finance.store.reset_link_session(session_id)
                    raise FinanceError("Connection still requires reauthorization")
                result, access_token = connection.public, None
            else:
                result, access_token = self._exchange(
                    public_token, institution_name, session.products
                )
        except ProviderError as exc:
            # An uncertain exchange outcome must never trigger a second exchange.
            if session.mode == "update" or not exc.outcome_unknown:
                self.finance.store.reset_link_session(session_id)
            raise
        self.finance.store.finish_link_session(session_id, result, access_token)
        return result

    def _exchange(
        self, public_token: str, institution_name: str | None, products: tuple[Product, ...]
    ) -> tuple[Connection, str]:
        payload = self.finance.client.post(
            "/item/public_token/exchange", {"public_token": public_token}
        )
        try:
            result = Connection(
                id=payload["item_id"],
                institution=institution_name,
                products=products,
                connected_at=now(),
            )
            access_token = payload["access_token"]
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("Missing access token")
        except (KeyError, ValueError) as exc:
            raise ProviderError("INVALID_RESPONSE") from exc
        return result, access_token

    def exchange(self, public_token: str, *, institution_name: str | None = None) -> Connection:
        result, access_token = self._exchange(
            public_token, institution_name, self.finance.config.products
        )
        self.finance.store.add_connection(result, access_token)
        return result

    def sandbox_connection(self, institution_id: str) -> Connection:
        if self.finance.config.environment != "sandbox":
            raise FinanceError("Sandbox creation is unavailable in production")
        payload = self.finance.client.post(
            "/sandbox/public_token/create",
            {
                "institution_id": institution_id,
                "initial_products": list(self.finance.config.products),
            },
        )
        token = payload.get("public_token")
        if not isinstance(token, str) or not token:
            raise ProviderError("INVALID_RESPONSE")
        return self.exchange(token)
