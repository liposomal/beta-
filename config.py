"""Secrets d'hébergement. La configuration Discord est enregistrée en base."""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    sentinelle_token: str
    oauth2_client_secret: str
    oauth2_redirect_uri: str
    supabase_url: str
    supabase_service_role_key: str
    token_encryption_key: str
    http_host: str = "127.0.0.1"
    http_port: int = 8080
    rate_limit_delay: float = 0.25
    evacuation_concurrency: int = 3

    @classmethod
    def load(cls):
        load_dotenv()
        def required(name):
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"Variable manquante : {name}")
            return value
        result = cls(
            sentinelle_token=required("SENTINELLE_TOKEN"),
            oauth2_client_secret=required("OAUTH2_CLIENT_SECRET"),
            oauth2_redirect_uri=required("OAUTH2_REDIRECT_URI"),
            supabase_url=required("SUPABASE_URL").rstrip("/"),
            supabase_service_role_key=required("SUPABASE_SERVICE_ROLE_KEY"),
            token_encryption_key=required("TOKEN_ENCRYPTION_KEY"),
            http_host=os.getenv("HTTP_HOST", "127.0.0.1"),
            http_port=int(os.getenv("HTTP_PORT", "8080")),
            rate_limit_delay=float(os.getenv("RATE_LIMIT_DELAY", "0.25")),
            evacuation_concurrency=int(os.getenv("EVACUATION_CONCURRENCY", "3")),
        )
        redirect = urlsplit(result.oauth2_redirect_uri)
        if redirect.path != "/callback" or redirect.query or redirect.fragment:
            raise ValueError("OAUTH2_REDIRECT_URI doit se terminer par /callback, sans paramètres.")
        if redirect.scheme != "https" and not (
            redirect.scheme == "http" and redirect.hostname in {"localhost", "127.0.0.1"}
        ):
            raise ValueError("Le callback public doit utiliser HTTPS.")
        if not 1 <= result.http_port <= 65535 or not 1 <= result.evacuation_concurrency <= 10:
            raise ValueError("Port ou concurrence invalide (concurrence : 1 à 10).")
        if result.rate_limit_delay < 0.1:
            raise ValueError("RATE_LIMIT_DELAY doit être >= 0.1.")
        return result


def get_config():
    return Config.load()
