"""JWT validation regressions with generated signing keys and mocked JWKS.

Reject attacker-selected symmetric algorithms, wrong signatures, invalid
claims, and unavailable key sources. Valid asymmetric tokens must continue
to work. All validation runs offline."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import JWTError, jwt
from jose.utils import base64url_encode

from epicor_mcp.auth.oauth import ALLOWED_ALGORITHMS, AzureADTokenValidator

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-000000000000"
ISSUER = f"https://login.microsoftonline.us/{TENANT}/v2.0"


@dataclass
class _Settings:
    """Only what the validator reads. A stub keeps `.env` out of the test."""

    azure_tenant_id: str = TENANT
    azure_client_id: str = CLIENT_ID

    @property
    def azure_issuer(self) -> str:
        return f"https://login.microsoftonline.us/{self.azure_tenant_id}/v2.0"

    @property
    def azure_jwks_url(self) -> str:
        return f"https://login.microsoftonline.us/{self.azure_tenant_id}/discovery/v2.0/keys"


def _keypair(kid: str) -> tuple[str, dict]:
    """(private PEM, public JWK) — a real RSA key, generated per call."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    numbers = key.public_key().public_numbers()

    def b64(i: int) -> str:
        return base64url_encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).decode()

    return pem, {"kty": "RSA", "use": "sig", "kid": kid, "n": b64(numbers.n), "e": b64(numbers.e)}


PEM, JWK_PUB = _keypair("test-signing-key-1")


def _claims(**over) -> dict:
    base = {
        "aud": CLIENT_ID,
        "iss": ISSUER,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()) - 10,
        "nbf": int(time.time()) - 10,
        "preferred_username": "adminuser@example.org",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def _validator(keys: list[dict] | None = None, *, settings: _Settings | None = None):
    """A validator whose JWKS endpoint is HEALTHY and returns *keys*."""
    v = AzureADTokenValidator(settings or _Settings())  # type: ignore[arg-type]
    served = [dict(k) for k in (keys if keys is not None else [JWK_PUB])]
    calls: list[bool] = []

    async def _fake_fetch(*, force: bool = False):
        calls.append(force)
        return served

    v._fetch_jwks = _fake_fetch  # type: ignore[assignment]
    v.jwks_calls = calls  # type: ignore[attr-defined]
    v.served_keys = served  # type: ignore[attr-defined]
    return v


async def _validate(v, token: str):
    return await v.validate_token(token)


# --------------------------------------------------------------------------- #
# The legitimate token still works — a security fix that breaks login is a bug
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_genuine_azure_id_token_validates():
    token = jwt.encode(_claims(), PEM, algorithm="RS256", headers={"kid": JWK_PUB["kid"]})
    result = await _validate(_validator(), token)
    assert result.user_id == "adminuser@example.org"
    assert result.claims["aud"] == CLIENT_ID


@pytest.mark.asyncio
async def test_a_govcloud_jwk_with_no_alg_still_validates():
    """GovCloud omits `alg` on some JWKS entries — that accommodation survives."""
    key_without_alg = {k: v for k, v in JWK_PUB.items() if k != "alg"}
    token = jwt.encode(_claims(), PEM, algorithm="RS256", headers={"kid": JWK_PUB["kid"]})
    assert (await _validate(_validator([key_without_alg]), token)).user_id


@pytest.mark.asyncio
async def test_email_and_upn_are_still_accepted_as_the_identity():
    for claim in ("email", "upn"):
        token = jwt.encode(
            _claims(preferred_username=None, **{claim: "EmployeeUser@example.org"}),
            PEM,
            algorithm="RS256",
            headers={"kid": JWK_PUB["kid"]},
        )
        assert (await _validate(_validator(), token)).user_id == "employeeuser@example.org"


# --------------------------------------------------------------------------- #
# Forged symmetric tokens must be rejected with a healthy asymmetric JWKS.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_forged_hs256_token_is_rejected_with_a_healthy_jwks():
    """THE finding. Attacker-chosen key, invented kid, JWKS healthy."""
    forged = jwt.encode(
        {"preferred_username": "mallory@attacker.invalid", "exp": int(time.time()) + 3600},
        "attacker-picked-key",
        algorithm="HS256",
        headers={"kid": "attacker-invented-kid"},
    )
    v = _validator()
    with pytest.raises(JWTError) as exc:
        await _validate(v, forged)
    assert "alg" in str(exc.value)
    # ...and it was rejected on the ALGORITHM, before a key was ever fetched.
    assert v.jwks_calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_hs256_token_signed_with_the_public_modulus_is_rejected():
    """Algorithm confusion: the public key is public, so HS* must never verify."""
    forged = jwt.encode(_claims(), JWK_PUB["n"], algorithm="HS256",
                        headers={"kid": JWK_PUB["kid"]})
    with pytest.raises(JWTError):
        await _validate(_validator(), forged)


@pytest.mark.asyncio
async def test_a_token_signed_by_a_different_rsa_key_is_rejected():
    """Right kid, right claims, WRONG signer — the signature is now checked."""
    other_pem, _ = _keypair(JWK_PUB["kid"])
    forged = jwt.encode(_claims(), other_pem, algorithm="RS256",
                        headers={"kid": JWK_PUB["kid"]})
    with pytest.raises(JWTError):
        await _validate(_validator(), forged)


