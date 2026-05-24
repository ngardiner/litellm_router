"""
Prompt Injection Detector — first-pass heuristic filter.

Scans incoming message content for known prompt injection and jailbreak patterns
using regex matching on normalised text. Catches commodity attacks and known
jailbreak templates; makes no claim to stop a determined adversary.

Registered via the callback_dispatcher (implements async_moderation_hook).

Configuration (module-level via module_configs table):
    action          block | flag | log_only   (default: log_only)
    scan_roles      comma-separated roles to scan (default: user)
    custom_patterns newline-separated regex patterns to add to the built-in set

Actions:
    log_only    Log the detection and let the request through (safe default for tuning)
    flag        Let the request through but add X-Injection-Detected: true header
                (requires async_post_call_response_headers_hook to surface it)
    block       Reject the request with HTTP 400
"""

import logging
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MODULE_NAME = "Prompt Injection Detector"
MODULE_DESCRIPTION = (
    "First-pass heuristic filter for prompt injection and jailbreak attempts"
)
MODULE_VERSION = "1.0.0"
ROUTING_MODULE = False

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["log_only", "flag", "block"],
            "default": "log_only",
            "description": "What to do when an injection is detected",
        },
        "scan_roles": {
            "type": "string",
            "default": "user",
            "description": "Comma-separated message roles to scan (e.g. user,system)",
        },
        "custom_patterns": {
            "type": "string",
            "default": "",
            "description": "Additional regex patterns, one per line",
        },
    },
}

# ---------------------------------------------------------------------------
# Pattern library
# ---------------------------------------------------------------------------
# Each entry: (name, pattern_string)
# Patterns are applied case-insensitively against unicode-normalised text.
# Ordered roughly by specificity / confidence.
# ---------------------------------------------------------------------------

_RAW_PATTERNS: List[Tuple[str, str]] = [

    # --- Explicit instruction override ---
    ("override_ignore",
     r"\b(?:ignore|disregard|forget|bypass|override)\s+(?:all\s+)?(?:previous|prior|above|earlier|your|the\s+previous)\s+instructions?\b"),

    ("new_instructions",
     r"\bnew\s+instructions?\s*:"),

    ("override_instructions",
     r"\boverride\s+(?:your\s+)?(?:instructions?|restrictions?|guidelines?|programming|training)\b"),

    # --- System prompt leakage / extraction ---
    ("extract_system_prompt",
     r"\b(?:reveal|repeat|show|print|output|display|tell\s+me|what\s+(?:are|were|is))\s+(?:your\s+)?(?:system\s+prompt|original\s+instructions?|initial\s+instructions?|hidden\s+instructions?|true\s+instructions?)\b"),

    ("leak_instructions",
     r"\b(?:what\s+(?:are|were)\s+your\s+instructions?|repeat\s+(?:your|the)\s+(?:system\s+)?instructions?)\b"),

    # --- Permission / restriction removal ---
    ("remove_restrictions",
     r"\bwithout\s+(?:any\s+)?(?:restrictions?|limitations?|ethical\s+(?:guidelines?|constraints?)|safety\s+guidelines?|filters?)\b"),

    ("ignore_safety",
     r"\b(?:ignore|bypass|disable|remove|turn\s+off)\s+(?:your\s+)?(?:ethical\s+)?(?:guidelines?|restrictions?|limitations?|safety|filters?|training|values?)\b"),

    ("no_restrictions_persona",
     r"\b(?:you\s+(?:have|had)\s+no\s+restrictions?|you\s+(?:are|were)\s+(?:not|no\s+longer)\s+(?:bound|limited|restricted|constrained))\b"),

    # --- Role / identity injection ---
    ("you_are_now",
     r"\byou\s+are\s+now\s+(?:a\s+)?(?:DAN|STAN|DUDE|AIM|GPT-?4|an?\s+unrestricted|an?\s+unfiltered|an?\s+uncensored)\b"),

    ("developer_mode",
     r"\bdeveloper\s+mode\s*(?:enabled|on|activated|is\s+now\s+active)?\b"),

    ("dan_mode",
     r"\bDAN\s+mode\b"),

    ("jailbreak_explicit",
     r"\bjailbreak\b"),

    ("do_anything_now",
     r"\bdo\s+anything\s+now\b"),

    # --- True self / hidden purpose ---
    ("true_self",
     r"\byour\s+true\s+(?:purpose|self|nature|instructions?|capabilities?|identity)\b"),

    ("hidden_mode",
     r"\b(?:secret|hidden|real|true|actual)\s+(?:mode|instructions?|programming|training|guidelines?)\b"),

    # --- Pretend / roleplay with explicit permission removal ---
    ("pretend_no_restrictions",
     r"\bpretend\s+(?:you\s+(?:have\s+no|don.t\s+have|are\s+without)\s+(?:restrictions?|guidelines?|ethical|limitations?)|to\s+be\s+an?\s+(?:AI|model|assistant)\s+(?:with\s+no|without)\s+restrictions?)\b"),

    ("act_as_unrestricted",
     r"\bact\s+as\s+(?:if\s+you\s+(?:have\s+no|don.t\s+have|are\s+not\s+bound)|an?\s+(?:AI|assistant|model)\s+(?:with\s+no|without)\s+(?:restrictions?|ethical|guidelines?))\b"),

    # --- LLM control token injection ---
    # These tokens have no legitimate use in user message content
    ("control_tokens",
     r"<\|(?:im_start|im_end|system|endoftext|pad|unk|begin_of_text|end_of_text)\|>"),

    # --- Encoding-based obfuscation hints ---
    ("encoding_bypass",
     r"\b(?:base64|rot13|hex|morse|binary)\b.{0,80}\b(?:decode|encoded|encoded\s+as|cipher)\b"),
]

