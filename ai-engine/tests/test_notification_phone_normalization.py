"""Engagement / opt-out gates must match a citizen across 08… / 62… phone
formats — the bug that silently suppressed every real citizen's status push.

Root cause (see services/notifications._canon): inbound `record_engagement`
stored APTANA's `62…` E.164, while the transparency poller checked with SIAP
`person_profile.mobile_phone` in `08…` local form, and the gate was exact-string
set membership. So `is_engaged("085…")` was False for a citizen recorded as
`625…` — suppressed as "no engagement".
"""
import pytest

from services import notifications as n


@pytest.fixture(autouse=True)
def _isolated_sets(monkeypatch):
    """Fresh in-memory sets per test, and keep the test off disk."""
    monkeypatch.setattr(n, "_save_set", lambda *a, **k: None)
    monkeypatch.setattr(n, "_engaged_phones", set())
    monkeypatch.setattr(n, "_opted_out_phones", set())


# Two formats of ONE citizen (fake documentation number): the loop stores one
# way (APTANA 62… E.164) and checks the other (SIAP mobile_phone 08… local).
_E164 = "6281234567890"   # how APTANA inbound / record_engagement stores it
_LOCAL = "081234567890"   # how SIAP mobile_phone / the poller checks it


def test_engaged_62_matches_check_08():
    n.record_engagement(_E164)
    assert n.is_engaged(_LOCAL) is True
    assert n.is_engaged(_E164) is True


def test_engaged_08_matches_check_62():
    n.record_engagement(_LOCAL)
    assert n.is_engaged(_E164) is True


@pytest.mark.parametrize(
    "variant",
    ["+6281234567890", "+62 812-3456-7890", "6281234567890", "081234567890", "81234567890"],
)
def test_all_common_formats_of_one_citizen_match(variant):
    n.record_engagement(_E164)
    assert n.is_engaged(variant) is True


def test_optout_matches_across_formats():
    # A STOP from the 08… inbound must suppress the 62…-keyed poller send.
    n.record_opt_out(_LOCAL)
    assert n.is_opted_out(_E164) is True
    assert n.is_opted_out(_LOCAL) is True


def test_different_citizens_do_not_collide():
    n.record_engagement("6281111111111")
    assert n.is_engaged("082222222222") is False


def test_telegram_key_not_coerced_to_phone():
    # tg:{chat_id} is NOT a phone — must be stored/checked verbatim, and must
    # never collide with the phone "62…" that normalize_phone would produce.
    n.record_engagement("tg:12345")
    assert n.is_engaged("tg:12345") is True
    assert n.is_engaged("6212345") is False
    assert n.is_engaged("012345") is False


def test_empty_input_is_safe():
    assert n.is_engaged("") is False
    n.record_engagement("")  # must not raise
