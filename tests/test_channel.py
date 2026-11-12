import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, quote, urlsplit

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from file_manager_mcp import channel
from file_manager_mcp.addressing import Addressing
from file_manager_mcp.channel import MAX_CHUNK_BYTES, FileChannel
from file_manager_mcp.config import ServerConfig
from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.jobs import JobRegistry
from file_manager_mcp.paths import PathPolicy
from file_manager_mcp.signed import DOWNLOAD, UPLOAD, LinkSigner


def file_channel(roots, interfaces=None, max_chunk_bytes=MAX_CHUNK_BYTES, **server):
    server = ServerConfig(**{"port": 8765, **server})
    addressing = Addressing(server, interfaces) if interfaces is not None else Addressing(server)
    return FileChannel(PathPolicy([str(r) for r in roots]), addressing, LinkSigner(), max_chunk_bytes)


@pytest.fixture
def client(root):
    return TestClient(Starlette(routes=file_channel([root], max_chunk_bytes=10).routes))


def q(path) -> str:
    return quote(str(path), safe="")


# download


def test_download_streams_file(client, root):
    (root / "a.bin").write_bytes(b"0123456789")
    r = client.get(f"/files/download?path={q(root / 'a.bin')}")
    assert r.status_code == 200
    assert r.content == b"0123456789"
    assert r.headers["content-type"] == "application/octet-stream"
    assert "a.bin" in r.headers["content-disposition"]
    assert "etag" in r.headers and "last-modified" in r.headers


def test_download_supports_range(client, root):
    (root / "a.bin").write_bytes(b"0123456789")
    r = client.get(f"/files/download?path={q(root / 'a.bin')}", headers={"Range": "bytes=2-4"})
    assert r.status_code == 206
    assert r.content == b"234"


def test_download_errors(client, root, tmp_path):
    (root / "d").mkdir()
    assert client.get(f"/files/download?path={q(root / 'missing')}").status_code == 404
    assert client.get(f"/files/download?path={q(root / 'd')}").status_code == 400
    r = client.get(f"/files/download?path={q(tmp_path / 'x')}")
    assert r.status_code == 403 and r.json()["error"] == "OUTSIDE_ROOTS"
    assert client.get("/files/download?path=relative").status_code == 400
    assert client.get("/files/download").status_code == 400


# upload


def test_chunked_upload_then_complete(client, root):
    target = q(root / "up.bin")
    assert client.get(f"/files/upload?path={target}").json() == {"offset": 0}

    assert client.put(f"/files/upload?path={target}&offset=0", content=b"hello ").json() == {"offset": 6}
    assert client.put(f"/files/upload?path={target}&offset=6", content=b"world").json() == {"offset": 11}
    assert client.get(f"/files/upload?path={target}").json() == {"offset": 11}

    digest = hashlib.sha256(b"hello world").hexdigest()
    r = client.post(f"/files/upload/complete?path={target}&sha256={digest}")

    assert r.status_code == 200
    assert r.json() == {"path": str(root / "up.bin"), "size": 11, "sha256": digest}
    assert (root / "up.bin").read_bytes() == b"hello world"
    assert not (root / "up.bin.fmcp-partial").exists()