# Compile once at module load
_COMPILED: List[Tuple[str, re.Pattern]] = [
    (name, re.compile(pattern, re.IGNORECASE | re.UNICODE))
    for name, pattern in _RAW_PATTERNS
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Common Cyrillic/Greek characters visually identical to Latin equivalents,
# used in homoglyph substitution attacks (e.g. Cyrillic І instead of Latin I).
_CONFUSABLES: Dict[int, str] = {
    # Cyrillic look-alikes
    0x0410: "A", 0x0430: "a",  # А а → A a
    0x0412: "B",               # В → B
    0x0421: "C", 0x0441: "c",  # С с → C c
    0x0415: "E", 0x0435: "e",  # Е е → E e
    0x0406: "I", 0x0456: "i",  # І і → I i (Ukrainian/Belarusian)
    0x041E: "O", 0x043E: "o",  # О о → O o
    0x0420: "P", 0x0440: "p",  # Р р → P p
    0x0405: "S", 0x0455: "s",  # Ѕ ѕ → S s
    0x0422: "T",               # Т → T
    0x0425: "X", 0x0445: "x",  # Х х → X x
    0x0423: "Y", 0x0443: "y",  # У у → Y y (visually)
    # Greek look-alikes
    0x03BF: "o",               # ο (omicron) → o
    0x03B1: "a",               # α → a
    0x03B5: "e",               # ε → e
    0x03BD: "v",               # ν → v
}
_CONFUSABLES_TABLE = str.maketrans(_CONFUSABLES)


def _normalise(text: str) -> str:
    """NFKC normalisation then homoglyph substitution for common look-alike characters."""
    return unicodedata.normalize("NFKC", text).translate(_CONFUSABLES_TABLE)


def _extract_text(message: dict) -> List[str]:
    """Pull text strings from a message's content (str or list of parts)."""
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
    return []


def _scan(text: str, extra_patterns: List[re.Pattern]) -> Optional[str]:
    """
    Return the name of the first matching pattern, or None if clean.
    Normalises text before matching.
    """
    normalised = _normalise(text)
    for name, pattern in _COMPILED:
        if pattern.search(normalised):
            return name
    for i, pattern in enumerate(extra_patterns):
        if pattern.search(normalised):
            return f"custom_{i}"
    return None


def _load_config() -> dict:
    """Read module config from DB; return defaults on failure."""
    defaults = {"action": "log_only", "scan_roles": "user", "custom_patterns": ""}
    try:
        import os
        import psycopg2
        import urllib.parse
        url = os.environ["DATABASE_URL"]
        clean = urllib.parse.urlparse(url)._replace(query="").geturl()
        with psycopg2.connect(clean) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config_key, config_value FROM module_configs "
                    "WHERE module_name = %s",
                    ("prompt_injection_detector",),
                )
                rows = cur.fetchall()
        if rows:
            return {**defaults, **{k: v for k, v in rows}}
    except Exception as exc:
        logger.debug("PromptInjectionDetector: config load failed: %s", exc)
    return defaults


def _compile_custom(raw: str) -> List[re.Pattern]:
    """Compile newline-separated custom patterns; skip blanks and bad patterns."""
    compiled = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            compiled.append(re.compile(line, re.IGNORECASE | re.UNICODE))
        except re.error as exc:
            logger.warning("PromptInjectionDetector: bad custom pattern %r: %s", line, exc)
    return compiled


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

async def async_moderation_hook(data: dict, user_api_key_dict, call_type: str):
    """
    Scan message content for injection patterns.

    - log_only: log and pass through (default — use while calibrating)
    - flag:     pass through, set a flag in data metadata for downstream use
    - block:    reject with HTTP 400
    """
    config = _load_config()
    action = config.get("action", "log_only")
    scan_roles = {r.strip() for r in config.get("scan_roles", "user").split(",")}
    extra_patterns = _compile_custom(config.get("custom_patterns", ""))

    messages = data.get("messages", [])
    if not messages:
        return None

    request_id = data.get("litellm_call_id", "unknown")

    for message in messages:
        role = message.get("role", "")
        if role not in scan_roles:
            continue

        for text in _extract_text(message):
            if not text:
                continue
            matched = _scan(text, extra_patterns)
            if matched:
                snippet = text[:120].replace("\n", " ")
                logger.warning(
                    "PromptInjectionDetector: pattern=%s request_id=%s action=%s snippet=%r",
                    matched, request_id, action, snippet,
                )

                if action == "block":
                    try:
                        from starlette.exceptions import HTTPException
                        raise HTTPException(
                            status_code=400,
                            detail=f"Request blocked: injection pattern detected ({matched})",
                        )
                    except ImportError:
                        raise ValueError(
                            f"Request blocked: injection pattern detected ({matched})"
                        )

                if action == "flag":
                    # Surface via response headers hook if wired up
                    metadata = data.setdefault("metadata", {})
                    metadata["injection_detected"] = matched
                    return data

                # log_only: fall through
                return None

    return None
