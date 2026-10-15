import asyncio
import hashlib
import os
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
    assert r["upload_url"] == f"http://127.0.0.1:8765/files/upload?path={encoded}"
    assert r["complete_url"] == f"http://127.0.0.1:8765/files/upload/complete?path={encoded}"
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
