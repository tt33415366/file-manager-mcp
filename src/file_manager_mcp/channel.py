"""File Channel: HTTP routes for bulk bytes and the prepare_* tools that link to them (spec sections 5.2, 6)."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import weakref
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode

import anyio
from mcp.server.mcpserver import Context
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .addressing import Addressing
from .auth import UploadRules, caller_name, caller_of
from .errors import FileManagerError, from_os_error
from .files import is_link, iso_time
from .jobs import STREAMS, JobRegistry
from .paths import PathPolicy
from .signed import DOWNLOAD, UPLOAD, LinkSigner

MAX_CHUNK_BYTES = 64 * 1024 * 1024
PARTIAL_SUFFIX = ".fmcp-partial"
DEFAULT_LINK_SECONDS = 3600
MIN_LINK_SECONDS = 60
MAX_LINK_SECONDS = 86400
MAX_CHUNKS = 32

_STATUS = {
    "INVALID_PATH": 400,
    "INVALID_ARGUMENT": 400,
    "NOT_A_FILE": 400,
    "NOT_A_DIRECTORY": 400,
    "OUTSIDE_ROOTS": 403,
    "NO_ALLOWED_ROOTS": 403,
    "INVALID_LINK": 403,
    "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404,
    "ALREADY_EXISTS": 409,
    "OFFSET_MISMATCH": 409,
    "SIZE_MISMATCH": 409,
    "LOG_RECLAIMED": 410,
    "JOB_NOT_FOUND": 404,
    "LOG_PROTECTION_LIMIT": 429,
    "TOO_LARGE": 413,
    "DIGEST_MISMATCH": 422,
    "IO_ERROR": 500,
}

_DIRECTIONS = {
    ("GET", "/files/download"): DOWNLOAD,
    ("HEAD", "/files/download"): DOWNLOAD,
    ("GET", "/files/upload"): UPLOAD,
    ("PUT", "/files/upload"): UPLOAD,
    ("DELETE", "/files/upload"): UPLOAD,
    ("POST", "/files/upload/complete"): UPLOAD,
}


def _error(exc: FileManagerError, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message, **extra}, status_code=_STATUS.get(exc.code, 400))


def _partial(real: str) -> str:
    partial = real + PARTIAL_SUFFIX
    if os.path.lexists(partial) and (is_link(partial) or not os.path.isfile(partial)):
        raise FileManagerError("NOT_A_FILE", f"{partial!r} is not a regular file")
    return partial


def _os_errors_as_json(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    @functools.wraps(handler)
    async def wrapper(request: Request) -> Response:
        try:
            return await handler(request)
        except OSError as exc:
            return _error(from_os_error(exc))

    return wrapper


def _upload_rules(request: Request) -> UploadRules:
    caller = caller_of(request)
    return caller.upload_rules if caller is not None else UploadRules(None, None)


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0


def _query_size(request: Request) -> int | None:
    raw = request.query_params.get("size")
    if raw is None:
        return None
    if not (raw.isascii() and raw.isdigit()):
        raise FileManagerError("INVALID_ARGUMENT", "query parameter 'size' must be a non-negative integer")
    return int(raw)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _check_expires_in(expires_in: int) -> None:
    if not MIN_LINK_SECONDS <= expires_in <= MAX_LINK_SECONDS:
        raise FileManagerError(
            "INVALID_ARGUMENT", f"expires_in must be between {MIN_LINK_SECONDS} and {MAX_LINK_SECONDS} seconds"
        )


_caller = caller_name


class _CountedStream(StreamingResponse):
    """A response that keeps its Job's log from being reclaimed for as long as it is being sent."""

    def __init__(self, job: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._job = job

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self._job.downloads += 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._job.downloads -= 1


def _chunk_curl(auth: str, url: str, offset: int, length: int) -> str:
    """Upload bytes ``offset`` to ``offset + length`` of $LOCAL_FILE as one chunk.

    ``head`` closing the pipe can SIGPIPE ``tail``, so pipefail is switched off inside a subshell, after probing
    in another one whether the shell has it (``set`` is a special builtin: an error may end a non-interactive shell).
    Success is curl's exit status plus the returned offset, not the pipeline's status.
    """
    return (
        "( if (set +o pipefail) >/dev/null 2>&1; then set +o pipefail; fi; "
        f'tail -c +{offset + 1} "$LOCAL_FILE" | head -c {length} | '
        f'curl -fS -X PUT {auth}--data-binary @- "{url}" )'
    )


def _url(base: str, route: str, real: str, signed: dict[str, str] | None = None) -> str:
    return f"{base}{route}?{urlencode({'path': real, **(signed or {})}, quote_via=quote)}"


class FileChannel:
    """The File Channel's routes, and the prepare_download / prepare_upload tools that link to them.

    Links for the Remote Agent follow the Host it called, so they work over whichever Connection Path it
    came; links for a DUT are Signed Links on the address the DUT can reach.
    """

    def __init__(
        self,
        policy: PathPolicy,
        addressing: Addressing,
        signer: LinkSigner,
        max_chunk_bytes: int = MAX_CHUNK_BYTES,
        jobs: JobRegistry | None = None,
    ) -> None:
        self.policy = policy
        self.addressing = addressing
        self.signer = signer
        self.max_chunk_bytes = max_chunk_bytes
        self.jobs = jobs
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.routes = [
            Route("/files/download", _os_errors_as_json(self._download), methods=["GET"]),
            Route("/files/upload", _os_errors_as_json(self._upload_status), methods=["GET"]),
            Route("/files/upload", _os_errors_as_json(self._upload_chunk), methods=["PUT"]),
            Route("/files/upload", _os_errors_as_json(self._upload_discard), methods=["DELETE"]),
            Route("/files/upload/complete", _os_errors_as_json(self._upload_complete), methods=["POST"]),
            Route("/files/job-log", _os_errors_as_json(self._job_log), methods=["GET"]),
        ]

    @staticmethod
    def direction_of(method: str, route: str) -> str | None:
        """The Signed Link direction a File Channel request needs; None outside the File Channel."""
        return _DIRECTIONS.get((method, route))

    # routes

    def _query_path(self, request: Request) -> str:
        path = request.query_params.get("path")
        if not path:
            raise FileManagerError("INVALID_ARGUMENT", "query parameter 'path' is required")
        caller = caller_of(request)
        if caller is not None and not caller.permits(self.direction_of(request.method, request.scope["path"]), path):
            raise FileManagerError("INVALID_LINK", f"this Signed Link does not cover {path!r}")
        return path

    def _partial_lock(self, partial: str) -> asyncio.Lock:
        """Requests on one partial run one at a time, so a chunk's offset check holds until its write ends."""
        return self._locks.setdefault(os.path.normcase(partial), asyncio.Lock())

    async def _download(self, request: Request) -> Response:
        try:
            real = self.policy.existing_file(self._query_path(request))
        except FileManagerError as exc:
            return _error(exc)
        return FileResponse(real, media_type="application/octet-stream", filename=os.path.basename(real))

    async def _upload_status(self, request: Request) -> Response:
        try:
            partial = _partial(self.policy.file_target(self._query_path(request)))
        except FileManagerError as exc:
            return _error(exc)
        return JSONResponse({"offset": _size(partial)})

    async def _upload_chunk(self, request: Request) -> Response:
        try:
            real = self.policy.file_target(self._query_path(request))
            raw_offset = request.query_params.get("offset", "")
            if not (raw_offset.isascii() and raw_offset.isdigit()):
                raise FileManagerError("INVALID_ARGUMENT", "query parameter 'offset' must be a non-negative integer")
            offset = int(raw_offset)
            partial = _partial(real)
        except FileManagerError as exc:
            return _error(exc)
        async with self._partial_lock(partial):
            return await self._write_chunk(request, partial, offset)

    async def _write_chunk(self, request: Request, partial: str, offset: int) -> Response:
        try:
            current = _size(partial)
            if offset not in (0, current):
                return _error(
                    FileManagerError("OFFSET_MISMATCH", f"partial upload is at offset {current}"),
                    offset=current,
                    expected_offset=current,
                )
            limit = self.max_chunk_bytes
            too_large = FileManagerError("TOO_LARGE", f"chunk exceeds {limit} bytes")
            max_bytes = _upload_rules(request).max_bytes
            if max_bytes is not None and max_bytes - offset < limit:
                limit = max_bytes - offset
                too_large = FileManagerError("TOO_LARGE", f"upload would exceed max_bytes={max_bytes}")
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise too_large
        except FileManagerError as exc:
            return _error(exc)

        f = await anyio.to_thread.run_sync(open, partial, "wb" if offset == 0 else "r+b")
        written = 0
        try:
            f.seek(offset)
            async for chunk in request.stream():
                written += len(chunk)
                if written > limit:
                    f.truncate(offset)
                    return _error(too_large)
                await anyio.to_thread.run_sync(f.write, chunk)
            f.truncate(offset + written)
        finally:
            f.close()
        return JSONResponse({"offset": offset + written})

    async def _upload_discard(self, request: Request) -> Response:
        try:
            partial = _partial(self.policy.file_target(self._query_path(request)))
        except FileManagerError as exc:
            return _error(exc)
        async with self._partial_lock(partial):
            existed = os.path.exists(partial)
            if existed:
                os.remove(partial)
        return JSONResponse({"deleted": existed})

    async def _upload_complete(self, request: Request) -> Response:
        try:
            path = self._query_path(request)
            real = self.policy.file_target(path)
            partial = _partial(real)
            async with self._partial_lock(partial):
                if not os.path.exists(partial):
                    raise FileManagerError("NOT_FOUND", f"no partial upload for {path!r}")
                rules = _upload_rules(request)
                expected_size = rules.size if rules.size is not None else _query_size(request)
                expected = rules.sha256 or request.query_params.get("sha256")
                partial_size = _size(partial)
                if expected_size is not None and partial_size != expected_size:
                    too_short = partial_size < expected_size
                    return _error(
                        FileManagerError(
                            "SIZE_MISMATCH",
                            f"partial upload is {partial_size} bytes, expected {expected_size}"
                            + ("; continue from offset" if too_short else "; reset it and upload again"),
                        ),
                        size=partial_size,
                        expected_size=expected_size,
                        offset=partial_size,
                        reset_required=not too_short,
                        reset=self._reset(request),
                    )
                digest = await anyio.to_thread.run_sync(_sha256, partial)
                if expected and expected.lower() != digest:
                    return _error(
                        FileManagerError("DIGEST_MISMATCH", f"sha256 is {digest}, expected {expected}"),
                        size=partial_size,
                        reset_required=expected_size is not None,
                        reset=self._reset(request),
                    )
                overwrite = rules.overwrite
                if overwrite is None:
                    overwrite = request.query_params.get("overwrite", "false").lower() == "true"
                if os.path.exists(real) and not overwrite:
                    raise FileManagerError("ALREADY_EXISTS", f"{path!r} already exists; pass overwrite=true")
                try:
                    size = os.path.getsize(partial)
                    if rules.max_bytes is not None and size > rules.max_bytes:
                        raise FileManagerError("TOO_LARGE", f"upload exceeds max_bytes={rules.max_bytes}")
                    os.replace(partial, real)
                except FileNotFoundError:
                    raise FileManagerError("NOT_FOUND", f"no partial upload for {path!r}") from None
        except FileManagerError as exc:
            return _error(exc)
        caller = caller_of(request)
        if caller is not None and caller.link is not None:
            self.signer.consume(caller.link)
        return JSONResponse({"path": path, "size": size, "sha256": digest})

    async def _job_log(self, request: Request) -> Response:
        """Serve the stored bytes of one Job stream, located by job id and stream name, never by path."""
        try:
            caller = caller_of(request)
            if caller is not None and caller.link is not None:
                raise FileManagerError("INVALID_LINK", "Job Logs are not available through Signed Links")
            if self.jobs is None:
                raise FileManagerError("JOB_NOT_FOUND", "this server has no jobs")
            job = self.jobs.get(request.query_params.get("job", ""))
            stream = request.query_params.get("stream", "")
            if stream not in STREAMS:
                raise FileManagerError("INVALID_ARGUMENT", "query parameter 'stream' must be stdout or stderr")
            if job.log_reclaimed:
                raise FileManagerError("LOG_RECLAIMED", "the job's log was reclaimed to free disk space")
        except FileManagerError as exc:
            return _error(exc)
        log = job.stream(stream)
        size, path = log.retained, log.path

        async def body():
            if not size:
                return
            remaining = size
            with open(path, "rb") as f:
                while remaining:
                    chunk = await anyio.to_thread.run_sync(f.read, min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return _CountedStream(
            job,
            body(),
            media_type="application/octet-stream",
            headers={"Content-Length": str(size), "Content-Disposition": f'attachment; filename="{stream}.log"'},
        )

    def _reset(self, request: Request) -> dict[str, str]:
        """The DELETE that discards the partial, with the same credentials as ``request``."""
        params = [(k, v) for k, v in request.query_params.multi_items() if k != "offset"]
        base = self.addressing.link_base(request.headers.get("host"))
        return {"method": "DELETE", "url": f"{base}/files/upload?{urlencode(params, quote_via=quote)}"}

    # tools

    def _base(self, ctx: Context | None) -> str:
        return self.addressing.link_base((ctx.headers or {}).get("host") if ctx is not None else None)

    def prepare_download(
        self, path: str, for_host: str = "", expires_in: int = DEFAULT_LINK_SECONDS, ctx: Context | None = None
    ) -> dict[str, Any]:
        _check_expires_in(expires_in)
        real = self.policy.existing_file(path)
        st = os.stat(real)
        name = "'" + os.path.basename(real).replace("'", "'\\''") + "'"
        if not for_host:
            url = _url(self._base(ctx), "/files/download", real)
            return {
                "url": url,
                "size": st.st_size,
                "modified": iso_time(st.st_mtime),
                "curl": f'curl -fSL -H "Authorization: Bearer $FILE_MANAGER_TOKEN" -o {name} "{url}"',
            }
        signed = self.signer.sign(DOWNLOAD, real, _caller(ctx), expires_in)
        url = _url(self.addressing.dut_base(for_host), "/files/download", real, signed)
        return {
            "url": url,
            "size": st.st_size,
            "modified": iso_time(st.st_mtime),
            "expires": iso_time(int(signed["exp"])),
            "curl": f'curl -fSL -o {name} "{url}"',
        }

    def prepare_job_log(
        self,
        job_id: str,
        stream: str = "stdout",
        expires_in: int = DEFAULT_LINK_SECONDS,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        _check_expires_in(expires_in)
        if self.jobs is None:
            raise FileManagerError("JOB_NOT_FOUND", "this server has no jobs")
        if stream not in STREAMS:
            raise FileManagerError("INVALID_ARGUMENT", "stream must be stdout or stderr")
        job = self.jobs.get(job_id)
        self.jobs.protect(job, self.jobs.wall() + expires_in)
        url = f"{self._base(ctx)}/files/job-log?{urlencode({'job': job.id, 'stream': stream})}"
        return {
            "url": url,
            "size": job.stream(stream).retained,
            "expires": iso_time(int(job.protect_until)),
            "log_truncated": job.stream(stream).truncated,
            "curl": f'curl -fSL -H "Authorization: Bearer $FILE_MANAGER_TOKEN" -o {stream}.log "{url}"',
        }

    def prepare_upload(
        self,
        path: str,
        for_host: str = "",
        expires_in: int = DEFAULT_LINK_SECONDS,
        overwrite: bool = False,
        max_bytes: int | None = None,
        size: int | None = None,
        sha256: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        _check_expires_in(expires_in)
        if max_bytes is not None and (not for_host or max_bytes < 1):
            raise FileManagerError("INVALID_ARGUMENT", "max_bytes must be >= 1 and needs for_host")
        if size is not None and (isinstance(size, bool) or size < 0):
            raise FileManagerError("INVALID_ARGUMENT", "size must be >= 0")
        if sha256 is not None:
            sha256 = sha256.lower()
            if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
                raise FileManagerError("INVALID_ARGUMENT", "sha256 must be 64 hexadecimal characters")
        if size is not None and max_bytes is not None and size > max_bytes:
            raise FileManagerError("INVALID_ARGUMENT", f"size {size} exceeds max_bytes {max_bytes}")
        real = self.policy.file_target(path)
        offset = _size(_partial(real))
        if size is not None and size < offset:
            raise FileManagerError(
                "INVALID_ARGUMENT",
                f"a partial upload of {offset} bytes already exceeds size {size}; "
                "discard it with DELETE on the upload URL first",
            )
        result: dict[str, Any] = {"offset": offset, "max_chunk_bytes": self.max_chunk_bytes}
        if not for_host:
            base = self._base(ctx)
            upload_url = _url(base, "/files/upload", real)
            auth = '-H "Authorization: Bearer $FILE_MANAGER_TOKEN" '
            expected = {"size": size, "sha256": sha256}
            complete_url = _url(base, "/files/upload/complete", real) + f"&overwrite={str(overwrite).lower()}"
            complete_url += "".join(f"&{k}={v}" for k, v in expected.items() if v is not None)
        else:
            signed = self.signer.sign(UPLOAD, real, _caller(ctx), expires_in, overwrite, max_bytes, size, sha256)
            base = self.addressing.dut_base(for_host)
            upload_url = _url(base, "/files/upload", real, signed)
            complete_url = _url(base, "/files/upload/complete", real, signed)
            auth = ""
            result["expires"] = iso_time(int(signed["exp"]))

        def put(at: int, length: int) -> dict[str, Any]:
            url = f"{upload_url}&offset={at}"
            return {
                "method": "PUT",
                "url": url,
                "offset": at,
                "length": length,
                "expect_offset": at + length,
                "curl": _chunk_curl(auth, url, at, length),
            }

        end = offset + self.max_chunk_bytes if size is None else size
        first = put(offset, min(self.max_chunk_bytes, end - offset))
        result["upload_url"] = first["url"]
        result["curl"] = first["curl"]
        result["complete_url"] = complete_url
        result["complete"] = {
            "method": "POST",
            "url": complete_url,
            "curl": f'curl -fS -X POST {auth}"{complete_url}"',
        }
        if size is not None:
            starts = range(offset, size, self.max_chunk_bytes)
            result["chunks"] = [put(at, min(self.max_chunk_bytes, size - at)) for at in starts[:MAX_CHUNKS]]
            if size == 0:  # an empty file still needs one empty PUT to create its partial
                result["chunks"] = [put(0, 0)]
            if len(starts) > MAX_CHUNKS:
                result["next_offset"] = starts[MAX_CHUNKS]
        return result
