"""Azure AD token validation for the optional Microsoft SSO mode.

Fetches and caches the configured tenant's JWKS, then validates incoming ID
tokens: RSA signature, issuer, audience, expiry, and not-before time. The
validated preferred_username or email claim identifies the caller.

Bearer headers are caller-controlled. A token must pass every check even when
clients normally obtain it through the OAuth proxy in server.py. The proxy
returns Azure's ID token, whose audience is this application's client ID and
whose issuer is the configured tenant's v2.0 issuer.

Only asymmetric RSA algorithms are accepted. Required issuer, audience, and
expiry claims cannot be omitted. Any validation failure raises JWTError;
there is no fallback to unverified claims. These boundaries are exercised by
tests/test_oauth_token_validation.py.

Some Microsoft US Government JWKS entries omit alg. An RSA JWK with no alg is
read as RS256; the key still comes from the configured tenant's HTTPS endpoint.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from jose import JWTError, jwk, jwt

from epicor_mcp.config import Settings

logger = logging.getLogger(__name__)

# Cache JWKS keys for 1 hour (Azure AD rotates keys roughly every 24h)
_JWKS_CACHE_TTL_SECONDS = 3600

#: Signature algorithms this server accepts. **Asymmetric only.** An ``HS*``
#: token verified against a public JWKS key is the classic algorithm-confusion
#: forgery. ``none`` is likewise absent.
ALLOWED_ALGORITHMS: tuple[str, ...] = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")

#: Clock skew tolerated on ``exp`` / ``nbf``, seconds.
_CLOCK_SKEW_LEEWAY_SECONDS = 60


@dataclass
class _JWKSCache:
    """In-memory cache for Azure AD JWKS keys."""

    keys: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.fetched_at) > _JWKS_CACHE_TTL_SECONDS

    @property
    def is_empty(self) -> bool:
        return len(self.keys) == 0


@dataclass
class ValidatedToken:
    """Result of a successful token validation.

    Attributes:
        user_id: The user's email or UPN from the token claims.
        claims: The full set of validated JWT claims.
    """

    user_id: str
    claims: dict[str, Any]


class AzureADTokenValidator:
    """Validates Azure AD OAuth 2.1 tokens (ID tokens / access tokens).

    Uses the Azure AD JWKS endpoint to verify RS256 signatures and checks
    issuer, audience, and expiry claims.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache = _JWKSCache()
        self._http_client: httpx.AsyncClient | None = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create the shared async HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def close(self) -> None:
        """Close the HTTP client. Call on shutdown."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def _fetch_jwks(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Fetch JWKS keys from Azure AD, using cache when valid.

        Args:
            force: Ignore the cache and re-fetch. Used exactly once per
                validation, when the token's ``kid`` matches no cached key —
                that is what a key rotation looks like, and without the refresh
                a valid token would be rejected for up to the cache TTL. It can
                only ever ADD keys Azure itself served; it never relaxes a check.

        Returns:
            List of JWK key dicts from Azure AD.

        Raises:
            RuntimeError: If the JWKS endpoint cannot be reached.
        """
        if not force and not self._cache.is_empty and not self._cache.is_expired:
            return self._cache.keys

        logger.info("Fetching JWKS keys from Azure AD: %s", self._settings.azure_jwks_url)
        client = await self._get_http_client()
        try:
            response = await client.get(self._settings.azure_jwks_url)
            response.raise_for_status()
            data = response.json()
            keys = data.get("keys", [])
            if not keys:
                raise RuntimeError("Azure AD JWKS response contained no keys")

            self._cache.keys = keys
            self._cache.fetched_at = time.time()
            logger.info("Cached %d JWKS keys from Azure AD", len(keys))
            return keys

        except httpx.HTTPError as exc:
            # If we have stale-but-valid cached keys, use them as fallback
            if not self._cache.is_empty:
                logger.warning(
                    "Failed to refresh JWKS keys (%s), using cached keys", exc
                )
                return self._cache.keys
            raise RuntimeError(
                f"Failed to fetch JWKS keys from Azure AD: {exc}"
            ) from exc

    def _find_signing_key(
        self, token_header: dict[str, Any], jwks_keys: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Find the JWK that matches the token's kid (Key ID) header.

        Args:
            token_header: Decoded JWT header containing 'kid'.
            jwks_keys: List of JWK dicts from Azure AD.

        Returns:
            The matching JWK dict.

        Raises:
            JWTError: If no matching key is found.
        """
        kid = token_header.get("kid")
        if not kid:
            raise JWTError("Token header missing 'kid' claim")

        for key in jwks_keys:
            if key.get("kid") == kid:
                return key

        raise JWTError(
            f"Unable to find matching JWKS key for kid={kid}. "
            f"Available kids: {[k.get('kid') for k in jwks_keys]}"
        )

    def _signing_key_for(self, header: dict[str, Any], keys: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the JWK for *header*'s ``kid``, RSA-only, with GovCloud's missing ``alg`` filled."""
        key = dict(self._find_signing_key(header, keys))
        if key.get("kty") != "RSA":
            # An `oct` (symmetric) JWK is a shared secret. Constructing one here
            # and handing it to jwt.decode is how algorithm confusion becomes a
            # forged token, so it is refused even though Azure never serves one.
            raise JWTError(
                f"JWKS key kid={header.get('kid')!r} has kty={key.get('kty')!r}; only RSA "
                "signing keys are accepted"
            )
        if not key.get("alg"):
            # GovCloud omits `alg` on some keys. This is metadata about a key
            # Azure served over TLS, NOT a claim from the token, so defaulting it
            # cannot be influenced by a caller.
            key["alg"] = "RS256"
        if key["alg"] not in ALLOWED_ALGORITHMS:
            raise JWTError(f"JWKS key advertises alg={key['alg']!r}, which is not accepted")
        return key

    async def validate_token(self, token: str) -> ValidatedToken:
        """Validate an Azure AD JWT and return its identity, or raise.

        **Every** check is mandatory: RSA signature against the tenant JWKS, an
        algorithm from :data:`ALLOWED_ALGORITHMS`, ``aud`` == this app's client
        id, ``iss`` == the configured v2.0 tenant issuer, and a present, unexpired
        ``exp``. There is deliberately **no** unverified-claims path — see the
        module docstring for the forged token that walked through the one this
        replaced.

        Args:
            token: The raw JWT string (Bearer token value).

        Returns:
            A ValidatedToken with the user's identity and full claims.

        Raises:
            JWTError: If the token is invalid, expired, forged, or cannot be
                decoded — and if the JWKS cannot be reached, because a signature
                check that cannot run must fail closed.
        """
        if not self._settings.azure_tenant_id or not self._settings.azure_client_id:
            raise JWTError(
                "OAuth is not configured on this server (azure_tenant_id / azure_client_id "
                "are unset), so no bearer token can be validated. Refusing rather than "
                "accepting an unverifiable token."
            )

        try:
            unverified_header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise JWTError(f"Cannot read token header: {exc}") from exc

        alg = str(unverified_header.get("alg") or "")
        if alg not in ALLOWED_ALGORITHMS:
            # Checked BEFORE any key is fetched so the rejection is unambiguous:
            # `none` and `HS*` never reach a verifier at all.
            raise JWTError(
                f"Token alg={alg!r} is not accepted. This server verifies asymmetric "
                f"signatures only ({', '.join(ALLOWED_ALGORITHMS)})."
            )

        jwks_keys = await self._fetch_jwks()
        try:
            signing_key_data = self._signing_key_for(unverified_header, jwks_keys)
        except JWTError:
            # A `kid` miss is what a key rotation looks like. Re-fetch ONCE and
            # retry; if it still misses, the token is not ours and it fails.
            jwks_keys = await self._fetch_jwks(force=True)
            signing_key_data = self._signing_key_for(unverified_header, jwks_keys)

        rsa_key = jwk.construct(signing_key_data)
        claims = jwt.decode(
            token,
            rsa_key.to_dict(),
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=self._settings.azure_client_id,
            issuer=self._settings.azure_issuer,
            options={
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                # `require_*`: a claim that is absent must FAIL, not pass
                # vacuously. A forged token can omit both aud and iss.
                "require_aud": True,
                "require_iss": True,
                "require_exp": True,
                # at_hash binds an id_token to a sibling access token we do not
                # forward; we never issue one to compare against, so requiring it
                # would reject legitimate Azure tokens without adding a control.
                "verify_at_hash": False,
                "leeway": _CLOCK_SKEW_LEEWAY_SECONDS,
            },
        )

        # Extract user identity
        user_id = (
            claims.get("preferred_username")
            or claims.get("email")
            or claims.get("upn")
        )
        if not user_id:
            raise JWTError(
                "Token does not contain preferred_username, email, or upn claim. "
                f"Available claims: {list(claims.keys())}"
            )

        logger.info("Successfully validated token for user: %s", user_id)
        return ValidatedToken(user_id=user_id.lower(), claims=claims)
