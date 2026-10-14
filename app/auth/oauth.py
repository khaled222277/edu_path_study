"""Authlib client for Google OAuth2 / OpenID Connect."""

from authlib.integrations.starlette_client import OAuth

from app.config import settings

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"

oauth = OAuth()

oauth.register(
    name="google",
    client_id=settings.google_client_id,
    client_secret=settings.google_client_secret,
    server_metadata_url=GOOGLE_METADATA_URL,
    client_kwargs={"scope": "openid email profile"},
)
