"""Regression coverage: test sec code eval."""

from __future__ import annotations

import pytest

from epicor_mcp.rbac.menu_authz import MenuAuthorizer

from fixtures.authz.fakes import srow


def _eval(row, user_id, groups=()):
    return MenuAuthorizer.evaluate_sec_code(row, user_id, tuple(groups))


# --- unlocked ------------------------------------------------------------- #

def test_no_security_row_is_unlocked_allow():
    assert _eval(None, "anyone", groups=("APP",)) is True


def test_configured_but_empty_row_denies_positive_allowlist():
    # AllowAll false + empty EntryList => nobody is on the allow-list => DENY.
    row = srow("EMPTY", entry_list="", no_entry_list="")
    assert _eval(row, "jsmith", groups=("APP",)) is False


# --- super-flags ---------------------------------------------------------- #

def test_allow_all_grants_everyone():
    row = srow("OPEN", allow_all=True)
    assert _eval(row, "nobody", groups=()) is True


def test_disallow_all_denies_everyone():
    row = srow("SHUT", disallow_all=True, entry_list="jsmith")
    # Even a principal on the EntryList is denied — disallow wins.
    assert _eval(row, "jsmith", groups=("APP",)) is False




def test_entrylist_matches_user_id():
    row = srow("APSEC", entry_list="jsmith,APP")
    assert _eval(row, "jsmith", groups=()) is True


def test_entrylist_matches_group_code():
    row = srow("APSEC", entry_list="jsmith,APP")
    assert _eval(row, "someoneelse", groups=("APP",)) is True


def test_entrylist_no_match_denies():
    row = srow("APSEC", entry_list="jsmith,APP")
    assert _eval(row, "outsider", groups=("ENG",)) is False


# --- disallow precedence -------------------------------------------------- #

def test_noentry_overrides_entry_match():
    # In EntryList via group APP, but personally on the NoEntryList => DENY.
    row = srow("MIXED", entry_list="APP", no_entry_list="jsmith")
    assert _eval(row, "jsmith", groups=("APP",)) is False


def test_noentry_overrides_allow_all():
    row = srow("MOSTLY_OPEN", allow_all=True, no_entry_list="baddie")
    assert _eval(row, "baddie", groups=()) is False
    assert _eval(row, "gooduser", groups=()) is True


# --- wildcard handling ---------------------------------------------------- #

def test_star_entry_is_ignored_not_a_wildcard_grant():
    # '*' must be stripped from the evaluation set: with AllowAll false and only
    # '*' on the list, nobody is granted.
    row = srow("STAR", entry_list="*")
    assert _eval(row, "jsmith", groups=("APP",)) is False


def test_star_alongside_real_principal_still_matches_the_real_one():
    row = srow("STAR2", entry_list="*,APP")
    assert _eval(row, "jsmith", groups=("APP",)) is True


# --- delimiter discipline ------------------------------------------------- #

@pytest.mark.parametrize("field", ["a, APP , b", "a,APP,b"])
def test_entrylist_is_comma_delimited_and_trimmed(field):
    row = srow("SEC", entry_list=field)
    assert _eval(row, "nobody", groups=("APP",)) is True
