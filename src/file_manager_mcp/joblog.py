"""Job Log: a Job's output streams, bounded on disk and readable by Output Cursor (spec section 7)."""

from __future__ import annotations

import codecs
import os
from collections.abc import Callable
from dataclasses import dataclass

from .errors import FileManagerError

RESULT_CAP_BYTES = 32 * 1024
BUFFER_LIMIT_BYTES = 1024 * 1024
STREAM_LOG_LIMIT = 256 * 1024 * 1024


def cap_text(data: bytes, dropped: int, cap: int = RESULT_CAP_BYTES) -> tuple[str, bool]:
    """Decode ``data``, keeping its head and tail around a truncation marker when over ``cap``.

    ``dropped`` counts bytes already discarded from the middle of ``data`` by an OutputBuffer.
    """
    if len(data) <= cap and not dropped:
        return data.decode("utf-8", errors="replace"), False
    if len(data) > cap:
        half = cap // 2
        head, tail = data[:half], data[len(data) - half :]
        removed = len(data) - 2 * half + dropped
    else:
        mid = len(data) // 2
        head, tail = data[:mid], data[mid:]
        removed = dropped
    marker = f"\n...[{removed} bytes truncated]...\n"
    return head.decode("utf-8", errors="replace") + marker + tail.decode("utf-8", errors="replace"), True


class OutputBuffer:
    """Keeps the first and last ``limit // 2`` bytes of a stream and counts what was dropped."""

    def __init__(self, limit: int = BUFFER_LIMIT_BYTES) -> None:
        self.half = limit // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def append(self, chunk: bytes) -> None:
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail += chunk
        excess = len(self.tail) - self.half
        if excess > 0:
            del self.tail[:excess]
            self.dropped += excess

    def snapshot(self) -> tuple[bytes, int]:
        return bytes(self.head + self.tail), self.dropped


@dataclass(frozen=True)
class StreamRead:
    text: str
    next: int
    lost: int
    more: bool


