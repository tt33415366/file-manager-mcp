"""Domain errors carrying the stable codes from spec section 5."""


class FileManagerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_OS_ERROR_CODES = (
    (FileNotFoundError, "NOT_FOUND"),
    (FileExistsError, "ALREADY_EXISTS"),
    (IsADirectoryError, "NOT_A_FILE"),
    (NotADirectoryError, "NOT_A_DIRECTORY"),
    (PermissionError, "PERMISSION_DENIED"),
)


def from_os_error(exc: OSError) -> FileManagerError:
    for cls, code in _OS_ERROR_CODES:
        if isinstance(exc, cls):
            return FileManagerError(code, str(exc))
    return FileManagerError("IO_ERROR", str(exc))
