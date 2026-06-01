"""Edge security helpers: client IP resolution, an in-memory rate limiter,
same-origin (CSRF) checks, and response security headers.

Pure/standalone so the logic is unit-testable; main.py wires it into middleware
and the login route.
"""

import threading
import time
from collections import deque
from urllib.parse import urlparse


# ---- Client IP (behind Cloudflare) --------------------------------------

def client_ip(request) -> str:
    """Real client IP. Cloudflare sets CF-Connecting-IP; fall back to the first
    X-Forwarded-For hop, then the socket peer."""
    h = request.headers
    cf = h.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = h.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"


# ---- Rate limiter (sliding window, in-memory per process) ---------------

class RateLimiter:
    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def _prune(self, key, window, now):
        q = self._d.get(key)
        if q is not None:
            cutoff = now - window
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                self._d.pop(key, None)

    def hit(self, key, limit, window, now=None):
        """Count this request. Returns (allowed, retry_after_seconds)."""
        now = time.time() if now is None else now
        with self._lock:
            self._prune(key, window, now)
            q = self._d.setdefault(key, deque())
            if len(q) >= limit:
                retry = int(window - (now - q[0])) + 1
                return False, max(retry, 1)
            q.append(now)
            return True, 0

    def count(self, key, window, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._prune(key, window, now)
            return len(self._d.get(key, ()))

    def add(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._d.setdefault(key, deque()).append(now)

    def clear(self, key):
        with self._lock:
            self._d.pop(key, None)


# ---- Same-origin / CSRF -------------------------------------------------

def is_same_origin(request) -> bool:
    """Defense-in-depth CSRF check for cookie-authenticated state changes.

    The session cookie is SameSite=lax (the primary CSRF defense). This adds a
    header check: if Origin/Referer is present and its host differs from the
    request host → reject. If neither header is present we allow (some legitimate
    same-origin posts omit them; SameSite=lax already blocks the cross-site cookie)."""
    host = (request.headers.get("host") or "").lower()
    for hdr in ("origin", "referer"):
        val = request.headers.get(hdr)
        if val:
            netloc = urlparse(val).netloc.lower()
            return netloc == host
    return True  # neither header present → rely on SameSite=lax


# ---- Response security headers ------------------------------------------

# CSP allows inline styles/scripts because the app renders inline styles and a
# few inline handlers/scripts; everything else is locked to same-origin. wss:
# permits the live dashboard WebSocket; img https:/data: covers the logo + svg.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' wss: https:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": _CSP,
    "X-Permitted-Cross-Domain-Policies": "none",
}
