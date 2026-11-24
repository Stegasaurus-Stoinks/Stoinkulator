"""The output scrubber masks an account-shaped id only when it stands on its own (issue 167).

Every id below is synthetic (a made-up shape), never a real account.
"""

from __future__ import annotations

import pytest

from MainStoinker.ib.cli import scrub

OBSERVED_PATH = (
    r"Output folder: C:\Users\someone\AppData\Local\Temp\claude\969200cbb8523486"
    r"\a9fd99a6-3ea1-4742-a806-bc51324d3841\scratchpad\paperbars"
)

MUST_MASK = [
    ("DU1234567", "ACCOUNT"),
    ("DUX123456", "ACCOUNT"),
    ("U7654321", "ACCOUNT"),
    ("AB12345", "ACCOUNT"),
    ("a12345", "ACCOUNT"),
    ("account DU1234567 invalid", "account ACCOUNT invalid"),
    ("account DUX123456 invalid", "account ACCOUNT invalid"),
    ("DU1234567 is bad", "ACCOUNT is bad"),
    ("bad DU1234567", "bad ACCOUNT"),
    ("(DU1234567)", "(ACCOUNT)"),
    ("[DUX123456]", "[ACCOUNT]"),
    ("'DU1234567'", "'ACCOUNT'"),
    ('"DUX123456"', '"ACCOUNT"'),
    ("acct=DU1234567", "acct=ACCOUNT"),
    ("acct: DU1234567", "acct: ACCOUNT"),
    ("ids DU1234567,DUX123456,U7654321", "ids ACCOUNT,ACCOUNT,ACCOUNT"),
    ("DU1234567.", "ACCOUNT."),
    ("DU1234567;", "ACCOUNT;"),
    ("acct_DU1234567", "acct_ACCOUNT"),
    ("acct_DUX123456", "acct_ACCOUNT"),
    ("DU1234567-x", "ACCOUNT-x"),
    ("x-DUX123456-y", "x-ACCOUNT-y"),
    ("DU1234567_x", "ACCOUNT_x"),
    (r"C:\data\DUX123456\bars", r"C:\data\ACCOUNT\bars"),
    ("/data/DU1234567/bars", "/data/ACCOUNT/bars"),
    (r"C:\data\DU1234567", r"C:\data\ACCOUNT"),
    ("DU1234567 and U7654321", "ACCOUNT and ACCOUNT"),
    ("also U7654321", "also ACCOUNT"),
]

MUST_NOT_MASK = [
    "969200cbb8523486",
    "a9fd99a6-3ea1-4742-a806-bc51324d3841",
    "bc51324d3841",
    "x969200cbb8523486",
    r"C:\Temp\claude\969200cbb8523486\x",
    "deadbeef0123456789",
    "abcd123456",
    "ABCDE1234567",
    "XDUX1234567",
    "DU1234567x",
    "DUX123456abc",
    "9DU1234567",
    "DU1234567890abcdef",
    "version2024",
    "ab1234",
    "DU1234",
    "12345678",
    "hello world",
    "",
    "2024-08-13",
]


@pytest.mark.parametrize(("text", "expected"), MUST_MASK)
def test_a_standalone_id_is_masked_whole(text: str, expected: str) -> None:
    assert scrub(text) == expected


@pytest.mark.parametrize("text", MUST_NOT_MASK)
def test_text_that_is_not_a_standalone_id_is_left_alone(text: str) -> None:
    assert scrub(text) == text


def test_the_table_sizes_meet_the_issue() -> None:
    assert len(MUST_MASK) >= 20
    assert len(MUST_NOT_MASK) >= 15


def test_the_observed_temp_path_prints_unmasked() -> None:
    assert scrub(OBSERVED_PATH) == OBSERVED_PATH


def test_a_three_letter_prefix_is_masked_whole_with_no_leftover_letter() -> None:
    out = scrub("paper DUX123456 ready")
    assert out == "paper ACCOUNT ready"
    assert "D" not in out.replace("ACCOUNT", "")
    assert scrub("DUX123456") == "ACCOUNT"
