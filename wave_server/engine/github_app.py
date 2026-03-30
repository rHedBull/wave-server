"""GitHub App authentication — generate short-lived installation tokens.

Each GitHub App authenticates in two steps:
1. Sign a JWT with the app's private key (valid 10 min)
2. Exchange the JWT for an installation token via GitHub API (valid 1 hour)

Tokens are cached and auto-refreshed with a safety margin.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import jwt as pyjwt

# Refresh tokens 5 minutes before they expire
_EXPIRY_MARGIN_SECONDS = 300
# JWT lifetime (GitHub allows max 10 min)
_JWT_LIFETIME_SECONDS = 600

GITHUB_API = "https://api.github.com"

_PEM_HEADER = "-----BEGIN"


def _resolve_private_key(key_or_path: str) -> str:
    """Resolve a private key from either inline PEM content or a file path.

    If the value starts with '-----BEGIN', it's treated as inline PEM.
    Otherwise it's treated as a file path.
    """
    if key_or_path.strip().startswith(_PEM_HEADER):
        return key_or_path.strip()
    path = Path(key_or_path).expanduser()
    return path.read_text().strip()


class GitHubAppAuth:
    """Manages authentication for a single GitHub App installation.

    Usage:
        auth = GitHubAppAuth(
            app_id="123",
            private_key="<PEM content or path to .pem file>",
            installation_id="456",
        )
        token = await auth.get_token()
        # Use token in Authorization header or GH_TOKEN env var
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: str,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self._private_key = _resolve_private_key(private_key)
        self._cached_token: str | None = None
        self._token_expires_at: float = 0

    def _make_jwt(self) -> str:
        """Create a signed JWT for the GitHub App."""
        now = int(time.time())
        payload = {
            "iat": now - 60,  # slight backdate for clock skew
            "exp": now + _JWT_LIFETIME_SECONDS,
            "iss": self.app_id,
        }
        return pyjwt.encode(payload, self._private_key, algorithm="RS256")

    async def get_token(self) -> str:
        """Get a valid installation token, refreshing if needed."""
        if self._cached_token and time.time() < self._token_expires_at:
            return self._cached_token

        token_jwt = self._make_jwt()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GITHUB_API}/app/installations/{self.installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {token_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        self._cached_token = data["token"]
        # GitHub tokens expire in 1 hour; refresh early
        self._token_expires_at = time.time() + 3600 - _EXPIRY_MARGIN_SECONDS
        return self._cached_token

    def is_configured(self) -> bool:
        """Check if this auth instance has all required fields."""
        return bool(self.app_id and self._private_key and self.installation_id)


def create_app_auth(
    app_id: str | None,
    private_key: str | None,
    installation_id: str | None,
) -> GitHubAppAuth | None:
    """Create a GitHubAppAuth if all required fields are present, else None.

    private_key can be either:
    - Inline PEM content (starts with '-----BEGIN')
    - A file path to a .pem file
    """
    if not all([app_id, private_key, installation_id]):
        return None
    # If it's a file path, verify it exists
    if not private_key.strip().startswith(_PEM_HEADER):
        key_path = Path(private_key).expanduser()
        if not key_path.is_file():
            return None
    return GitHubAppAuth(
        app_id=app_id,
        private_key=private_key,
        installation_id=installation_id,
    )
