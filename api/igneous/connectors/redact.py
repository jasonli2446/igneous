"""Strip credentials from text before it is stored or sent to a model.

Mail, chats and AI transcripts are full of pasted secrets (tokens, keys, connection strings). The sync runner
(`base.run_sync`) passes every item's title, text and metadata text (not its ids, paths or addresses) through here
before storing it, so a match is replaced with `[REDACTED:<kind>]` and never reaches source_items, chunks,
embeddings or a prompt. Items stored under an earlier VERSION of these rules are brought up to date by the
pipeline's first step (`base.redact_stored`). A connector that shortens text redacts it first, so a secret cut in
half can't slip through. Patterns are deliberately broad: a false positive costs a few characters of context; a
false negative leaks a credential.

Every pattern runs in time linear in the text, whatever the text holds (an attachment's base64, a run of
kebab-case, a thousand key headers), so no item can stall a sync or a rebuild. The engine tries a pattern at each
position in turn, so a pattern must never rescan, from a later start, a run an earlier start read to its end: each
one starts only where its run begins, or bounds how far it reads. `tests/test_redact.py` times every pattern on such
texts.
"""
import re

# The version of PATTERNS that stored items carry (`source_items.redaction_version`). Bump it with any change to
# PATTERNS: the next pipeline run applies the new rules to every item stored under the old ones. Redacting is
# idempotent (a placeholder never matches again), so items without new matches stay as they are.
VERSION = 2

# What joins a name to its value: NAME=value, name: value, JSON's "name": "value" (escaped quotes too), and a
# call's ("name", "value"). Its whitespace is possessive (`*+`): giving any back could never let the rest match.
_JOIN = r"""(?:(?:\\?['"])?\s*+[:=]\s*+(?:\\?['"])?|\\?['"]\s*+,\s*+\\?['"])"""

# (kind, pattern). Order matters: specific formats before the generic catch-alls. A pattern's `keep` group (a
# header's or a variable's name) is context, not a secret: it stays, and only the rest of the match is replaced.
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
    ("slack_webhook", re.compile(r"\bhooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{16,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b")),
    # A JWT's header never reads past a "-eyJ": that is where the next attempt starts (a run of "eyJ-eyJ-..." would
    # otherwise be read again from each one).
    ("jwt", re.compile(r"\beyJ(?:[A-Za-z0-9_]|-(?!eyJ)){10,}+\.eyJ[A-Za-z0-9_-]{10,}+\.[A-Za-z0-9_-]{10,}")),
    # A key block runs to its END line, or to the next key's BEGIN line when it was cut short (a key pasted twice,
    # the first copy truncated): it never reads past another key's BEGIN.
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|"
                               r"(?=-----BEGIN [A-Z ]*PRIVATE KEY-----))", re.S)),
    # user:password@host in any URL scheme (postgres://, mongodb+srv://, https://...); the password runs to the
    # last @ before the host, as it may hold one.
    ("url_credentials", re.compile(r"(?<=://)(?!\[REDACTED)[^\s:/@]+:[^\s/]+(?=@)")),
    # An HTTP Authorization header's credentials (curl -H, a request in code or a log): Bearer, Basic, token.
    ("authorization_header", re.compile(
        r"(?i)(?P<keep>\b(?:proxy-)?authorization" + _JOIN + r"(?:bearer|basic|token|bot)\s++)"
        r"(?!\[REDACTED)[A-Za-z0-9._~+/-]{8,}=*")),
    # A value after a name that says it is a secret: KEY=value, key: value, a header (x-api-key: value), JSON's
    # "key": "value" and a call's ("key", "value"). The match starts at that word: what comes before it in the name
    # stays as it is, and at most 64 letters, digits or underscores of the name follow it (unbounded, every word in
    # a long run of names would rescan the run). A name ends at a hyphen after its word, and AUTH is not "author",
    # so headers such as Co-Authored-By:, Authentication-Results: or token-endpoint: hold no secret.
    ("assigned_secret", re.compile(
        r"(?i)(?P<keep>(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|ACCOUNT[_-]?KEY|"
        r"AUTH(?!OR(?!I[SZ])))[A-Z0-9_]{0,64}+" + _JOIN + r")(?!\[REDACTED)[^\s'\",;]{8,}")),
]
# scheme://user:password@ or scheme://token@ at the start of a URL: everything before the last @ that comes before
# the host ends (a password may hold an @).
USERINFO = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/?#\s]*@", re.I)


def url_without_credentials(url: str) -> str:
    """A URL without the user name and password tools embed in it (`https://user:token@host/...`, as a git remote
    often is), and with anything else that looks like a credential redacted."""
    return redact(USERINFO.sub(r"\1", url.strip()))[0] or ""


def redact(text: str | None) -> tuple[str | None, dict[str, int]]:
    """Returns (redacted text, {kind: count})."""
    out, counts, _ = redact_moving(text, [])
    return out, counts


def redact_moving(text: str | None, positions: list[int]) -> tuple[str | None, dict[str, int], list[int]]:
    """redact(), and where each of `positions` (offsets into `text`, such as a parser's page boundaries) lands in
    the redacted text. A position inside a replaced secret moves into its replacement."""
    if not text:
        return text, {}, list(positions)
    counts: dict[str, int] = {}
    positions = list(positions)
    for kind, pat in PATTERNS:
        pieces, edits, last = [], [], 0  # edits: (start, end, replacement length) in this pass's input
        for m in pat.finditer(text):
            counts[kind] = counts.get(kind, 0) + 1
            new = f"{m.groupdict().get('keep') or ''}[REDACTED:{kind}]"
            pieces += [text[last:m.start()], new]
            edits.append((m.start(), m.end(), len(new)))
            last = m.end()
        if edits:
            text = "".join(pieces) + text[last:]
            positions = _moved(positions, edits)
    return text, counts, positions


def _moved(positions: list[int], edits: list[tuple[int, int, int]]) -> list[int]:
    """Where each of `positions` lands once `edits` (in order, as finditer found them) are made: one pass over both,
    in order of position."""
    out, shift, e = list(positions), 0, 0
    for i in sorted(range(len(positions)), key=positions.__getitem__):
        pos = positions[i]
        while e < len(edits) and edits[e][1] <= pos:
            start, end, size = edits[e]
            shift += size - (end - start)
            e += 1
        start, _, size = edits[e] if e < len(edits) else (pos, pos, 0)
        out[i] = start + shift + min(pos - start, size) if start < pos else pos + shift
    return out
