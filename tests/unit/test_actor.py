"""Actor header contract: parsing, validation, and base64url name decoding."""

from __future__ import annotations

import base64

import pytest
from starlette.datastructures import Headers

from app.security.actor import Actor, ActorError, parse_actor_headers


def _parse(provider=None, actor_id=None, name=None) -> Actor:
    raw = {}
    if provider is not None:
        raw["X-BaseWard-Actor-Provider"] = provider
    if actor_id is not None:
        raw["X-BaseWard-Actor-Id"] = actor_id
    if name is not None:
        raw["X-BaseWard-Actor-Name"] = name
    return parse_actor_headers(Headers(raw))


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_minimal_valid_headers() -> None:
    actor = _parse(provider="dingtalk", actor_id="manager1234")
    assert actor == Actor(provider="dingtalk", actor_id="manager1234", name=None, trust="asserted")


def test_chinese_name_roundtrip() -> None:
    actor = _parse(provider="dingtalk", actor_id="u1", name=_b64("李四"))
    assert actor.name == "李四"
    assert actor.trust == "asserted"


def test_no_headers_is_required() -> None:
    with pytest.raises(ActorError) as exc:
        _parse()
    assert exc.value.code == "ACTOR_REQUIRED"


def test_blank_headers_are_required() -> None:
    with pytest.raises(ActorError) as exc:
        _parse(provider="  ", actor_id="", name=" ")
    assert exc.value.code == "ACTOR_REQUIRED"


def test_provider_without_id_is_invalid() -> None:
    with pytest.raises(ActorError) as exc:
        _parse(provider="dingtalk")
    assert exc.value.code == "ACTOR_INVALID"
    assert exc.value.field == "id"


def test_id_without_provider_is_invalid() -> None:
    with pytest.raises(ActorError) as exc:
        _parse(actor_id="u1")
    assert exc.value.code == "ACTOR_INVALID"
    assert exc.value.field == "provider"


@pytest.mark.parametrize("provider", ["DingTalk", "ding_talk", "-dingtalk", "a" * 33, "1abc"])
def test_bad_provider_format(provider) -> None:
    with pytest.raises(ActorError) as exc:
        _parse(provider=provider, actor_id="u1")
    assert exc.value.code == "ACTOR_INVALID"
    assert exc.value.field == "provider"


def test_provider_is_not_whitelisted() -> None:
    assert _parse(provider="wecom", actor_id="u1").provider == "wecom"


def test_id_length_boundary() -> None:
    assert _parse(provider="dingtalk", actor_id="u" * 128).actor_id == "u" * 128
    with pytest.raises(ActorError) as exc:
        _parse(provider="dingtalk", actor_id="u" * 129)
    assert exc.value.field == "id"


@pytest.mark.parametrize("actor_id", ["has space", "has\nnewline", "中文id", "tab\tchar"])
def test_id_must_be_visible_ascii(actor_id) -> None:
    # Raw bytes bypass Headers' latin-1 encoding, mirroring what the HTTP layer delivers.
    headers = Headers(
        raw=[
            (b"x-baseward-actor-provider", b"dingtalk"),
            (b"x-baseward-actor-id", actor_id.encode("utf-8")),
        ]
    )
    with pytest.raises(ActorError) as exc:
        parse_actor_headers(headers)
    assert exc.value.code == "ACTOR_INVALID"
    assert exc.value.field == "id"


def test_name_bad_base64_is_invalid() -> None:
    with pytest.raises(ActorError) as exc:
        _parse(provider="dingtalk", actor_id="u1", name="!!!not-base64!!!")
    assert exc.value.field == "name"


def test_name_bad_utf8_is_invalid() -> None:
    bad = base64.urlsafe_b64encode(b"\xff\xfe").decode("ascii")
    with pytest.raises(ActorError) as exc:
        _parse(provider="dingtalk", actor_id="u1", name=bad)
    assert exc.value.field == "name"


def test_name_control_character_is_invalid() -> None:
    with pytest.raises(ActorError) as exc:
        _parse(provider="dingtalk", actor_id="u1", name=_b64("a\x00b"))
    assert exc.value.field == "name"


def test_name_truncated_to_128_chars() -> None:
    actor = _parse(provider="dingtalk", actor_id="u1", name=_b64("长" * 200))
    assert actor.name == "长" * 128


def test_name_accepts_padded_and_unpadded_base64url() -> None:
    assert _parse(provider="dingtalk", actor_id="u1", name="YQ").name == "a"
    assert _parse(provider="dingtalk", actor_id="u1", name="YQ==").name == "a"


def test_header_names_are_case_insensitive() -> None:
    actor = parse_actor_headers(
        Headers({"x-baseward-actor-provider": "dingtalk", "x-baseward-actor-id": "u1"})
    )
    assert actor.actor_id == "u1"


def test_error_message_is_human_readable() -> None:
    with pytest.raises(ActorError) as exc:
        _parse()
    assert "X-BaseWard-Actor" in str(exc.value)