@pytest.mark.asyncio
async def test_an_unknown_kid_is_rejected_after_one_forced_refresh():
    token = jwt.encode(_claims(), PEM, algorithm="RS256", headers={"kid": "no-such-kid"})
    v = _validator()
    with pytest.raises(JWTError):
        await _validate(v, token)
    # exactly one cached read + one forced re-read, then failure. The refresh
    # exists for key ROTATION; it must not become an unbounded retry loop.
    assert v.jwks_calls == [False, True]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_rotated_key_validates_on_the_forced_refresh():
    new_pem, new_pub = _keypair("test-signing-key-2")
    v = _validator([JWK_PUB])
    token = jwt.encode(_claims(), new_pem, algorithm="RS256", headers={"kid": new_pub["kid"]})

    async def _fetch(*, force: bool = False):
        v.jwks_calls.append(force)  # type: ignore[attr-defined]
        return [JWK_PUB, new_pub] if force else [JWK_PUB]

    v._fetch_jwks = _fetch  # type: ignore[assignment]
    assert (await _validate(v, token)).user_id


@pytest.mark.asyncio
async def test_a_symmetric_jwks_entry_is_never_constructed_into_a_key():
    """If anything ever served an `oct` JWK, using it would be the forgery."""
    forged = jwt.encode(_claims(), "shared-secret", algorithm="HS256",
                        headers={"kid": "oct-key"})
    v = _validator([{"kty": "oct", "kid": "oct-key", "k": "c2hhcmVkLXNlY3JldA", "alg": "HS256"}])
    with pytest.raises(JWTError):
        await _validate(v, forged)


# --------------------------------------------------------------------------- #
# The claims the forger controls are no longer the only checks
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims, why",
    [
        (_claims(exp=None), "no exp at all — accepted by the old fallback"),
        (_claims(exp=int(time.time()) - 120), "expired"),
        (_claims(aud=None), "aud missing entirely (require_aud)"),
        (_claims(aud="some-other-app"), "aud is another application"),
        (_claims(iss=None), "iss missing entirely (require_iss)"),
        (_claims(iss="https://login.microsoftonline.com/other/v2.0"), "wrong tenant/cloud"),
        (_claims(nbf=int(time.time()) + 600), "not valid yet"),
        (_claims(preferred_username=None), "no identity claim"),
    ],
)
async def test_claim_level_rejections(claims, why):
    token = jwt.encode(claims, PEM, algorithm="RS256", headers={"kid": JWK_PUB["kid"]})
    with pytest.raises(JWTError):
        await _validate(_validator(), token)


@pytest.mark.asyncio
async def test_sixty_seconds_of_clock_skew_is_tolerated_but_not_two_minutes():
    ok = jwt.encode(_claims(exp=int(time.time()) - 30), PEM, algorithm="RS256",
                    headers={"kid": JWK_PUB["kid"]})
    assert (await _validate(_validator(), ok)).user_id
    stale = jwt.encode(_claims(exp=int(time.time()) - 120), PEM, algorithm="RS256",
                       headers={"kid": JWK_PUB["kid"]})
    with pytest.raises(JWTError):
        await _validate(_validator(), stale)


@pytest.mark.asyncio
async def test_an_unconfigured_server_refuses_rather_than_accepting_anything():
    token = jwt.encode(_claims(), PEM, algorithm="RS256", headers={"kid": JWK_PUB["kid"]})
    v = _validator(settings=_Settings(azure_tenant_id="", azure_client_id=""))
    with pytest.raises(JWTError) as exc:
        await _validate(v, token)
    assert "not configured" in str(exc.value)


@pytest.mark.asyncio
async def test_an_unreachable_jwks_endpoint_fails_closed():
    """A signature check that cannot RUN must not degrade into no check."""
    v = AzureADTokenValidator(_Settings())  # type: ignore[arg-type]

    async def _boom(*, force: bool = False):
        raise RuntimeError("Failed to fetch JWKS keys from Azure AD: connect timeout")

    v._fetch_jwks = _boom  # type: ignore[assignment]
    token = jwt.encode(_claims(), PEM, algorithm="RS256", headers={"kid": JWK_PUB["kid"]})
    with pytest.raises((JWTError, RuntimeError)):
        await v.validate_token(token)


@pytest.mark.asyncio
async def test_garbage_is_rejected():
    for junk in ("", "not-a-jwt", "a.b.c"):
        with pytest.raises(JWTError):
            await _validate(_validator(), junk)


# --------------------------------------------------------------------------- #
# The fallback must not come back
# --------------------------------------------------------------------------- #


def test_the_module_never_decodes_without_verification():
    """A source-level fence: the fix is one deleted branch and easy to re-add.

    Docstrings are stripped first so the module's own explanation of the defect
    does not defeat its own test.
    """
    import ast

    path = Path(__file__).resolve().parent.parent / "src" / "epicor_mcp" / "auth" / "oauth.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    body[0].value.value = ""
    source = ast.unparse(tree)
    assert "get_unverified_claims" not in source, (
        "the unverified-claims fallback is back; a forged HS256 token could "
        "bypass signature verification"
    )
    # `get_unverified_header` IS legitimate (reading `kid`/`alg` to pick a key),
    # so it is not banned — but no symmetric algorithm may appear as a verifier.
    assert "HS256" not in source and "HS512" not in source


def test_only_asymmetric_algorithms_are_allowed():
    assert all(a.startswith(("RS", "PS", "ES")) for a in ALLOWED_ALGORITHMS)
    for banned in ("none", "None", "HS256", "HS384", "HS512"):
        assert banned not in ALLOWED_ALGORITHMS