def test_offset_mismatch_is_409_with_current_offset(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    r = client.put(f"/files/upload?path={target}&offset=1", content=b"x")
    assert r.status_code == 409
    assert r.json()["offset"] == 3


def test_offset_zero_restarts_partial(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abcdef")
    assert client.put(f"/files/upload?path={target}&offset=0", content=b"x").json() == {"offset": 1}


def test_chunk_too_large_is_413_and_partial_unchanged(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    r = client.put(f"/files/upload?path={target}&offset=3", content=b"x" * 11)
    assert r.status_code == 413
    assert client.get(f"/files/upload?path={target}").json() == {"offset": 3}


def test_complete_digest_mismatch_keeps_partial(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    r = client.post(f"/files/upload/complete?path={target}&sha256={'0' * 64}")
    assert r.status_code == 422
    assert (root / "up.bin.fmcp-partial").exists()


def test_complete_refuses_existing_target_without_overwrite(client, root):
    (root / "up.bin").write_bytes(b"old")
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"new")

    assert client.post(f"/files/upload/complete?path={target}").status_code == 409
    assert client.post(f"/files/upload/complete?path={target}&overwrite=true").status_code == 200
    assert (root / "up.bin").read_bytes() == b"new"


def test_complete_without_partial_is_404(client, root):
    assert client.post(f"/files/upload/complete?path={q(root / 'nope')}").status_code == 404


def test_complete_racing_another_complete_is_404(client, root, monkeypatch):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    real_sha256 = channel._sha256

    def moved_meanwhile(path):
        digest = real_sha256(path)
        os.replace(path, root / "up.bin")
        return digest

    monkeypatch.setattr(channel, "_sha256", moved_meanwhile)
    r = client.post(f"/files/upload/complete?path={target}&overwrite=true")
    assert (r.status_code, r.json()["error"]) == (404, "NOT_FOUND")


def test_upload_requires_existing_parent_and_rejects_outside(client, root, tmp_path):
    assert client.put(f"/files/upload?path={q(root / 'no' / 'f')}&offset=0", content=b"x").status_code == 404
    assert client.put(f"/files/upload?path={q(tmp_path / 'f')}&offset=0", content=b"x").status_code == 403


def test_routes_without_roots_are_403_no_allowed_roots(root):
    client = TestClient(Starlette(routes=file_channel([]).routes))
    target = q(root / "f")
    responses = [
        client.get(f"/files/download?path={target}"),
        client.get(f"/files/upload?path={target}"),
        client.put(f"/files/upload?path={target}&offset=0", content=b"x"),
        client.delete(f"/files/upload?path={target}"),
        client.post(f"/files/upload/complete?path={target}"),
    ]
    for r in responses:
        assert r.status_code == 403
        assert r.json()["error"] == "NO_ALLOWED_ROOTS"


def test_upload_bad_offset_is_400(client, root):
    assert client.put(f"/files/upload?path={q(root / 'f')}&offset=-1", content=b"x").status_code == 400
    assert client.put(f"/files/upload?path={q(root / 'f')}", content=b"x").status_code == 400


def test_delete_discards_partial(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    assert client.delete(f"/files/upload?path={target}").json() == {"deleted": True}
    assert client.delete(f"/files/upload?path={target}").json() == {"deleted": False}


def test_partial_that_is_not_a_regular_file_is_refused(client, root):
    (root / "up.bin.fmcp-partial").mkdir()
    target = q(root / "up.bin")
    responses = [
        client.get(f"/files/upload?path={target}"),
        client.put(f"/files/upload?path={target}&offset=0", content=b"x"),
        client.delete(f"/files/upload?path={target}"),
        client.post(f"/files/upload/complete?path={target}"),
    ]
    for r in responses:
        assert (r.status_code, r.json()["error"]) == (400, "NOT_A_FILE")
    assert (root / "up.bin.fmcp-partial").is_dir()


def test_partial_that_is_a_link_out_of_the_roots_is_refused(client, root, tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    try:
        (root / "up.bin.fmcp-partial").symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks needs privilege on this machine")
    target = q(root / "up.bin")

    r = client.put(f"/files/upload?path={target}&offset=0", content=b"x")
    assert (r.status_code, r.json()["error"]) == (400, "NOT_A_FILE")
    assert client.post(f"/files/upload/complete?path={target}").status_code == 400
    assert outside.read_bytes() == b"secret"


@pytest.mark.anyio
async def test_concurrent_chunks_at_the_same_offset_let_only_one_win(root):
    app = Starlette(routes=file_channel([root]).routes)
    url = f"/files/upload?path={q(root / 'up.bin')}"

    async def slowly(data: bytes):
        for i in range(len(data)):
            await anyio.sleep(0.02)
            yield data[i : i + 1]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await c.put(f"{url}&offset=0", content=b"abc")
        first, second = await asyncio.gather(
            c.put(f"{url}&offset=3", content=slowly(b"1111")),
            c.put(f"{url}&offset=3", content=slowly(b"2222")),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner = b"1111" if first.status_code == 200 else b"2222"
    assert (root / "up.bin.fmcp-partial").read_bytes() == b"abc" + winner


def test_os_error_on_a_route_is_a_json_error(client, root, monkeypatch):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")

    def denied(path):
        raise PermissionError(13, "Access is denied", path)

    monkeypatch.setattr(channel, "_sha256", denied)
    r = client.post(f"/files/upload/complete?path={target}")
    assert (r.status_code, r.json()["error"]) == (403, "PERMISSION_DENIED")


def test_offset_of_non_ascii_digits_is_400(client, root):
    r = client.put(f"/files/upload?path={q(root / 'f')}&offset=%C2%B2", content=b"x")
    assert (r.status_code, r.json()["error"]) == (400, "INVALID_ARGUMENT")


@pytest.mark.parametrize(
    "method, route, direction",
    [
        ("GET", "/files/download", DOWNLOAD),
        ("HEAD", "/files/download", DOWNLOAD),
        ("GET", "/files/upload", UPLOAD),
        ("PUT", "/files/upload", UPLOAD),
        ("DELETE", "/files/upload", UPLOAD),
        ("POST", "/files/upload/complete", UPLOAD),
        ("POST", "/files/download", None),
        ("PUT", "/files/upload/complete", None),
        ("POST", "/mcp", None),
    ],
)
def test_direction_of_each_route(method, route, direction):
    assert FileChannel.direction_of(method, route) == direction


# prepare_download / prepare_upload


def make_links(root, **server):
    return file_channel([root], max_chunk_bytes=64, **server)


def dut_links(root, interfaces):
    return file_channel([root], interfaces, host="0.0.0.0")


@pytest.fixture
def dut_route(interfaces):
    interfaces.own, interfaces.source = {"192.168.50.1"}, "192.168.50.1"
    return interfaces


def test_prepare_download_builds_url_and_curl(root):
    (root / "a b.txt").write_text("hey")
    links = make_links(root, public_base_url="https://fm.example.com/")

    r = links.prepare_download(str(root / "a b.txt"))

    assert r["url"] == f"https://fm.example.com/files/download?path={q(os.path.realpath(root / 'a b.txt'))}"
    assert r["size"] == 3
    assert "$FILE_MANAGER_TOKEN" in r["curl"] and r["url"] in r["curl"]


def test_curl_examples_quote_the_file_name_for_the_shell(root, dut_route):
    (root / "$(id) `x`.bin").write_bytes(b"x")
    path = str(root / "$(id) `x`.bin")
    links = dut_links(root, dut_route)

    for r in (links.prepare_download(path), links.prepare_download(path, for_host="192.168.50.10")):
        assert "-o '$(id) `x`.bin'" in r["curl"]


def test_prepare_download_requires_existing_file(root):
    links = make_links(root)
    with pytest.raises(FileManagerError) as exc:
        links.prepare_download(str(root / "missing"))
    assert exc.value.code == "NOT_FOUND"


def test_prepare_download_of_a_directory_is_not_a_file(root):
    (root / "d").mkdir()
    links = make_links(root)
    with pytest.raises(FileManagerError) as exc:
        links.prepare_download(str(root / "d"))
    assert exc.value.code == "NOT_A_FILE"


def test_prepare_upload_reports_offset_and_urls(root):
    (root / "f.bin.fmcp-partial").write_bytes(b"12")
    links = make_links(root)

    r = links.prepare_upload(str(root / "f.bin"))

    encoded = q(os.path.realpath(root / "f.bin"))
    assert r["upload_url"] == f"http://127.0.0.1:8765/files/upload?path={encoded}&offset=2"
    assert r["complete_url"] == f"http://127.0.0.1:8765/files/upload/complete?path={encoded}&overwrite=false"
    assert r["offset"] == 2
    assert r["max_chunk_bytes"] == 64
    assert "$FILE_MANAGER_TOKEN" in r["curl"]


def invalid_argument(fn, **kwargs) -> str:
    with pytest.raises(FileManagerError) as exc:
        fn(**kwargs)
    assert exc.value.code == "INVALID_ARGUMENT"
    return exc.value.message


def test_for_host_gives_a_tokenless_link_on_the_dut_side_address(root, dut_route):
    (root / "fw.bin").write_bytes(b"x")

    r = dut_links(root, dut_route).prepare_download(str(root / "fw.bin"), for_host="192.168.50.10", expires_in=600)

    url = urlsplit(r["url"])
    assert (url.netloc, url.path) == ("192.168.50.1:8765", "/files/download")
    assert {"path", "exp", "by", "sig"} <= parse_qs(url.query, keep_blank_values=True).keys()
    assert r["url"] in r["curl"] and "Authorization" not in r["curl"]
    assert r["expires"]


def test_for_host_the_dut_cannot_reach_is_refused(root, interfaces):
    (root / "fw.bin").write_bytes(b"x")
    links = dut_links(root, interfaces)
    invalid_argument(links.prepare_download, path=str(root / "fw.bin"), for_host="192.168.50.10")
    invalid_argument(links.prepare_upload, path=str(root / "log.txt"), for_host="192.168.50.10")


@pytest.mark.parametrize("expires_in", [59, 86401])
def test_expires_in_out_of_range_is_refused(root, dut_route, expires_in):
    (root / "fw.bin").write_bytes(b"x")
    links = dut_links(root, dut_route)
    invalid_argument(
        links.prepare_download, path=str(root / "fw.bin"), for_host="192.168.50.10", expires_in=expires_in
    )


def test_for_host_upload_links_are_signed_with_overwrite_and_max_bytes(root, dut_route):
    links = dut_links(root, dut_route)
    r = links.prepare_upload(str(root / "log.txt"), for_host="192.168.50.10", overwrite=True, max_bytes=5)

    for key in ("upload_url", "complete_url"):
        url = urlsplit(r[key])
        assert url.netloc == "192.168.50.1:8765"
        q = parse_qs(url.query, keep_blank_values=True)
        assert (q["overwrite"], q["max_bytes"]) == (["true"], ["5"]) and "sig" in q
    assert "Authorization" not in r["curl"] and r["expires"]


def test_max_bytes_needs_for_host(root, interfaces):
    links = dut_links(root, interfaces)
    invalid_argument(links.prepare_upload, path=str(root / "log.txt"), max_bytes=5)


def test_prepare_upload_requires_parent(root):
    links = make_links(root)
    with pytest.raises(FileManagerError) as exc:
        links.prepare_upload(str(root / "no" / "f"))
    assert exc.value.code == "NOT_FOUND"


# chunked upload contract: offset in the URLs, size/sha256 checks, reset, per-chunk commands


def test_prepare_upload_with_size_lists_chunks_from_the_current_offset(root):
    (root / "f.bin.fmcp-partial").write_bytes(b"12")
    links = make_links(root)

    r = links.prepare_upload(str(root / "f.bin"), size=150)

    assert [(c["offset"], c["length"], c["expect_offset"]) for c in r["chunks"]] == [
        (2, 64, 66),
        (66, 64, 130),
        (130, 20, 150),
    ]
    assert all(c["method"] == "PUT" for c in r["chunks"])
    assert r["chunks"][0]["url"] == r["upload_url"] and r["chunks"][0]["curl"] == r["curl"]
    assert r["chunks"][1]["url"].endswith("&offset=66")
    assert "tail -c +67" in r["chunks"][1]["curl"] and "head -c 64" in r["chunks"][1]["curl"]
    assert "next_offset" not in r
    assert r["complete"]["method"] == "POST" and r["complete"]["url"] == r["complete_url"]
    assert "size=150" in r["complete_url"]


def test_prepare_upload_without_size_uploads_at_most_one_chunk(root):
    links = make_links(root)
    r = links.prepare_upload(str(root / "f.bin"))
    assert "chunks" not in r
    assert "head -c 64" in r["curl"] and "@LOCAL_FILE" not in r["curl"] and "complete" not in r["curl"]


def test_prepare_upload_caps_chunks_and_reports_next_offset(root):
    links = make_links(root)
    r = links.prepare_upload(str(root / "f.bin"), size=64 * 40)
    assert len(r["chunks"]) == channel.MAX_CHUNKS
    assert r["next_offset"] == 64 * channel.MAX_CHUNKS


def test_prepare_upload_of_an_empty_file_still_has_one_empty_chunk(root):
    r = make_links(root).prepare_upload(str(root / "f.bin"), size=0)
    assert [(c["offset"], c["length"]) for c in r["chunks"]] == [(0, 0)]


def test_prepare_upload_rejects_impossible_size_and_sha256(root, dut_route):
    (root / "f.bin.fmcp-partial").write_bytes(b"12345")
    links = make_links(root)
    assert "DELETE" in invalid_argument(links.prepare_upload, path=str(root / "f.bin"), size=4)
    invalid_argument(links.prepare_upload, path=str(root / "f.bin"), size=-1)
    invalid_argument(links.prepare_upload, path=str(root / "f.bin"), sha256="xyz")
    dut = dut_links(root, dut_route)
    invalid_argument(dut.prepare_upload, path=str(root / "g.bin"), for_host="192.168.50.10", size=10, max_bytes=9)


def test_for_host_upload_links_sign_size_and_sha256(root, dut_route):
    digest = "AB" * 32
    r = dut_links(root, dut_route).prepare_upload(
        str(root / "log.txt"), for_host="192.168.50.10", size=5, sha256=digest
    )
    for key in ("upload_url", "complete_url"):
        query = parse_qs(urlsplit(r[key]).query, keep_blank_values=True)
        assert (query["size"], query["sha256"]) == (["5"], [digest.lower()])


def test_offset_mismatch_reports_expected_offset(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    body = client.put(f"/files/upload?path={target}&offset=1", content=b"x").json()
    assert (body["offset"], body["expected_offset"]) == (3, 3)


def test_complete_short_partial_is_resumable_size_mismatch(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    r = client.post(f"/files/upload/complete?path={target}&size=5")
    body = r.json()
    assert (r.status_code, body["error"]) == (409, "SIZE_MISMATCH")
    assert (body["size"], body["expected_size"], body["offset"], body["reset_required"]) == (3, 5, 3, False)
    assert (root / "up.bin.fmcp-partial").exists() and not (root / "up.bin").exists()


def test_complete_long_partial_needs_reset_and_the_reset_url_works(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abcdef")
    body = client.post(f"/files/upload/complete?path={target}&size=5").json()
    assert (body["error"], body["reset_required"], body["reset"]["method"]) == ("SIZE_MISMATCH", True, "DELETE")

    url = urlsplit(body["reset"]["url"])
    assert url.path == "/files/upload" and "offset" not in parse_qs(url.query)
    assert client.delete(f"{url.path}?{url.query}").json() == {"deleted": True}
    assert not (root / "up.bin.fmcp-partial").exists()


def test_complete_size_and_sha256_publish_when_both_match(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"hello")
    digest = hashlib.sha256(b"hello").hexdigest()
    r = client.post(f"/files/upload/complete?path={target}&size=5&sha256={digest}")
    assert (r.status_code, r.json()["size"], r.json()["sha256"]) == (200, 5, digest)
    assert (root / "up.bin").read_bytes() == b"hello" and not (root / "up.bin.fmcp-partial").exists()


def test_digest_mismatch_at_full_size_requires_reset(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"hello")
    body = client.post(f"/files/upload/complete?path={target}&size=5&sha256={'0' * 64}").json()
    assert (body["error"], body["reset_required"], body["size"]) == ("DIGEST_MISMATCH", True, 5)
    assert body["reset"]["method"] == "DELETE"
    assert (root / "up.bin.fmcp-partial").exists()


def test_digest_mismatch_without_size_does_not_claim_a_reset_is_required(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    body = client.post(f"/files/upload/complete?path={target}&sha256={'0' * 64}").json()
    assert (body["error"], body["reset_required"]) == ("DIGEST_MISMATCH", False)


def test_complete_with_a_bad_size_query_is_400(client, root):
    target = q(root / "up.bin")
    client.put(f"/files/upload?path={target}&offset=0", content=b"abc")
    assert client.post(f"/files/upload/complete?path={target}&size=abc").status_code == 400


def test_prepare_upload_never_discards_an_existing_partial(root):
    (root / "f.bin.fmcp-partial").write_bytes(b"12")
    make_links(root).prepare_upload(str(root / "f.bin"), size=10)
    assert (root / "f.bin.fmcp-partial").read_bytes() == b"12"


def _shell():
    candidates = [shutil.which("bash"), r"C:\Program Files\Git\usr\bin\bash.exe", "/bin/bash"]
    for candidate in candidates:
        if candidate and os.path.exists(candidate) and "system32" not in candidate.lower():
            return candidate
    return None


@pytest.mark.skipif(_shell() is None, reason="needs a POSIX shell with tail and head")
@pytest.mark.parametrize("options", ["", "set -e; set -o pipefail;"])
def test_chunk_command_slices_and_survives_sigpipe_under_pipefail(tmp_path, options):
    source = tmp_path / "big.bin"
    source.write_bytes(bytes(range(256)) * 2048)  # 512 KiB: far more than a pipe buffer, so head cuts tail short
    sent = tmp_path / "sent.bin"
    command = channel._chunk_curl("", "http://x/files/upload?path=p&offset=10", 10, 5)
    script = (
        f"{options} curl() {{ cat > '{sent.as_posix()}'; }}; "
        f"LOCAL_FILE='{source.as_posix()}'; {command}; echo rc=$?"
    )
    env = {**os.environ, "PATH": os.path.dirname(_shell()) + os.pathsep + os.environ["PATH"]}

    out = subprocess.run([_shell(), "-c", script], capture_output=True, text=True, env=env)

    assert "rc=0" in out.stdout, out.stderr
    assert sent.read_bytes() == source.read_bytes()[10:15]


# job logs


def job_channel(root, tmp_path, **kw):
    jobs = JobRegistry(state_dir=tmp_path, **kw)
    channel_ = file_channel([root])
    channel_.jobs = jobs
    return jobs, channel_, TestClient(Starlette(routes=channel_.routes))


async def run_job(jobs, code):
    job = await jobs.start([sys.executable, "-c", code])
    await jobs.wait_result(job, 20)
    return job


@pytest.mark.anyio
async def test_job_log_route_streams_the_stored_bytes(root, tmp_path):
    jobs, _, http = job_channel(root, tmp_path)
    job = await run_job(jobs, "import sys; sys.stdout.buffer.write(b'out\\n' * 3); sys.stderr.write('err')")
    out = await anyio.to_thread.run_sync(lambda: http.get(f"/files/job-log?job={job.id}&stream=stdout"))
    err = await anyio.to_thread.run_sync(lambda: http.get(f"/files/job-log?job={job.id}&stream=stderr"))
    assert out.status_code == 200 and out.content == b"out\n" * 3
    assert out.headers["content-length"] == "12" and err.content == b"err"
    assert job.downloads == 0


@pytest.mark.anyio
async def test_job_log_route_errors(root, tmp_path):
    jobs, _, http = job_channel(root, tmp_path)
    job = await run_job(jobs, "print(1)")
    get = lambda url: anyio.to_thread.run_sync(lambda: http.get(url))  # noqa: E731
    assert (await get("/files/job-log?job=nope&stream=stdout")).status_code == 404
    assert (await get(f"/files/job-log?job={job.id}&stream=other")).status_code == 400
    job.log_reclaimed = True
    reclaimed = await get(f"/files/job-log?job={job.id}&stream=stdout")
    assert reclaimed.status_code == 410 and reclaimed.json()["error"] == "LOG_RECLAIMED"


@pytest.mark.anyio
async def test_prepare_job_log_protects_the_log_and_has_no_signed_link(root, tmp_path):
    jobs, channel_, _ = job_channel(root, tmp_path)
    job = await run_job(jobs, "print('hello')")
    info = channel_.prepare_job_log(job.id, "stdout", expires_in=600)
    assert info["size"] == len("hello") + len(os.linesep)
    assert f"job={job.id}" in info["url"] and "stream=stdout" in info["url"] and "sig=" not in info["url"]
    assert "Authorization: Bearer" in info["curl"] and info["log_truncated"] is False
    assert job.protect_until > jobs.wall()
    with pytest.raises(FileManagerError) as exc:
        channel_.prepare_job_log(job.id, "stdout", expires_in=0)
    assert exc.value.code == "INVALID_ARGUMENT"
    with pytest.raises(FileManagerError) as exc:
        channel_.prepare_job_log(job.id, "both")
    assert exc.value.code == "INVALID_ARGUMENT"
    with pytest.raises(FileManagerError) as exc:
        channel_.prepare_job_log("nope")
    assert exc.value.code == "JOB_NOT_FOUND"


@pytest.mark.anyio
async def test_job_log_route_refuses_signed_links(root, tmp_path):
    from file_manager_mcp.auth import Caller
    from file_manager_mcp.signed import Grant

    jobs, channel_, _ = job_channel(root, tmp_path)
    job = await run_job(jobs, "print(1)")
    link = Caller("tok", Grant(UPLOAD, "x", "tok", False, None, "sig", 2**40))

    async def signed(scope, receive, send):
        scope.setdefault("state", {})["caller"] = link
        await app(scope, receive, send)

    app = Starlette(routes=channel_.routes)
    http = TestClient(signed)
    response = await anyio.to_thread.run_sync(lambda: http.get(f"/files/job-log?job={job.id}&stream=stdout"))
    assert response.status_code == 403 and response.json()["error"] == "INVALID_LINK"
