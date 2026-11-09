"""Security primitives shared by the Flask application.

Tokens are tied to the Flask signed session. Do not store credentials or
bearer tokens in cookies: Flask sessions are integrity-protected, not encrypted.
"""
import hmac
import secrets
from urllib.parse import unquote, urlsplit

from flask import abort, request, session


def csrf_token():
    """Return a per-session cryptographically random token for unsafe requests."""
    token = session.get('_csrf_token')
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def verify_csrf():
    """Reject unsafe requests without a token generated for this same session."""
    expected = session.get('_csrf_token')
    provided = request.form.get('csrf_token', '') or request.headers.get('X-CSRF-Token', '')
    if not (isinstance(expected, str) and isinstance(provided, str) and
            len(expected) >= 32 and hmac.compare_digest(expected, provided)):
        abort(403, description='Richiesta non autorizzata: token CSRF mancante o non valido.')


def allowed_domain(email, domains):
    """Validate a whole Microsoft account domain, not a suffix or substring."""
    if not isinstance(email, str) or email.count('@') != 1:
        return False
    local, domain = email.strip().casefold().split('@', 1)
    allowed = {part.strip().casefold().lstrip('@') for part in domains.split(',') if part.strip()}
    return bool(local and domain and domain in allowed)


def safe_local_path(value):
    """Accept only absolute local paths, never a scheme-relative or JS redirect.

    The second decode detects encoded scheme-relative URLs such as %2F%2Fevil.
    Do not accept backslashes because browsers interpret them as separators.
    """
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith('/'):
        return None
    decoded = unquote(value)
    if (decoded.startswith('//') or '\\' in decoded or
            any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
        return None
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc:
        return None
    return value
