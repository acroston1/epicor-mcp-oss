"""Legacy query registration checks and public configuration safeguards.

Legacy development mode is gated by environment and an explicit override.
The open-source Settings reject all development identity bypass options;
SSO-disabled operation uses the supported authentication and table policy."""

from __future__ import annotations

import pytest
from dataclasses import dataclass

from epicor_mcp.config import Settings
from epicor_mcp.sql.tool import register_query_tool, registration_decision


@dataclass
class FakeSettings:
    dev_mode: bool
    environment: str


class RecordingMCP:
    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self, **kwargs):
        def decorator(fn):
            self.registered.append(kwargs.get("name") or fn.__name__)
            return fn

        return decorator


def test_dev_mode_plus_live_refuses_registration():
    decision = registration_decision(FakeSettings(dev_mode=True, environment="live"))
    assert decision.allowed is False
    assert bool(decision) is False
    assert "MUST NOT REGISTER" in decision.reason
    assert "DEV_MODE=false" in decision.remedy


def test_the_refusal_actually_registers_nothing():
    """The real registration path must enforce its reported decision."""
    mcp = RecordingMCP()
    decision = register_query_tool(
        mcp, FakeSettings(dev_mode=True, environment="live"), runner=None
    )
    assert decision.allowed is False
    assert mcp.registered == []


def test_dev_mode_off_allows_registration():
    mcp = RecordingMCP()
    decision = register_query_tool(
        mcp, FakeSettings(dev_mode=False, environment="live"), runner=None
    )
    assert decision.allowed is True
    assert mcp.registered == ["epicor_query"]


def test_dev_mode_against_pilot_is_the_plans_own_carve_out():
    """The registration rule is `dev_mode AND environment == "live"`. Dev mode against
    PILOT is allowed, and that is a deliberate narrowing, not an oversight."""
    assert registration_decision(FakeSettings(dev_mode=True, environment="pilot")).allowed


def test_the_gate_fails_closed_on_a_settings_object_it_cannot_read():
    class Opaque:
        pass

    assert registration_decision(Opaque()).allowed is False


def test_dev_mode_plus_live_still_refuses_without_the_authorisation():
    """The dev-mode override is OPT-IN. Absent that explicit authorisation, the
    dev-mode refusal is unchanged — this is the assertion that keeps the
    override from silently becoming the default."""

    @dataclass
    class Unauthorised:
        dev_mode: bool = True
        environment: str = "live"
        allow_dev_mode_query_tool: bool = False

    decision = registration_decision(Unauthorised())
    assert decision.allowed is False
    assert "MUST NOT REGISTER" in decision.reason


def test_the_owner_authorisation_registers_and_says_why():
    """An explicit legacy override must record why registration is allowed."""

    @dataclass
    class Authorised:
        dev_mode: bool = True
        environment: str = "live"
        allow_dev_mode_query_tool: bool = True

    mcp = RecordingMCP()
    decision = register_query_tool(mcp, Authorised(), runner=None)
    assert decision.allowed is True
    assert decision.reason == "owner_authorised_dev_mode"
    assert mcp.registered == ["epicor_query"]


def test_the_current_env_is_one_of_the_two_legitimate_postures():
    """Configured legacy registration must agree with its environment and override."""
    settings = Settings()
    decision = registration_decision(settings)
    if settings.dev_mode and settings.environment == "live":
        if getattr(settings, "allow_dev_mode_query_tool", False):
            assert decision.allowed is True
            assert decision.reason == "owner_authorised_dev_mode"
        else:
            assert decision.allowed is False, "the registration gate stopped firing on the live .env"
    else:
        assert decision.allowed is True


@pytest.mark.parametrize("override", [{"dev_mode": True}, {"allow_dev_mode_query_tool": True}, {"dev_identity": "other@example.org"}])
def test_public_configuration_rejects_development_identity_bypasses(override):
    with pytest.raises(ValueError, match="unsupported"):
        Settings(_env_file=None, **override)
