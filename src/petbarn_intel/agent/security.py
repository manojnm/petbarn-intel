"""Output-side leak detection. Runs on every synthesizer answer, regardless
of mode (fast or complex), before the answer leaves the graph.

The verifier only runs in 'complex' mode, so fast-mode answers were
previously unchecked. This module adds a lightweight scan on every path.
"""

from __future__ import annotations

import re

# Patterns that must never appear in an outgoing answer.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                          # OpenAI keys
    re.compile(r"gsk_[A-Za-z0-9]{10,}"),                         # Groq keys
    re.compile(r"AZURE_OPENAI_API_KEY\s*[=:]\s*\S+", re.I),     # Azure key in env-var form
    re.compile(r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----"), # PEM blocks
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\."),   # JWTs
]

# Detect if the synthesizer has leaked its own system prompt verbatim.
_PROMPT_LEAK_PATTERN = re.compile(
    r"You are the Petbarn shopping assistant talking directly to the customer",
    re.I,
)

_REFUSAL = (
    "I'm not able to share that information. Ask me about a Petbarn product, "
    "its price or specs, or what customers say about it."
)


def scan_output(text: str) -> str | None:
    """Return a refusal string if `text` appears to leak secrets or the
    system prompt; return None if the answer is clean.

    Called after synthesizer_node on every graph path so fast-mode answers
    are checked even though the verifier skips them.
    """
    if not text:
        return None
    if _PROMPT_LEAK_PATTERN.search(text):
        return _REFUSAL
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return _REFUSAL
    return None
