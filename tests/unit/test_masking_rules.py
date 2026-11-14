"""Masking rules and masking functions unit tests."""

from datetime import date

import pytest

from app.errors import ValidationError
from app.masking.rules import (
    STRATEGY_MASK,
    STRATEGY_NULL,
    Rule,
    detect_auto,
    mask_value,
    strictest,
    validate_rule,
)
from app.security.crypto import mask_hmac

KEY = b"k" * 32


@pytest.mark.parametrize(
    ("column", "strategy", "pattern"),
    [
        ("phone", STRATEGY_MASK, "phone"),
        ("user_mobile", STRATEGY_MASK, "phone"),
        ("contact_tel", STRATEGY_MASK, "phone"),
        ("email", STRATEGY_MASK, "email"),
        ("user_mail", STRATEGY_MASK, "email"),
        ("password", STRATEGY_NULL, None),
        ("passwd", STRATEGY_NULL, None),
        ("pwd", STRATEGY_NULL, None),
        ("client_secret", STRATEGY_NULL, None),
        ("id_card", STRATEGY_MASK, "id_card"),
        ("identity_no", STRATEGY_MASK, "id_card"),
        ("cert_no", STRATEGY_MASK, "id_card"),
        ("bank_card", STRATEGY_MASK, "bank_card"),
        ("card_no", STRATEGY_MASK, "bank_card"),
        ("real_name", STRATEGY_MASK, "name"),
        ("full_name", STRATEGY_MASK, "name"),
        ("address", STRATEGY_MASK, "generic"),
        ("addr", STRATEGY_MASK, "generic"),
    ],
)
def test_detect_auto(column: str, strategy: str, pattern: str | None) -> None:
    rule = detect_auto(column)
    assert rule is not None
    assert rule.strategy == strategy
    assert rule.pattern == pattern


def test_detect_auto_no_match() -> None:
    assert detect_auto("id") is None
    assert detect_auto("created_at") is None
    assert detect_auto("username") is None


def test_detect_auto_priority_password_first() -> None:
    rule = detect_auto("user_password_phone")
    assert rule is not None
    assert rule.strategy == STRATEGY_NULL


def test_validate_rule_combinations() -> None:
    assert validate_rule("mask", "phone") == Rule("mask", "phone")
    assert validate_rule("hash", None) == Rule("hash", None)
    assert validate_rule("off", "") == Rule("off", None)
    with pytest.raises(ValidationError):
        validate_rule("mask", "unknown")
    with pytest.raises(ValidationError):
        validate_rule("mask", None)
    with pytest.raises(ValidationError):
        validate_rule("hash", "phone")
    with pytest.raises(ValidationError):
        validate_rule("unknown", None)


def test_strictness_order() -> None:
    rules = [Rule("off"), Rule("mask", "phone"), Rule("hash"), Rule("null")]
    assert strictest(rules) == Rule("null")
    assert strictest([]) is None


@pytest.mark.parametrize(
    ("pattern", "value", "expected"),
    [
        ("phone", "13812341234", "138****1234"),
        ("phone", "123456", "1****6"),
        ("email", "alice@example.com", "a***@example.com"),
        ("email", "a@example.com", "a@e*******com"),
        ("email", "abc", "a*c"),
        ("id_card", "110101199003071234", "110101********1234"),
        ("name", "张三", "张*"),
        ("name", "单", "*"),
        ("bank_card", "6222021234567890", "************7890"),
        ("bank_card", "123", "1*3"),
        ("generic", "abcdefgh", "ab****gh"),
        ("generic", "ab", "**"),
    ],
)
def test_mask_patterns(pattern: str, value: str, expected: str) -> None:
    assert mask_value(value, Rule("mask", pattern), KEY) == expected


def test_hash_is_stable() -> None:
    rule = Rule("hash")
    first = mask_value("13812341234", rule, KEY)
    second = mask_value("13812341234", rule, KEY)
    assert first == second
    assert first == mask_hmac("13812341234", KEY)
    assert len(first) == 64


def test_null_off_and_none() -> None:
    assert mask_value("secret", Rule("null"), KEY) is None
    assert mask_value("secret", Rule("off"), KEY) == "secret"
    assert mask_value(None, Rule("mask", "phone"), KEY) is None


def test_non_string_values_are_converted() -> None:
    assert mask_value(123, Rule("hash"), KEY) == mask_hmac("123", KEY)
    assert mask_value(date(2026, 1, 2), Rule("hash"), KEY) == mask_hmac("2026-01-02", KEY)
    assert mask_value(b"abc", Rule("hash"), KEY) == mask_hmac("b'abc'", KEY)
