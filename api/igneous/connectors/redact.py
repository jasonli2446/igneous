"""Strip credentials from text before it is stored or sent to a model.

AI chat transcripts are full of pasted secrets (tokens, keys, connection strings). Anything matching here is
replaced with `[REDACTED:<kind>]` at ingestion time, so it never reaches source_items, chunks, embeddings,
or a prompt. Patterns are deliberately broad: a false positive costs a few characters of context; a false
negative leaks a credential.
"""
import re

# (kind, pattern). Order matters: specific formats before the generic catch-alls.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("bedrock_bearer", re.compile(r"\bABSK[A-Za-z0-9+/=]{40,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("google_oauth", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}|\b1//[0-9A-Za-z_-]{30,}")),
    ("google_client_secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    # user:password@host in any URL scheme (postgres://, mongodb+srv://, https://...)
    ("url_credentials", re.compile(r"(?<=://)(?!\[REDACTED)[^\s:/@]+:[^\s@/]+(?=@)")),
    # KEY=value / "key": "value" where the name says it's a secret
    ("assigned_secret", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|AUTH)[A-Z0-9_]*)"
        r"(\s*[:=]\s*['\"]?)(?!\[REDACTED)([^\s'\",;]{8,})")),
]


def redact(text: str | None) -> tuple[str | None, dict[str, int]]:
    """Returns (redacted text, {kind: count})."""
    if not text:
        return text, {}
    counts: dict[str, int] = {}
    for kind, pat in PATTERNS:
        def sub(m, kind=kind):
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "assigned_secret":  # keep the variable name; it's useful context and not secret
                return f"{m.group(1)}{m.group(2)}[REDACTED:{kind}]"
            return f"[REDACTED:{kind}]"
        text = pat.sub(sub, text)
    return text, counts