class StreamLog:
    """One output stream of a Job.

    Bytes go to a file (at most ``STREAM_LOG_LIMIT``, and only as much as ``reserve`` allows) and to an
    in-memory head/tail buffer. Past the limit the stream is still drained and counted, never stored. A
    cursor read sees the file plus the buffer's head and tail; what neither holds is a gap, counted as lost.
    ``produced`` is None for a stream recovered after a crash, whose true length is unknown.
    """

    def __init__(
        self,
        path: str | None,
        reserve: Callable[[int], int] = lambda n: n,
        release: Callable[[int], None] = lambda n: None,
    ) -> None:
        self.path = path
        self.reserve = reserve
        self.release = release
        self.buffer = OutputBuffer()
        self.produced: int | None = 0
        self.retained = 0
        self.truncated = False
        self.closed = False
        self.reclaimed = False
        self._file = None
        self._loaded = True
        self._from_disk = False

    @classmethod
    def recovered(cls, path: str, produced: int | None, truncated: bool) -> StreamLog:
        log = cls(path)
        log.produced = produced
        log.truncated = truncated
        log.closed = True
        log._loaded = False
        log._from_disk = True
        try:
            log.retained = os.path.getsize(path)
        except OSError:
            log.retained = 0
        return log

    # writing

    def append(self, chunk: bytes) -> None:
        assert self.produced is not None
        self.produced += len(chunk)
        self.buffer.append(chunk)
        if self.path is None or self.truncated:  # once a byte is not stored, none after it is: the file stays a prefix
            return
        room = min(len(chunk), STREAM_LOG_LIMIT - self.retained)
        allowed = self.reserve(room) if room > 0 else 0
        if allowed < len(chunk):
            self.truncated = True
        if allowed <= 0:
            return
        try:
            if self._file is None:
                self._file = open(self.path, "ab")
            self._file.write(chunk[:allowed])
            self._file.flush()
        except OSError:
            self.release(allowed)
            self.truncated = True
            self._drop_file()
            return
        self.retained += allowed

    def _drop_file(self) -> None:
        """Stop storing (the file cannot be written); the stream keeps draining."""
        self.path = None if self.retained == 0 else self.path
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def close(self) -> None:
        self.closed = True
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def discard(self) -> int:
        """Delete the stored bytes (reclaim); returns how many were freed."""
        self.close()
        freed = self.retained
        if self.path is not None:
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.retained = 0
        self.reclaimed = True
        return freed

    # snapshot

    def snapshot(self) -> tuple[bytes, int]:
        if not self._loaded:
            self._load_buffer()
        data, dropped = self.buffer.snapshot()
        if self._from_disk and self.produced is not None and self.produced > self.retained:
            dropped += self.produced - self.retained
        return data, dropped

    def _load_buffer(self) -> None:
        self._loaded = True
        if self.path is None or not self.retained:
            return
        half = self.buffer.half
        try:
            with open(self.path, "rb") as f:
                if self.retained <= 2 * half:
                    self.buffer.append(f.read())
                    return
                self.buffer.head = bytearray(f.read(half))
                f.seek(self.retained - half)
                self.buffer.tail = bytearray(f.read(half))
                self.buffer.dropped = self.retained - 2 * half
        except OSError:
            pass

    @property
    def bound(self) -> int:
        """The end of what exists: the produced length, or the stored length when that is unknown."""
        return self.produced if self.produced is not None else self.retained

    # cursor reads

    def _covering(self, pos: int) -> tuple[int, str] | None:
        """The source that holds byte ``pos`` and reaches furthest: (end, name), or None in a gap."""
        best: tuple[int, str] | None = None
        if pos < self.retained:
            best = (self.retained, "disk")
        head = 0 if self._from_disk else len(self.buffer.head)
        if pos < head and (best is None or head > best[0]):
            best = (head, "head")
        if self.produced is not None and not self._from_disk:
            if self.produced - len(self.buffer.tail) <= pos < self.produced and (
                best is None or self.produced > best[0]
            ):
                best = (self.produced, "tail")
        return best

    def _fetch(self, source: str, pos: int, n: int) -> bytes:
        if source == "disk":
            with open(self.path, "rb") as f:
                f.seek(pos)
                return f.read(n)
        if source == "head":
            return bytes(self.buffer.head[pos : pos + n])
        start = self.produced - len(self.buffer.tail)
        return bytes(self.buffer.tail[pos - start : pos - start + n])

    def read(self, offset: int, limit: int = RESULT_CAP_BYTES) -> StreamRead:
        """Up to ``limit`` raw bytes from ``offset``, decoded as UTF-8 with replacement, in one consistent step.

        Never ends inside a character that more output could complete; only a boundary that will not grow
        (the end of a stored region before a gap, a finished stream's end) is decoded as it stands.
        """
        if self.reclaimed:
            raise FileManagerError("LOG_RECLAIMED", "the job's log was reclaimed to free disk space")
        bound = self.bound
        if offset < 0 or offset > bound:
            raise FileManagerError("INVALID_ARGUMENT", f"offset {offset} is outside the stream (end {bound})")
        out = bytearray()
        pos, lost = offset, 0
        while len(out) < limit:
            cov = self._covering(pos)
            if cov is None:
                if out or pos >= bound:
                    break
                resume = self._next_start(pos)
                lost += resume - pos
                pos = resume
                continue
            end, source = cov
            n = min(limit - len(out), end - pos)
            out += self._fetch(source, pos, n)
            pos += n
        at_end = pos >= bound
        final = (not at_end and self._covering(pos) is None) or (at_end and self.closed)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(bytes(out), final)
        pending = 0 if final else len(decoder.getstate()[0])
        nxt = pos - pending
        waiting = pending > 0 and at_end
        return StreamRead(text, nxt, lost, nxt < bound and not waiting)

    def _next_start(self, pos: int) -> int:
        """The first readable byte after the gap that ``pos`` is in (or the end of the stream)."""
        if self.produced is not None and not self._from_disk:
            tail_start = self.produced - len(self.buffer.tail)
            if tail_start > pos:
                return tail_start
        return self.bound
