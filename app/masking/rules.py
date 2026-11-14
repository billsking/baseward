"""Masking rule definitions, auto-detection, and masking functions (pure logic)."""

from __future__ import annotations

from dataclasses import dataclass

from app.errors import ValidationError
from app.security.crypto import mask_hmac

STRATEGY_MASK = "mask"
STRATEGY_HASH = "hash"
STRATEGY_NULL = "null"
STRATEGY_OFF = "off"

PATTERNS = ("phone", "email", "id_card", "name", "bank_card", "generic")

STRICTNESS = {
    STRATEGY_NULL: 3,
    STRATEGY_HASH: 2,
    STRATEGY_MASK: 1,
    STRATEGY_OFF: 0,
}


@dataclass(frozen=True)
class Rule:
    strategy: str
    pattern: str | None = None

    @property
    def strictness(self) -> int:
        return STRICTNESS[self.strategy]


@dataclass(frozen=True)
class ManualRule:
    id: int | None
    datasource_id: int
    database: str
    table: str
    column: str
    strategy: str
    pattern: str | None = None

    def as_rule(self) -> Rule:
        return Rule(self.strategy, self.pattern)


# Order is priority; the first match wins.
AUTO_RULES: tuple[tuple[tuple[str, ...], Rule], ...] = (
    (("password", "passwd", "pwd", "secret"), Rule(STRATEGY_NULL)),
    (("id_card", "idcard", "identity_no", "id_no", "cert_no"), Rule(STRATEGY_MASK, "id_card")),
    (("bank_card", "card_no", "bank_account"), Rule(STRATEGY_MASK, "bank_card")),
    (("phone", "mobile", "tel"), Rule(STRATEGY_MASK, "phone")),
    (("email", "mail"), Rule(STRATEGY_MASK, "email")),
    (("real_name", "full_name"), Rule(STRATEGY_MASK, "name")),
    (("address", "addr"), Rule(STRATEGY_MASK, "generic")),
)


def detect_auto(column: str) -> Rule | None:
    """Detect the automatic masking rule from column-name keywords."""
    lowered = column.lower()
    for keywords, rule in AUTO_RULES:
        if any(keyword in lowered for keyword in keywords):
            return rule
    return None


def validate_rule(strategy: str, pattern: str | None) -> Rule:
    """Validate a manual rule combination; raise ValidationError when invalid."""
    if strategy not in STRICTNESS:
        raise ValidationError("脱敏策略必须是 mask/hash/null/off 之一")
    if strategy == STRATEGY_MASK:
        if pattern not in PATTERNS:
            raise ValidationError("脱敏模式必须是 phone/email/id_card/name/bank_card/generic 之一")
        return Rule(strategy, pattern)
    if pattern:
        raise ValidationError("hash/null/off 策略不允许指定脱敏模式")
    return Rule(strategy, None)


def strictest(rules: list[Rule]) -> Rule | None:
    """Return the strictest rule; None for an empty list."""
    if not rules:
        return None
    return max(rules, key=lambda rule: rule.strictness)


def _mask_generic(value: str) -> str:
    length = len(value)
    if length <= 2:
        return "*" * length
    keep = max(1, length // 4)
    return value[:keep] + "*" * (length - keep * 2) + value[-keep:]


def _mask_phone(value: str) -> str:
    if len(value) < 7:
        return _mask_generic(value)
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def _mask_email(value: str) -> str:
    if "@" not in value:
        return _mask_generic(value)
    local, domain = value.rsplit("@", 1)
    if len(local) <= 1 or not domain:
        return _mask_generic(value)
    return local[0] + "***@" + domain


def _mask_id_card(value: str) -> str:
    if len(value) < 10:
        return _mask_generic(value)
    return value[:6] + "*" * (len(value) - 10) + value[-4:]


def _mask_name(value: str) -> str:
    if len(value) <= 1:
        return "*"
    return value[0] + "*" * (len(value) - 1)


def _mask_bank_card(value: str) -> str:
    if len(value) < 4:
        return _mask_generic(value)
    return "*" * (len(value) - 4) + value[-4:]


_MASKERS = {
    "phone": _mask_phone,
    "email": _mask_email,
    "id_card": _mask_id_card,
    "name": _mask_name,
    "bank_card": _mask_bank_card,
    "generic": _mask_generic,
}


def mask_value(value: object, rule: Rule, mask_key: bytes):
    """Mask a single value with the given rule; None passes through."""
    if value is None:
        return None
    if rule.strategy == STRATEGY_OFF:
        return value
    text = str(value)
    if rule.strategy == STRATEGY_NULL:
        return None
    if rule.strategy == STRATEGY_HASH:
        return mask_hmac(text, mask_key)
    masker = _MASKERS.get(rule.pattern or "generic", _mask_generic)
    return masker(text)
