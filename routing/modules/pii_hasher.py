"""
PII Hasher — bi-directional PII tokenization module.

Outbound (pre-request): detects PII in messages via regex patterns and replaces
each match with a deterministic opaque token ([PII_{TYPE}_{SHA256_8}]).

Inbound (post-response): looks up tokens in the database and restores the
original values before the response reaches the client.

Tokens are stored with a configurable TTL. The same original value always maps
to the same token (deterministic), so a single request that contains the same
email address twice only stores one DB row.

Streaming responses are handled with a per-request chunk buffer to safely
straddle token boundaries across chunks.

Registered as a LiteLLM CustomLogger via litellm_settings.callbacks in config.yaml:

    litellm_settings:
      callbacks:
        - routing.modules.pii_hasher.pii_hasher

Requires DATABASE_URL in the environment.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from ..database.connection import get_simple_connection
except ImportError:
    # Loaded standalone by LiteLLM (get_instance_fn, no package context)
    import os as _os
    import psycopg2 as _psycopg2

    def get_simple_connection():  # type: ignore[misc]
        import urllib.parse as _urlparse
        url = _os.environ["DATABASE_URL"]
        # LiteLLM appends Prisma params (connection_limit, pool_timeout) at startup;
        # strip them since psycopg2 rejects unknown query parameters.
        parsed = _urlparse.urlparse(url)
        clean_url = parsed._replace(query="").geturl()
        return _psycopg2.connect(clean_url)

logger = logging.getLogger(__name__)

# Module metadata (used by the module loader / web interface)
MODULE_NAME = "PII Hasher"
MODULE_DESCRIPTION = (
    "Bi-directional PII tokenization — hash PII outbound, restore on response"
)
MODULE_VERSION = "1.0.0"
ROUTING_MODULE = False  # CustomLogger callback, not a routing module

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "token_ttl_hours": {
            "type": "integer",
            "default": 24,
            "description": "Hours to keep token→original mappings in the database",
        },
        "enabled": {
            "type": "boolean",
            "default": True,
            "description": "Master switch; set false to pass all traffic through unmodified",
        },
    },
}

# ---------------------------------------------------------------------------
# PII patterns — ordered most-specific first to resolve overlaps predictably.
# Each entry: (entity_type_label, compiled_regex)
# Add new types here; the rest of the module is pattern-agnostic.
# ---------------------------------------------------------------------------
_PII_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "EMAIL",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        "PHONE",
        re.compile(
            r"\b(?:\+?1[-.\s]?)?\(?(\d{3})\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
        ),
    ),
    (
        "IP_ADDR",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
        ),
    ),
]

# Token format: [PII_{TYPE}_{8_HEX_CHARS}]
_TOKEN_RE = re.compile(r"\[PII_([A-Z_]+)_([0-9a-f]{8})\]")
# Partial-token candidate: could be the beginning of a token
_PARTIAL_TOKEN_RE = re.compile(
    r"^\[(?:P(?:I(?:I(?:_(?:[A-Z_]*(?:_[0-9a-f]{0,8}\]?)?)?)?)?)?)?$"
)


# ---------------------------------------------------------------------------
# Pure functions — no DB, no class state
# ---------------------------------------------------------------------------

def _make_token(entity_type: str, value: str) -> str:
    """Deterministic token for a PII value."""
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"[PII_{entity_type}_{digest}]"


def _hash_text(text: str) -> Tuple[str, List[Dict[str, str]]]:
    """
    Scan *text* for PII and replace each match with a token.

    Returns:
        (hashed_text, tokens) where tokens is a list of
        {"token": ..., "original": ..., "entity_type": ...} dicts.
    """
    # Collect all non-overlapping matches across all patterns
    all_matches: List[Tuple[int, int, str, str]] = []
    for entity_type, pattern in _PII_PATTERNS:
        for m in pattern.finditer(text):
            all_matches.append((m.start(), m.end(), m.group(), entity_type))

    if not all_matches:
        return text, []

    # Sort by position; discard overlapping matches (first / most specific wins)
    all_matches.sort(key=lambda x: x[0])
    non_overlapping: List[Tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, value, entity_type in all_matches:
        if start >= last_end:
            non_overlapping.append((start, end, value, entity_type))
            last_end = end

    # Single-pass substitution
    parts: List[str] = []
    tokens: List[Dict[str, str]] = []
    pos = 0
    for start, end, value, entity_type in non_overlapping:
        parts.append(text[pos:start])
        token = _make_token(entity_type, value)
        parts.append(token)
        tokens.append({"token": token, "original": value, "entity_type": entity_type})
        pos = end
    parts.append(text[pos:])

    return "".join(parts), tokens


def _split_safe_and_partial(text: str) -> Tuple[str, str]:
    """
    Split *text* into a safe-to-process portion and a held-back tail.

    The tail is held back when it ends with what could be the beginning of a
    PII token that hasn't been fully received yet (chunk-boundary case).

    Returns:
        (safe, held) — reassemble with safe + held to get the original text.
    """
    last_open = text.rfind("[")
    if last_open == -1:
        return text, ""

    tail = text[last_open:]

    # Fast path: complete token(s) in the tail
    complete = list(_TOKEN_RE.finditer(tail))
    if complete:
        after = tail[complete[-1].end():]
        if "[" not in after:
            return text, ""
        # Another unclosed bracket after the last complete token
        split_at = last_open + complete[-1].end() + after.rfind("[")
        return text[:split_at], text[split_at:]

    # No complete token — hold back if the tail could grow into one
    if _PARTIAL_TOKEN_RE.match(tail):
        return text[:last_open], tail

    return text, ""


# ---------------------------------------------------------------------------
# CustomLogger subclass
# ---------------------------------------------------------------------------

try:
    from litellm.integrations.custom_logger import CustomLogger as _CustomLogger
except ImportError:
    # Allow the module to be imported in environments without litellm
    # (e.g. unit tests that mock everything)
    class _CustomLogger:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass


class PIIHasher(_CustomLogger):
    """
    LiteLLM CustomLogger that tokenizes PII on the way out and restores it
    on the way back.

    One module-level singleton (``pii_hasher``) is registered as a callback.
    The table is created lazily on first DB write so startup never blocks.
    """

    def __init__(self, token_ttl_hours: int = 24, enabled: bool = True, **kwargs):
        self.token_ttl_hours = token_ttl_hours
        self.enabled = enabled
        # {litellm_call_id: partial_chunk_string}
        self._stream_buffers: Dict[str, str] = {}
        self._table_ready = False
        super().__init__(**kwargs)
        logger.info(
            "PIIHasher initialised (ttl=%dh, enabled=%s)", token_ttl_hours, enabled
        )
        self._ensure_table()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Idempotent table + index creation. Failures are non-fatal."""
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS pii_tokens (
                            id          SERIAL PRIMARY KEY,
                            token       VARCHAR(64) UNIQUE NOT NULL,
                            original_value TEXT NOT NULL,
                            entity_type VARCHAR(50) NOT NULL,
                            request_id  VARCHAR(255),
                            created_at  TIMESTAMP DEFAULT NOW(),
                            expires_at  TIMESTAMP WITH TIME ZONE NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_pii_tokens_token "
                        "ON pii_tokens(token)"
                    )
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_pii_tokens_expires "
                        "ON pii_tokens(expires_at)"
                    )
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_pii_tokens_request "
                        "ON pii_tokens(request_id) WHERE request_id IS NOT NULL"
                    )
                    conn.commit()
            self._table_ready = True
        except Exception as exc:
            logger.warning("PIIHasher: could not ensure table: %s", exc)

    def _store_tokens(self, tokens: List[Dict[str, str]], request_id: str) -> None:
        """Upsert token→original rows, extending TTL on conflict."""
        if not tokens:
            return
        if not self._table_ready:
            self._ensure_table()

        expires_at = datetime.now(timezone.utc) + timedelta(hours=self.token_ttl_hours)
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    for entry in tokens:
                        cursor.execute(
                            """
                            INSERT INTO pii_tokens
                                (token, original_value, entity_type, request_id, expires_at)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (token) DO UPDATE SET
                                expires_at = GREATEST(pii_tokens.expires_at, EXCLUDED.expires_at)
                            """,
                            (
                                entry["token"],
                                entry["original"],
                                entry["entity_type"],
                                request_id,
                                expires_at,
                            ),
                        )
                    conn.commit()
        except Exception as exc:
            logger.error("PIIHasher: failed to store tokens for %s: %s", request_id, exc)

    def _lookup_tokens(self, token_strings: List[str]) -> Dict[str, str]:
        """Return {token: original_value} for all live tokens in the list."""
        if not token_strings:
            return {}
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT token, original_value FROM pii_tokens "
                        "WHERE token = ANY(%s) AND expires_at > NOW()",
                        (token_strings,),
                    )
                    return {row[0]: row[1] for row in cursor.fetchall()}
        except Exception as exc:
            logger.error("PIIHasher: token lookup failed: %s", exc)
            return {}

    def _restore_text(self, text: str) -> str:
        """Replace all live PII tokens in *text* with their originals."""
        if "[PII_" not in text:
            return text

        found = [f"[PII_{t}_{h}]" for t, h in _TOKEN_RE.findall(text)]
        if not found:
            return text

        mapping = self._lookup_tokens(list(set(found)))
        for token, original in mapping.items():
            text = text.replace(token, original)
        return text

    # ------------------------------------------------------------------
    # Message-level helpers
    # ------------------------------------------------------------------

    def _hash_messages(
        self, messages: List[Dict], request_id: str
    ) -> Tuple[List[Dict], bool]:
        """
        Walk *messages* and hash any PII found in text content.

        Returns:
            (modified_messages, was_any_change_made)
        """
        result: List[Dict] = []
        changed = False
        all_tokens: List[Dict[str, str]] = []

        for msg in messages:
            content = msg.get("content")

            if isinstance(content, str):
                new_content, tokens = _hash_text(content)
                if tokens:
                    changed = True
                    all_tokens.extend(tokens)
                    result.append({**msg, "content": new_content})
                else:
                    result.append(msg)

            elif isinstance(content, list):
                new_parts: List[Any] = []
                part_changed = False
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        new_text, tokens = _hash_text(part.get("text", ""))
                        if tokens:
                            part_changed = True
                            changed = True
                            all_tokens.extend(tokens)
                            new_parts.append({**part, "text": new_text})
                        else:
                            new_parts.append(part)
                    else:
                        new_parts.append(part)
                result.append({**msg, "content": new_parts} if part_changed else msg)

            else:
                result.append(msg)

        if all_tokens:
            self._store_tokens(all_tokens, request_id)

        return result, changed

    # ------------------------------------------------------------------
    # LiteLLM hooks
    # ------------------------------------------------------------------

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: Dict,
        call_type: str,
    ) -> Optional[Dict]:
        """
        Hash PII for the standard OpenAI-compat endpoint (/v1/chat/completions).

        Returns a modified data dict, or None if nothing changed.
        """
        if not self.enabled:
            return None

        if call_type not in ("completion", "text_completion"):
            return None

        msgs = data.get("messages")
        if not msgs:
            return None

        request_id = data.get("litellm_call_id", "unknown")
        new_messages, changed = self._hash_messages(msgs, request_id)
        if not changed:
            return None

        logger.info("PIIHasher: hashed PII in request %s (pre_call)", request_id)
        return {**data, "messages": new_messages}

    async def async_pre_request_hook(
        self, model: str, messages: List, kwargs: Dict
    ) -> Optional[Dict]:
        """
        Hash PII for the Anthropic passthrough endpoint (/anthropic/v1/messages).

        In the Anthropic path, LiteLLM passes `messages` as a separate variable and
        uses the same reference after the hook returns. The hook return value is merged
        back into the outer **kwargs via `kwargs.update(request_kwargs)`. Returning a
        dict containing `messages` would inject it into the outer kwargs, causing
        `partial(messages=messages, **kwargs)` to fail with "multiple values".

        The fix: always mutate the `messages` list in-place and return None.
        """
        if not self.enabled:
            return None

        effective_messages = messages or []
        if not effective_messages:
            return None

        request_id = kwargs.get("litellm_call_id", "unknown")
        new_messages, changed = self._hash_messages(effective_messages, request_id)
        if not changed:
            return None

        # Mutate in-place — the Anthropic handler holds the same list reference
        messages.clear()
        messages.extend(new_messages)

        logger.info("PIIHasher: hashed PII in request %s (pre_request)", request_id)
        return None  # never include messages in returned dict

    async def async_post_call_success_deployment_hook(
        self,
        request_data: dict,
        response: Any,
        call_type: Any,
    ) -> Optional[Any]:
        """
        Restore PII tokens in the LLM's response before it reaches the client.

        Returns the mutated response object if any replacements were made,
        otherwise None (LiteLLM keeps the original).
        """
        if not self.enabled:
            return None

        try:
            choices = getattr(response, "choices", None)
            if not choices:
                return None

            changed = False
            for choice in choices:
                message = getattr(choice, "message", None)
                if message is None:
                    continue
                content = getattr(message, "content", None)
                if isinstance(content, str):
                    restored = self._restore_text(content)
                    if restored != content:
                        message.content = restored
                        changed = True

            return response if changed else None

        except Exception as exc:
            logger.error("PIIHasher: post-call hook error: %s", exc)
            return None

    async def async_post_call_streaming_deployment_hook(
        self,
        request_data: dict,
        response_chunk: Any,
        call_type: Any,
    ) -> Optional[Any]:
        """
        Restore PII tokens in each streaming chunk.

        Chunks are buffered per-request so that tokens split across chunk
        boundaries are reassembled before lookup.
        """
        if not self.enabled:
            return None

        try:
            request_id = request_data.get("litellm_call_id", "")

            # Extract the text delta from this chunk
            delta_content: Optional[str] = None
            try:
                choices = getattr(response_chunk, "choices", None)
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    if delta:
                        delta_content = getattr(delta, "content", None)
            except Exception:
                return None

            if delta_content is None:
                return None

            buf = self._stream_buffers.get(request_id, "") + delta_content
            safe, held = _split_safe_and_partial(buf)

            # On the final chunk, flush everything
            finish_reason = None
            try:
                finish_reason = getattr(response_chunk.choices[0], "finish_reason", None)
            except Exception:
                pass

            if finish_reason:
                safe = safe + held
                self._stream_buffers.pop(request_id, None)
            else:
                self._stream_buffers[request_id] = held

            if not safe:
                return None

            restored = self._restore_text(safe)
            if restored == safe:
                return None

            try:
                response_chunk.choices[0].delta.content = restored
            except Exception:
                pass

            return response_chunk

        except Exception as exc:
            logger.error("PIIHasher: streaming hook error: %s", exc)
            return None

    async def async_logging_hook(
        self,
        kwargs: dict,
        result: Any,
        call_type: str,
    ) -> Tuple[dict, Any]:
        """
        Scrub PII from logged request/response before LiteLLM writes to spend_logs.

        Called just before data is persisted. Applies the same tokenization as the
        pre-call hook, so it is idempotent on already-tokenized messages and catches
        anything that bypassed the pre-call path (e.g. Anthropic passthrough).
        """
        if not self.enabled:
            return kwargs, result

        try:
            request_id = kwargs.get("litellm_call_id", "unknown")

            # Scrub request messages in kwargs
            messages = kwargs.get("messages")
            if messages and isinstance(messages, list):
                new_messages, changed = self._hash_messages(list(messages), request_id)
                if changed:
                    kwargs = {**kwargs, "messages": new_messages}

            # Scrub response content — post-call hooks may have already restored
            # tokens to original PII for the client; re-hash before logging.
            try:
                choices = getattr(result, "choices", None)
                if choices:
                    for choice in choices:
                        message = getattr(choice, "message", None)
                        if message is None:
                            continue
                        content = getattr(message, "content", None)
                        if isinstance(content, str):
                            hashed, tokens = _hash_text(content)
                            if tokens:
                                self._store_tokens(tokens, request_id)
                                message.content = hashed
            except Exception as exc:
                logger.debug("PIIHasher: logging hook result scrub: %s", exc)

            return kwargs, result

        except Exception as exc:
            logger.error("PIIHasher: async_logging_hook error: %s", exc)
            return kwargs, result

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def cleanup_expired_tokens(self) -> int:
        """Delete expired token rows. Safe to call on a schedule."""
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM pii_tokens WHERE expires_at < NOW()")
                    count = cursor.rowcount
                    conn.commit()
            logger.info("PIIHasher: purged %d expired tokens", count)
            return count
        except Exception as exc:
            logger.error("PIIHasher: cleanup error: %s", exc)
            return 0

    def get_token_stats(self) -> Dict[str, Any]:
        """Return a summary of current token table state."""
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) FROM pii_tokens WHERE expires_at > NOW()"
                    )
                    active = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM pii_tokens")
                    total = cursor.fetchone()[0]
                    cursor.execute(
                        "SELECT entity_type, COUNT(*) FROM pii_tokens "
                        "WHERE expires_at > NOW() GROUP BY entity_type ORDER BY entity_type"
                    )
                    by_type = {row[0]: row[1] for row in cursor.fetchall()}
            return {"active": active, "total": total, "by_entity_type": by_type}
        except Exception as exc:
            logger.error("PIIHasher: stats error: %s", exc)
            return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Module-level singleton — this is what config.yaml points to:
#   routing.modules.pii_hasher.pii_hasher
# ---------------------------------------------------------------------------
pii_hasher = PIIHasher()
