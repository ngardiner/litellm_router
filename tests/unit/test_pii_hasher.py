"""
Unit tests for the PII Hasher module.
"""

import hashlib
import pytest
from typing import Optional
from unittest.mock import MagicMock, patch, AsyncMock

from routing.modules.pii_hasher import (
    PIIHasher,
    _make_token,
    _hash_text,
    _split_safe_and_partial,
)


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

class TestMakeToken:
    def test_deterministic(self):
        t1 = _make_token("EMAIL", "user@example.com")
        t2 = _make_token("EMAIL", "user@example.com")
        assert t1 == t2

    def test_format(self):
        token = _make_token("EMAIL", "user@example.com")
        assert token.startswith("[PII_EMAIL_")
        assert token.endswith("]")
        assert len(token) == len("[PII_EMAIL_") + 8 + 1  # prefix + 8 hex + ]

    def test_different_values_different_tokens(self):
        assert _make_token("EMAIL", "a@a.com") != _make_token("EMAIL", "b@b.com")

    def test_different_types_different_tokens(self):
        assert _make_token("EMAIL", "x") != _make_token("PHONE", "x")

    def test_uses_sha256(self):
        value = "test@example.com"
        expected_hash = hashlib.sha256(value.encode()).hexdigest()[:8]
        assert _make_token("EMAIL", value) == f"[PII_EMAIL_{expected_hash}]"


# ---------------------------------------------------------------------------
# Pattern detection via _hash_text
# ---------------------------------------------------------------------------

class TestHashText:
    def test_email_detected(self):
        text, tokens = _hash_text("Contact us at admin@example.com please")
        assert len(tokens) == 1
        assert tokens[0]["entity_type"] == "EMAIL"
        assert tokens[0]["original"] == "admin@example.com"
        assert tokens[0]["token"] in text
        assert "admin@example.com" not in text

    def test_phone_detected(self):
        text, tokens = _hash_text("Call me on 555-867-5309 tomorrow")
        assert any(t["entity_type"] == "PHONE" for t in tokens)
        assert "555-867-5309" not in text

    def test_ssn_detected(self):
        text, tokens = _hash_text("SSN: 123-45-6789")
        assert any(t["entity_type"] == "SSN" for t in tokens)
        assert "123-45-6789" not in text

    def test_ip_addr_detected(self):
        text, tokens = _hash_text("Server at 192.168.1.100")
        assert any(t["entity_type"] == "IP_ADDR" for t in tokens)
        assert "192.168.1.100" not in text

    def test_no_pii_returns_unchanged(self):
        text = "Hello, world! This is a normal sentence."
        result, tokens = _hash_text(text)
        assert result == text
        assert tokens == []

    def test_multiple_pii_types(self):
        text = "Email admin@example.com or call 555-867-5309"
        result, tokens = _hash_text(text)
        types = {t["entity_type"] for t in tokens}
        assert "EMAIL" in types
        assert "PHONE" in types
        assert "admin@example.com" not in result
        assert "555-867-5309" not in result

    def test_same_value_same_token(self):
        text = "Email admin@example.com or admin@example.com again"
        result, tokens = _hash_text(text)
        # Two matches but the same token (deterministic)
        token_vals = [t["token"] for t in tokens]
        assert len(set(token_vals)) == 1

    def test_ssn_wins_over_phone_overlap(self):
        # SSN pattern is more specific; patterns are non-overlapping
        text = "123-45-6789"
        _, tokens = _hash_text(text)
        # Should match SSN, not attempt PHONE on same span
        assert any(t["entity_type"] == "SSN" for t in tokens)

    def test_token_roundtrip_present_in_text(self):
        text = "Reach me at dev@example.org"
        result, tokens = _hash_text(text)
        assert tokens[0]["token"] in result

    def test_empty_string(self):
        text, tokens = _hash_text("")
        assert text == ""
        assert tokens == []


# ---------------------------------------------------------------------------
# Streaming chunk boundary splitting
# ---------------------------------------------------------------------------

class TestSplitSafeAndPartial:
    def test_no_bracket_all_safe(self):
        safe, held = _split_safe_and_partial("hello world")
        assert safe == "hello world"
        assert held == ""

    def test_complete_token_all_safe(self):
        token = _make_token("EMAIL", "a@b.com")
        text = f"reply to {token} please"
        safe, held = _split_safe_and_partial(text)
        assert safe == text
        assert held == ""

    def test_partial_pii_prefix_held(self):
        safe, held = _split_safe_and_partial("text [PII_EMAIL_abc")
        assert held == "[PII_EMAIL_abc"
        assert safe == "text "

    def test_just_open_bracket_held(self):
        safe, held = _split_safe_and_partial("hello [")
        assert held == "["
        assert safe == "hello "

    def test_pii_prefix_only_held(self):
        safe, held = _split_safe_and_partial("data [PII_")
        assert held == "[PII_"
        assert safe == "data "

    def test_non_pii_bracket_not_held(self):
        # "[other" definitely cannot become a PII token
        safe, held = _split_safe_and_partial("text [other")
        assert safe == "text [other"
        assert held == ""

    def test_complete_token_then_partial(self):
        token = _make_token("EMAIL", "x@y.com")
        text = f"first {token} then [PII_"
        safe, held = _split_safe_and_partial(text)
        assert held == "[PII_"
        assert safe == f"first {token} then "

    def test_empty_string(self):
        safe, held = _split_safe_and_partial("")
        assert safe == ""
        assert held == ""


