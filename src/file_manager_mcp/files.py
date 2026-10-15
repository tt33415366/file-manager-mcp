"""Synchronous file operations behind the MCP file tools (spec section 5.1)."""

from __future__ import annotations

import codecs
import fnmatch
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from typing import Any

from .errors import FileManagerError
from .paths import PathPolicy

MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
WRITE_MODES = ("create", "overwrite", "append")


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def is_link(path: str) -> bool:
    """True for symlinks and Windows junctions (any reparse point)."""
    if os.path.islink(path):
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name == "nt" else False


def _is_within(path: str, directory: str) -> bool:
    norm_path, norm_dir = os.path.normcase(path), os.path.normcase(directory)
    try:
        return os.path.commonpath([norm_path, norm_dir]) == norm_dir
    except ValueError:
        return False


def _type_of(path: str) -> str:
    if is_link(path):
        return "symlink"
    if os.path.isdir(path):
        return "dir"
    if os.path.isfile(path):
        return "file"
    return "other"


def _check_limit(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise FileManagerError("INVALID_ARGUMENT", f"{name} must be between {low} and {high}")


def _codec(encoding: str) -> codecs.CodecInfo:
    try:
        return codecs.lookup(encoding)
    except LookupError:
        raise FileManagerError("INVALID_ARGUMENT", f"unknown encoding {encoding!r}") from None


class FileOps:
    def __init__(self, policy: PathPolicy) -> None:
        self.policy = policy

    # resolution helpers

    def _dir(self, path: str) -> str:
        real = self.policy.existing(path)
        if not os.path.isdir(real):
            raise FileManagerError("NOT_A_DIRECTORY", f"{path!r} is not a directory")
        return real

    def _require_parent(self, real: str, path: str) -> None:
        if not os.path.isdir(os.path.dirname(real)):
            raise FileManagerError("NOT_FOUND", f"parent directory of {path!r} does not exist")

    # tools

    def list_roots(self) -> dict[str, Any]:
        return {"roots": list(self.policy.roots)}

    def list_dir(self, path: str, cursor: str | None = None, limit: int = 200) -> dict[str, Any]:
        _check_limit("limit", limit, 1, 1000)
        start = 0
        if cursor:
            if not (cursor.isascii() and cursor.isdigit()):
                raise FileManagerError("INVALID_ARGUMENT", "cursor must come from a previous next_cursor")
            start = int(cursor)
        real = self._dir(path)
        names = sorted(os.listdir(real))
        entries = []
        for name in names[start : start + limit]:
            full = os.path.join(real, name)
            st = os.lstat(full)
            kind = _type_of(full)
            entries.append(
                {"name": name, "type": kind, "size": st.st_size if kind == "file" else 0, "modified": iso_time(st.st_mtime)}
            )
        end = start + limit
        return {"entries": entries, "next_cursor": str(end) if end < len(names) else None}

    def stat(self, path: str) -> dict[str, Any]:
        real = self.policy.resolve(path)
        if not os.path.lexists(real):
            return {"path": path, "exists": False}
        st = os.lstat(real)
        kind = _type_of(real)
        return {
            "path": path,
            "exists": True,
            "type": kind,
            "size": st.st_size if kind == "file" else 0,
            "modified": iso_time(st.st_mtime),
        }

    def read_text(self, path: str, offset: int = 0, max_bytes: int = 32768, encoding: str = "utf-8") -> dict[str, Any]:
        _check_limit("max_bytes", max_bytes, 1, MAX_READ_BYTES)
        if offset < 0:
            raise FileManagerError("INVALID_ARGUMENT", "offset must be >= 0")
        decoder = _codec(encoding).incrementaldecoder("strict")
        real = self.policy.existing_file(path)
        total = os.path.getsize(real)
        with open(real, "rb") as f:
            f.seek(offset)
            chunk = f.read(max_bytes)
        at_end = offset + len(chunk) >= total
        try:
            content = decoder.decode(chunk, final=at_end)
        except UnicodeDecodeError:
            raise FileManagerError(
                "NOT_TEXT", f"{path!r} is not valid {encoding} text; use prepare_download for binary files"
            ) from None
        pending = len(decoder.getstate()[0])
        consumed = len(chunk) - pending
        if consumed == 0 and chunk:
            raise FileManagerError("INVALID_ARGUMENT", "max_bytes is smaller than one character")
        return {
            "content": content,
            "offset": offset,
            "bytes_read": consumed,
            "total_size": total,
            "eof": offset + consumed >= total,
        }

    def write_text(
        self,
        path: str,
        content: str,
        mode: str = "create",
        encoding: str = "utf-8",
        create_parents: bool = False,
    ) -> dict[str, Any]:
        if mode not in WRITE_MODES:
            raise FileManagerError("INVALID_ARGUMENT", f"mode must be one of {', '.join(WRITE_MODES)}")
        try:
            data = content.encode(_codec(encoding).name)
        except UnicodeEncodeError:
            raise FileManagerError("INVALID_ARGUMENT", f"content cannot be encoded as {encoding}") from None
        if len(data) > MAX_WRITE_BYTES:
            raise FileManagerError("TOO_LARGE", "content exceeds 1 MiB; use prepare_upload and the File Channel")
        if create_parents:
            os.makedirs(os.path.dirname(self.policy.resolve(path)), exist_ok=True)
        real = self.policy.file_target(path)

        if mode == "create":
            try:
                with open(real, "xb") as f:
                    f.write(data)
            except FileExistsError:
                raise FileManagerError("ALREADY_EXISTS", f"{path!r} already exists") from None
        elif mode == "append":
            with open(real, "ab") as f:
                f.write(data)
        else:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".fmcp-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, real)
            except BaseException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
        return {"path": path, "bytes_written": len(data)}

    def make_dir(self, path: str) -> dict[str, Any]:
        real = self.policy.resolve(path)
        if os.path.lexists(real) and not os.path.isdir(real):
            raise FileManagerError("NOT_A_DIRECTORY", f"{path!r} exists and is not a directory")
        os.makedirs(real, exist_ok=True)
        return {"path": path}

    def _transfer_target(self, source: str, destination: str, overwrite: bool) -> tuple[str, str]:
        roots = self.policy.snapshot()
        src = roots.existing(source, follow=False)
        if roots.is_root(src):
            raise FileManagerError("INVALID_ARGUMENT", "an Allowed Root cannot be moved or copied onto")
        dst = roots.resolve(destination, follow=False)
        if roots.is_root(dst):
            raise FileManagerError("INVALID_ARGUMENT", "an Allowed Root cannot be overwritten")
        self._require_parent(dst, destination)
        if os.path.isdir(src) and not is_link(src) and _is_within(dst, src):
            raise FileManagerError("INVALID_ARGUMENT", "a directory cannot be moved or copied into itself")
        if os.path.lexists(dst):
            if not overwrite:
                raise FileManagerError("ALREADY_EXISTS", f"{destination!r} already exists")
            if os.path.isdir(dst) or os.path.isdir(src):
                raise FileManagerError("INVALID_ARGUMENT", "overwrite only replaces files with files")
        return src, dst

    def move(self, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        src, dst = self._transfer_target(source, destination, overwrite)
        if os.path.lexists(dst):
            os.remove(dst)
        shutil.move(src, dst)
        return {"source": source, "destination": destination}

    def copy(self, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        src, dst = self._transfer_target(source, destination, overwrite)
        if os.path.isdir(src) and not is_link(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
        return {"source": source, "destination": destination}

    def delete(self, path: str, recursive: bool = False) -> dict[str, Any]:
        roots = self.policy.snapshot()
        real = roots.existing(path, follow=False)
        if roots.is_root(real):
            raise FileManagerError("INVALID_ARGUMENT", "an Allowed Root cannot be deleted")
        if is_link(real):
            try:
                os.unlink(real)
            except (IsADirectoryError, PermissionError):
                os.rmdir(real)
        elif os.path.isdir(real):
            if os.listdir(real) and not recursive:
                raise FileManagerError("DIRECTORY_NOT_EMPTY", f"{path!r} is not empty; pass recursive=true")
            shutil.rmtree(real)
        else:
            os.remove(real)
        return {"path": path}

    def search(
        self, path: str, name_glob: str = "*", content_regex: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        _check_limit("limit", limit, 1, 1000)
        pattern = None
        if content_regex is not None:
            try:
                pattern = re.compile(content_regex)
            except re.error as exc:
                raise FileManagerError("INVALID_ARGUMENT", f"invalid content_regex: {exc}") from None
        real = self._dir(path)
        matches: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(real):
            names = sorted(filenames) if pattern else sorted(dirnames + filenames)
            dirnames[:] = sorted(d for d in dirnames if not is_link(os.path.join(dirpath, d)))
            for name in names:
                if not fnmatch.fnmatch(name, name_glob):
                    continue
                full = os.path.join(dirpath, name)
                if pattern is None:
                    matches.append({"path": full})
                elif not is_link(full):
                    matches.extend(self._grep(full, pattern))
                if len(matches) >= limit:
                    return {"matches": matches[:limit], "truncated": True}
        return {"matches": matches, "truncated": False}

    @staticmethod
    def _grep(full: str, pattern: re.Pattern) -> list[dict[str, Any]]:
        try:
            if os.path.getsize(full) > MAX_SEARCH_FILE_BYTES:
                return []
            with open(full, "rb") as f:
                text = f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return [
            {"path": full, "line": i, "text": line.strip()[:200]}
            for i, line in enumerate(text.splitlines(), start=1)
            if pattern.search(line)
        ]
