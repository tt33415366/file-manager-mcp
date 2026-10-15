from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from file_manager_mcp.auth import AuthMiddleware, Caller, UploadRules, caller_of
from file_manager_mcp.channel import FileChannel
from file_manager_mcp.signed import DOWNLOAD, UPLOAD, LinkSigner

TOKENS = {"fmcp_good": "laptop"}


SIGNER = LinkSigner()


def make_client(rejections=None):
    async def echo(request: Request):
        return JSONResponse({"token": caller_of(request).audit_name})

    app = Starlette(routes=[Route("/mcp", echo, methods=["GET", "POST"]), Route("/files/download", echo)])
    wrapped = AuthMiddleware(
        app,
        verify=TOKENS.get,
        accepts_host=lambda host: host == "testserver",
        allowed_origins=["https://fm.example.com"],
        direction_of=FileChannel.direction_of,
        verify_link=SIGNER.verify,
        on_reject=(lambda scope, status: rejections.append(status)) if rejections is not None else None,
    )
    return TestClient(wrapped)


AUTH = {"Authorization": "Bearer fmcp_good"}


def test_missing_token_is_401_with_bearer_challenge():
    r = make_client().get("/mcp")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.json() == {"error": "unauthorized"}


def test_wrong_token_is_401():
    r = make_client().get("/mcp", headers={"Authorization": "Bearer fmcp_bad"})
    assert r.status_code == 401


def test_non_bearer_scheme_is_401():
    r = make_client().get("/mcp", headers={"Authorization": "Basic fmcp_good"})
    assert r.status_code == 401


def test_valid_token_passes_and_exposes_token_name():
    r = make_client().get("/mcp", headers={"Authorization": "Bearer fmcp_good"})
    assert r.status_code == 200
    assert r.json() == {"token": "laptop"}


def test_every_path_is_protected():
    client = make_client()
    assert client.get("/files/download").status_code == 401
    assert client.get("/anything-else").status_code == 401


def test_unknown_host_is_rejected_before_auth():
    r = make_client().get("/mcp", headers={"Authorization": "Bearer fmcp_good", "Host": "evil.example"})
    assert r.status_code == 421


def test_disallowed_origin_is_403():
    r = make_client().get(
        "/mcp", headers={"Authorization": "Bearer fmcp_good", "Origin": "https://evil.example"}
    )
    assert r.status_code == 403


def test_allowed_origin_passes():
    r = make_client().get(
        "/mcp", headers={"Authorization": "Bearer fmcp_good", "Origin": "https://fm.example.com"}
    )
    assert r.status_code == 200


def test_signed_link_passes_without_a_bearer_token():
    params = {"path": r"D:\root\fw.bin", **SIGNER.sign(DOWNLOAD, r"D:\root\fw.bin", "laptop", 600)}
    r = make_client().get("/files/download", params=params)
    assert r.status_code == 200
    assert r.json() == {"token": "signed:laptop"}


def test_bad_signed_link_is_403_even_with_a_valid_token():
    rejections = []
    params = {"path": r"D:\root\fw.bin", **SIGNER.sign(DOWNLOAD, r"D:\root\fw.bin", "laptop", 600)}
    params["path"] = r"D:\root\other.bin"
    r = make_client(rejections).get("/files/download", params=params, headers=AUTH)
    assert r.status_code == 403
    assert r.json() == {"error": "invalid_link"}
    assert rejections == [403]


def test_non_ascii_signature_is_a_reported_403():
    rejections = []
    params = {"path": r"D:\root\fw.bin", **SIGNER.sign(DOWNLOAD, r"D:\root\fw.bin", "laptop", 600)}
    params["sig"] = "é"
    r = make_client(rejections).get("/files/download", params=params)
    assert r.status_code == 403
    assert rejections == [403]


def test_rejections_are_reported():
    rejections = []
    make_client(rejections).get("/mcp")
    assert rejections == [401]


def test_signed_link_outside_the_file_channel_is_403():
    params = {"path": r"D:\root\fw.bin", **SIGNER.sign(DOWNLOAD, r"D:\root\fw.bin", "laptop", 600)}
    r = make_client().get("/mcp", params=params, headers=AUTH)
    assert (r.status_code, r.json()) == (403, {"error": "invalid_link"})


def test_a_token_caller_may_do_anything():
    caller = Caller("laptop")
    assert caller.audit_name == "laptop"
    assert caller.permits(DOWNLOAD, r"D:\root\a") and caller.permits(UPLOAD, r"D:\root\b")
    assert caller.upload_rules == UploadRules(None, None)


def test_a_signed_link_caller_is_held_to_its_link():
    path = r"D:\root\log.txt"
    grant = SIGNER.verify(UPLOAD, {"path": path, **SIGNER.sign(UPLOAD, path, "laptop", 600, max_bytes=5)})
    caller = Caller(grant.by, grant)
    assert caller.audit_name == "signed:laptop"
    assert caller.permits(UPLOAD, path)
    assert not caller.permits(DOWNLOAD, path)
    assert not caller.permits(UPLOAD, r"D:\root\other.txt")
    assert caller.upload_rules == UploadRules(overwrite=False, max_bytes=5)
