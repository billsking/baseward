"""Locale negotiation and message translation.

Two locales are supported: Simplified Chinese (``zh-CN``) and English (``en``).
The admin UI follows the browser's ``Accept-Language`` header; an explicit
language choice is stored in the ``gw_admin_lang`` cookie and wins over the
header. MCP clients have no browser language, so their locale comes from
``GATEWAY_MCP_LANGUAGE``.

The active locale lives in a context variable populated by ``LocaleMiddleware``.
Code outside a request (scripts, background jobs) falls back to Chinese, which
keeps the historical message text for existing integrations.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar, Token
from typing import Any

from app.i18n.errors import ERROR_MESSAGES, LABELS
from app.i18n.messages import MESSAGES

logger = logging.getLogger(__name__)

SUPPORTED_LOCALES = ("zh-CN", "en")
DEFAULT_LOCALE = "zh-CN"
LOCALE_COOKIE = "gw_admin_lang"
LOCALE_MAX_AGE = 365 * 24 * 3600

_current_locale: ContextVar[str] = ContextVar("gateway_locale", default=DEFAULT_LOCALE)

_ACCEPT_LANGUAGE_ITEM = re.compile(
    r"^\s*([A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*)\s*(?:;\s*q\s*=\s*([0-9.]+))?\s*$"
)


def normalize_locale(value: str | None) -> str | None:
    """Map an arbitrary tag (``en-US``, ``zh_CN``, ``ZH``) onto a supported locale."""
    if not value:
        return None
    tag = value.strip().replace("_", "-").lower()
    if tag == "zh" or tag.startswith("zh-"):
        return "zh-CN"
    if tag == "en" or tag.startswith("en-"):
        return "en"
    return None


def parse_accept_language(header: str | None) -> str | None:
    """Return the best supported locale from an ``Accept-Language`` header."""
    if not header:
        return None
    ranked: list[tuple[float, int, str]] = []
    for index, item in enumerate(header.split(",")):
        match = _ACCEPT_LANGUAGE_ITEM.match(item)
        if match is None:
            continue
        try:
            quality = float(match.group(2)) if match.group(2) else 1.0
        except ValueError:
            quality = 1.0
        locale = normalize_locale(match.group(1))
        if locale is not None:
            ranked.append((quality, -index, locale))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][2]


def resolve_locale(
    cookie_value: str | None,
    accept_language: str | None,
    query_value: str | None = None,
) -> str:
    """Pick the locale: explicit ``?lang=`` > remembered cookie > browser header > default."""
    return (
        normalize_locale(query_value)
        or normalize_locale(cookie_value)
        or parse_accept_language(accept_language)
        or DEFAULT_LOCALE
    )


def get_locale() -> str:
    return _current_locale.get()


def set_locale(locale: str) -> Token:
    return _current_locale.set(normalize_locale(locale) or DEFAULT_LOCALE)


def reset_locale(token: Token) -> None:
    _current_locale.reset(token)


def translate_error(text: str) -> str:
    """Translate a catalogued service error or label; unknown text is returned unchanged."""
    if get_locale() == "zh-CN" or not text:
        return text
    return ERROR_MESSAGES.get(text, text)


def translate_label(label: str) -> str:
    if get_locale() == "zh-CN":
        return label
    return LABELS.get(label, label)


def t(key: str, **params: Any) -> str:
    """Translate a UI message key and format ``{name}`` placeholders."""
    entry = MESSAGES.get(key)
    if entry is None:
        logger.warning("missing i18n key: %s", key)
        return key
    text = entry.get(get_locale()) or entry.get(DEFAULT_LOCALE) or key
    if not params:
        return text
    try:
        return text.format(**params)
    except (KeyError, IndexError):
        logger.warning("bad i18n placeholders for key: %s", key)
        return text


def js_messages() -> dict[str, str]:
    """Messages needed by the admin's vanilla JS, keyed without the ``js.`` prefix."""
    return {key[3:]: t(key) for key in MESSAGES if key.startswith("js.")}