# ---------------------------------------------------------------------------
# PIIHasher class (with DB mocked)
# ---------------------------------------------------------------------------

def _make_hasher() -> PIIHasher:
    """Instantiate PIIHasher with the DB calls mocked out."""
    with patch("routing.modules.pii_hasher.get_simple_connection"):
        return PIIHasher(token_ttl_hours=1, enabled=True)


class TestPIIHasherHashMessages:
    def test_string_content_hashed(self):
        hasher = _make_hasher()
        messages = [{"role": "user", "content": "my email is me@example.com"}]
        with patch.object(hasher, "_store_tokens") as mock_store:
            new_msgs, changed = hasher._hash_messages(messages, "req-1")
        assert changed is True
        assert "me@example.com" not in new_msgs[0]["content"]
        mock_store.assert_called_once()

    def test_no_pii_unchanged(self):
        hasher = _make_hasher()
        messages = [{"role": "user", "content": "no PII here at all"}]
        with patch.object(hasher, "_store_tokens") as mock_store:
            new_msgs, changed = hasher._hash_messages(messages, "req-2")
        assert changed is False
        assert new_msgs[0]["content"] == "no PII here at all"
        mock_store.assert_not_called()

    def test_multimodal_text_parts_hashed(self):
        hasher = _make_hasher()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "call 555-111-2222"},
                    {"type": "image_url", "image_url": {"url": "http://img"}},
                ],
            }
        ]
        with patch.object(hasher, "_store_tokens"):
            new_msgs, changed = hasher._hash_messages(messages, "req-3")
        assert changed is True
        text_part = new_msgs[0]["content"][0]
        assert "555-111-2222" not in text_part["text"]
        # image part is untouched
        assert new_msgs[0]["content"][1]["type"] == "image_url"

    def test_non_string_content_passed_through(self):
        hasher = _make_hasher()
        messages = [{"role": "user", "content": None}]
        with patch.object(hasher, "_store_tokens"):
            new_msgs, changed = hasher._hash_messages(messages, "req-4")
        assert changed is False
        assert new_msgs[0]["content"] is None


class TestPIIHasherRestoreText:
    def test_restores_known_token(self):
        hasher = _make_hasher()
        token = _make_token("EMAIL", "secret@corp.com")
        with patch.object(hasher, "_lookup_tokens", return_value={token: "secret@corp.com"}):
            restored = hasher._restore_text(f"email: {token}")
        assert restored == "email: secret@corp.com"

    def test_unknown_token_left_in_place(self):
        hasher = _make_hasher()
        token = _make_token("EMAIL", "x@y.com")
        with patch.object(hasher, "_lookup_tokens", return_value={}):
            restored = hasher._restore_text(f"email: {token}")
        # token stays — we never strip unknown tokens, avoids data loss
        assert token in restored

    def test_no_token_marker_skips_db(self):
        hasher = _make_hasher()
        with patch.object(hasher, "_lookup_tokens") as mock_lookup:
            result = hasher._restore_text("plain text with no brackets")
        mock_lookup.assert_not_called()
        assert result == "plain text with no brackets"


class TestPIIHasherPreRequestHook:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_pii(self):
        hasher = _make_hasher()
        messages = [{"role": "user", "content": "no PII"}]
        kwargs = {"litellm_call_id": "call-1"}
        result = await hasher.async_pre_request_hook("gpt-4", messages, kwargs)
        assert result is None

    @pytest.mark.asyncio
    async def test_mutates_messages_in_place_when_pii_found(self):
        hasher = _make_hasher()
        messages = [{"role": "user", "content": "email me@test.com"}]
        kwargs = {"litellm_call_id": "call-2"}
        with patch.object(hasher, "_store_tokens"):
            result = await hasher.async_pre_request_hook("gpt-4", messages, kwargs)
        # Always returns None — messages is mutated in-place, never in the return dict
        assert result is None
        assert "me@test.com" not in messages[0]["content"]

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        with patch("routing.modules.pii_hasher.get_simple_connection"):
            hasher = PIIHasher(enabled=False)
        messages = [{"role": "user", "content": "a@b.com"}]
        kwargs = {"litellm_call_id": "c3"}
        result = await hasher.async_pre_request_hook("gpt-4", messages, kwargs)
        assert result is None


