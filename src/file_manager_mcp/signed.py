"""Signed Links: tokenless, expiring File Channel URLs that a Remote Agent obtains for a DUT (ADR 0003)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass

DOWNLOAD = "download"
UPLOAD = "upload"
SIGNED_FIELDS = ("path", "exp", "by", "overwrite", "max_bytes", "sig")


@dataclass(frozen=True)
class Grant:
    direction: str
    path: str
    by: str
    overwrite: bool
    max_bytes: int | None
    sig: str
    exp: int


class LinkSigner:
    """Signs with a per-launch random key, so a restart revokes every link.

    With ``issuers``, a link is only valid while the token name that issued it still exists.
    """

    def __init__(
        self, clock: Callable[[], float] = time.time, issuers: Callable[[], Collection[str]] | None = None
    ) -> None:
        self._key = secrets.token_bytes(32)
        self._clock = clock
        self._issuers = issuers
        self._consumed: dict[str, int] = {}

    def consume(self, grant: Grant) -> None:
        """Void a link (an upload after its complete); entries are dropped once they expire anyway."""
        now = self._clock()
        self._consumed = {sig: exp for sig, exp in self._consumed.items() if exp > now}
        self._consumed[grant.sig] = grant.exp

    def _sig(self, direction: str, path: str, exp: str, by: str, overwrite: str, max_bytes: str) -> str:
        message = json.dumps([direction, path, exp, by, overwrite, max_bytes]).encode("utf-8")
        digest = hmac.new(self._key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def sign(
        self,
        direction: str,
        path: str,
        by: str,
        expires_in: int,
        overwrite: bool = False,
        max_bytes: int | None = None,
    ) -> dict[str, str]:
        """Query parameters (besides ``path``) that make a link for ``direction`` on ``path``."""
        params = {"exp": str(int(self._clock()) + expires_in), "by": by}
        if direction == UPLOAD:
            params["overwrite"] = "true" if overwrite else "false"
            if max_bytes is not None:
                params["max_bytes"] = str(max_bytes)
        fields = (params.get("overwrite", ""), params.get("max_bytes", ""))
        params["sig"] = self._sig(direction, path, params["exp"], by, *fields)
        return params

    def verify(self, direction: str, query: Mapping[str, str] | Iterable[tuple[str, str]]) -> Grant | None:
        """The Grant a request's query carries for ``direction``, or None if it is not a live link for it."""
        pairs = list(query.items() if isinstance(query, Mapping) else query)
        names = [k for k, _ in pairs if k in SIGNED_FIELDS]
        if len(names) != len(set(names)):
            return None
        q = dict(pairs)
        path, exp, by, sig = q.get("path"), q.get("exp", ""), q.get("by"), q.get("sig", "")
        overwrite, max_bytes = q.get("overwrite", ""), q.get("max_bytes", "")
        if path is None or by is None or not exp.isdigit():
            return None
        expected = self._sig(direction, path, exp, by, overwrite, max_bytes)
        if not hmac.compare_digest(sig.encode("utf-8"), expected.encode("ascii")) or self._clock() >= int(exp) or sig in self._consumed:
            return None
        if self._issuers is not None and by not in self._issuers():
            return None
        return Grant(direction, path, by, overwrite == "true", int(max_bytes) if max_bytes else None, sig, int(exp))
