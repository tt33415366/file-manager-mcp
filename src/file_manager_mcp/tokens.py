"""Bearer token store: names and SHA-256 digests in tokens.json (spec section 3)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

TOKEN_PREFIX = "fmcp_"


class TokenError(Exception):
    pass


@dataclass
class TokenEntry:
    name: str
    sha256: str
    created: str


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """Reloads tokens.json when another process (the ``token`` commands) changed it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._load()

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return st.st_mtime_ns, st.st_size

    def _load(self) -> None:
        stamp = self._stamp()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        self._entries = [TokenEntry(**e) for e in data.get("tokens", [])]
        self._loaded_stamp = stamp

    def _refresh(self) -> None:
        if self._stamp() != self._loaded_stamp:
            try:
                self._load()
            except ValueError:
                pass  # torn read of another process's write: keep the last good entries, retry next call

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tokens": [asdict(e) for e in self._entries]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._loaded_stamp = self._stamp()

    def list(self) -> list[TokenEntry]:
        self._refresh()
        return list(self._entries)

    def add(self, name: str) -> str:
        self._load()
        if any(e.name == name for e in self._entries):
            raise TokenError(f"token {name!r} already exists")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._entries.append(TokenEntry(name=name, sha256=_digest(token), created=created))
        self._save()
        return token

    def remove(self, name: str) -> None:
        self._load()
        remaining = [e for e in self._entries if e.name != name]
        if len(remaining) == len(self._entries):
            raise TokenError(f"no token named {name!r}")
        self._entries = remaining
        self._save()

    def verify(self, token: str) -> str | None:
        if not token:
            return None
        self._refresh()
        digest = _digest(token)
        match = None
        for entry in self._entries:
            if hmac.compare_digest(digest, entry.sha256):
                match = entry.name
        return match
