"""Path validation and Allowed Root confinement (spec section 4)."""

from __future__ import annotations

import os

from .errors import FileManagerError

_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def _invalid(path: str, why: str) -> FileManagerError:
    return FileManagerError("INVALID_PATH", f"{path!r}: {why}")


def no_allowed_roots() -> FileManagerError:
    return FileManagerError(
        "NO_ALLOWED_ROOTS", "No Allowed Roots configured yet; ask the operator to add one in the config UI."
    )


def _validate_syntax(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise _invalid(str(path), "path must be a non-empty string")
    if "\0" in path:
        raise _invalid(path, "path contains a NUL byte")
    native = path.replace("/", os.sep)
    if os.name == "nt":
        if native.startswith("\\\\?\\") or native.startswith("\\\\.\\"):
            raise _invalid(path, "device namespace paths are not allowed")
        drive, rest = os.path.splitdrive(native)
        if ":" in rest:
            raise _invalid(path, "alternate data streams are not allowed")
        for part in rest.split(os.sep):
            if part.split(".")[0].strip().upper() in _RESERVED:
                raise _invalid(path, f"reserved device name {part!r}")
            if part not in (".", "..") and part.endswith((".", " ")):
                raise _invalid(path, f"component {part!r} ends with a dot or space, which Windows strips")
    if not os.path.isabs(native):
        raise _invalid(path, "path must be absolute")
    return native


class Roots:
    """An immutable set of Allowed Roots; an operation making several checks uses one instance."""

    __slots__ = ("roots", "_norm_roots")

    def __init__(self, roots: tuple[str, ...]) -> None:
        self.roots = roots
        self._norm_roots = tuple(os.path.normcase(r) for r in roots)

    def resolve(self, path: str, follow: bool = True) -> str:
        """Return the real path, or raise if it is invalid or outside every Allowed Root.

        With ``follow=False`` the final component is not dereferenced, so a link resolves
        to the link entry itself (used when deleting or moving the link).
        """
        if not self.roots:
            raise no_allowed_roots()
        native = os.path.normpath(_validate_syntax(path))
        if follow:
            real = os.path.realpath(native)
        else:
            parent, name = os.path.split(native)
            real = os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(native)
        norm = os.path.normcase(real)
        for root in self._norm_roots:
            try:
                if os.path.commonpath([root, norm]) == root:
                    return real
            except ValueError:
                continue
        raise FileManagerError("OUTSIDE_ROOTS", f"{path!r} is outside the Allowed Roots")

    def existing(self, path: str, follow: bool = True) -> str:
        """The real path of something that exists, of any type."""
        real = self.resolve(path, follow)
        if not os.path.lexists(real):
            raise FileManagerError("NOT_FOUND", f"{path!r} does not exist")
        return real

    def existing_file(self, path: str) -> str:
        """The real path of an existing regular file, to read from."""
        real = self.existing(path)
        if not os.path.isfile(real):
            raise FileManagerError("NOT_A_FILE", f"{path!r} is not a file")
        return real

    def file_target(self, path: str) -> str:
        """The real path a file may be written to: not a directory, and its parent directory exists."""
        real = self.resolve(path)
        if os.path.isdir(real):
            raise FileManagerError("NOT_A_FILE", f"{path!r} is a directory")
        if not os.path.isdir(os.path.dirname(real)):
            raise FileManagerError("NOT_FOUND", f"parent directory of {path!r} does not exist")
        return real

    def is_root(self, real_path: str) -> bool:
        return os.path.normcase(real_path) in self._norm_roots


class PathPolicy:
    def __init__(self, roots: list[str]) -> None:
        self.set_roots(roots)

    def set_roots(self, roots: list[str]) -> None:
        self._snapshot = Roots(tuple(os.path.realpath(r) for r in roots))

    def snapshot(self) -> Roots:
        return self._snapshot

    @property
    def roots(self) -> tuple[str, ...]:
        return self._snapshot.roots

    def resolve(self, path: str, follow: bool = True) -> str:
        return self._snapshot.resolve(path, follow)

    def existing(self, path: str, follow: bool = True) -> str:
        return self._snapshot.existing(path, follow)

    def existing_file(self, path: str) -> str:
        return self._snapshot.existing_file(path)

    def file_target(self, path: str) -> str:
        return self._snapshot.file_target(path)

    def is_root(self, real_path: str) -> bool:
        return self._snapshot.is_root(real_path)