class TestPIIHasherPreCallHook:
    @pytest.mark.asyncio
    async def test_hashes_messages_in_data(self):
        hasher = _make_hasher()
        data = {
            "messages": [{"role": "user", "content": "email me@test.com"}],
            "litellm_call_id": "hook-1",
        }
        with patch.object(hasher, "_store_tokens"):
            result = await hasher.async_pre_call_hook(None, None, data, "completion")
        assert result is not None
        assert "me@test.com" not in result["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_returns_none_when_no_pii(self):
        hasher = _make_hasher()
        data = {
            "messages": [{"role": "user", "content": "no PII here"}],
            "litellm_call_id": "hook-2",
        }
        result = await hasher.async_pre_call_hook(None, None, data, "completion")
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_non_completion_calls(self):
        hasher = _make_hasher()
        data = {"messages": [{"role": "user", "content": "user@example.com"}], "litellm_call_id": "hook-3"}
        result = await hasher.async_pre_call_hook(None, None, data, "embeddings")
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        with patch("routing.modules.pii_hasher.get_simple_connection"):
            hasher = PIIHasher(enabled=False)
        data = {"messages": [{"role": "user", "content": "email foo@bar.com"}], "litellm_call_id": "hook-4"}
        result = await hasher.async_pre_call_hook(None, None, data, "completion")
        assert result is None


class TestPIIHasherPostCallHook:
    def _make_response(self, content: str):
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = content
        response.choices = [choice]
        return response

    @pytest.mark.asyncio
    async def test_restores_token_in_response(self):
        hasher = _make_hasher()
        token = _make_token("EMAIL", "private@corp.com")
        response = self._make_response(f"Your email is {token}.")

        with patch.object(hasher, "_lookup_tokens", return_value={token: "private@corp.com"}):
            result = await hasher.async_post_call_success_deployment_hook(
                {}, response, None
            )

        assert result is not None
        assert result.choices[0].message.content == "Your email is private@corp.com."

    @pytest.mark.asyncio
    async def test_returns_none_when_no_tokens(self):
        hasher = _make_hasher()
        response = self._make_response("No sensitive content here.")
        result = await hasher.async_post_call_success_deployment_hook({}, response, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        with patch("routing.modules.pii_hasher.get_simple_connection"):
            hasher = PIIHasher(enabled=False)
        token = _make_token("EMAIL", "x@x.com")
        response = self._make_response(f"See {token}")
        result = await hasher.async_post_call_success_deployment_hook({}, response, None)
        assert result is None


class TestPIIHasherStreamingHook:
    def _make_chunk(self, content: Optional[str], finish_reason=None):
        chunk = MagicMock()
        chunk.choices[0].delta.content = content
        chunk.choices[0].finish_reason = finish_reason
        return chunk

    @pytest.mark.asyncio
    async def test_complete_token_in_single_chunk_restored(self):
        hasher = _make_hasher()
        token = _make_token("EMAIL", "x@y.com")
        chunk = self._make_chunk(f"email: {token}", finish_reason="stop")

        with patch.object(hasher, "_lookup_tokens", return_value={token: "x@y.com"}):
            result = await hasher.async_post_call_streaming_deployment_hook(
                {"litellm_call_id": "s1"}, chunk, None
            )

        assert result is not None
        assert result.choices[0].delta.content == "email: x@y.com"

    @pytest.mark.asyncio
    async def test_partial_token_held_across_chunks(self):
        hasher = _make_hasher()
        token = _make_token("EMAIL", "split@example.com")
        mid = len(token) // 2
        chunk1 = self._make_chunk(f"see {token[:mid]}")
        chunk2 = self._make_chunk(token[mid:], finish_reason="stop")

        with patch.object(
            hasher,
            "_lookup_tokens",
            return_value={token: "split@example.com"},
        ):
            r1 = await hasher.async_post_call_streaming_deployment_hook(
                {"litellm_call_id": "s2"}, chunk1, None
            )
            r2 = await hasher.async_post_call_streaming_deployment_hook(
                {"litellm_call_id": "s2"}, chunk2, None
            )

        # First chunk held back the partial token; second chunk flushes and restores
        combined = ""
        if r1 is not None:
            combined += r1.choices[0].delta.content or ""
        if r2 is not None:
            combined += r2.choices[0].delta.content or ""

        assert "split@example.com" in combined
        assert token not in combined

    @pytest.mark.asyncio
    async def test_none_content_delta_returns_none(self):
        hasher = _make_hasher()
        chunk = self._make_chunk(None)
        result = await hasher.async_post_call_streaming_deployment_hook(
            {"litellm_call_id": "s3"}, chunk, None
        )
        assert result is None
