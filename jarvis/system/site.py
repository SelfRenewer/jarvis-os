"""The Jarvis OS page's access key (desktop/system-site/auth.mjs derives the very same one).

The page's server holds the Mac app's token, so it must not be an open door: it lets a browser in only after the
personal link `http://127.0.0.1:<port>/?key=<site key>` was opened once (then a 30-day HttpOnly session cookie).
The key is HMAC-SHA256(desktop token, "jarvis-system-site") — derived, nothing new to keep; printing it is fine (the
user needs the link), printing the token is not. `?key=` and not `?k=`: LocalSites takes `k` for its own LAN key.
"""

from __future__ import annotations

import hashlib
import hmac

SITE_KEY_LABEL = b"jarvis-system-site"
SITE_PORT = 39110  # LocalSites targetPort of the site (desktop/system-site/server.mjs default)
KEY_PARAM = "key"


def site_key(token: str) -> str:
    """32 hex characters: HMAC-SHA256(token, "jarvis-system-site")."""
    return hmac.new(token.encode(), SITE_KEY_LABEL, hashlib.sha256).hexdigest()[:32]


def site_link(token: str, port: int = SITE_PORT, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}/?{KEY_PARAM}={site_key(token)}"
