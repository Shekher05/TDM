"""Credential redaction for user-facing error messages and persisted state."""
import re
from urllib.parse import urlsplit, urlunsplit

# scheme://user:pass@rest - matched anywhere in the text, not just as a
# whitespace-separated token, so a URL glued to punctuation (a leading colon,
# parentheses from an exception's own formatting, ...) is still caught.
_CREDENTIALED_URL = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)"
    r"(?P<user>[^\s:@/]+):(?P<password>[^\s@/]+)@"
    r"(?P<rest>\S*)"
)


def redact(text: str) -> str:
    """Rewrite every ``scheme://user:pass@host`` URL in ``text`` as
    ``scheme://user:***@host`` so an error message never leaks credentials."""
    return _CREDENTIALED_URL.sub(
        lambda m: f"{m['scheme']}{m['user']}:***@{m['rest']}", text
    )


def strip_credentials(url: str) -> str:
    """Return ``url`` with any ``user:pass@`` userinfo removed entirely (not
    masked) - for state that gets persisted to disk, where even a masked
    password shouldn't linger."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)
