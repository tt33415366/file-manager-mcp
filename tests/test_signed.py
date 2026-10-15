import pytest

from file_manager_mcp.signed import DOWNLOAD, UPLOAD, LinkSigner


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def signer(clock):
    return LinkSigner(clock=clock)


def download_query(signer, path=r"D:\root\fw.bin", by="laptop", expires_in=3600):
    return {"path": path, **signer.sign(DOWNLOAD, path, by, expires_in)}


def test_valid_download_link_verifies_on_get(signer):
    grant = signer.verify(DOWNLOAD, download_query(signer))
    assert grant is not None
    assert (grant.direction, grant.path, grant.by) == (DOWNLOAD, r"D:\root\fw.bin", "laptop")


def test_links_of_a_removed_issuer_are_rejected(clock):
    issuers = {"laptop"}
    signer = LinkSigner(clock=clock, issuers=lambda: issuers)
    query = download_query(signer)
    assert signer.verify(DOWNLOAD, query) is not None
    issuers.clear()
    assert signer.verify(DOWNLOAD, query) is None


def test_non_ascii_signature_is_rejected(signer):
    query = download_query(signer)
    query["sig"] = "é"
    assert signer.verify(DOWNLOAD, query) is None


def test_fields_cannot_shift_across_a_newline_in_the_path(signer, clock):
    signed = download_query(signer, path="/r/a\n9999999999\nevil", expires_in=60)
    exp = signed["exp"]
    forged = {"path": "/r/a", "exp": "9999999999", "by": f"evil\n{exp}\nlaptop", "sig": signed["sig"]}
    assert signer.verify(DOWNLOAD, forged) is None


def test_link_is_reusable_until_it_expires(signer, clock):
    query = download_query(signer, expires_in=60)
    assert signer.verify(DOWNLOAD, query) is not None
    clock.now += 59
    assert signer.verify(DOWNLOAD, query) is not None
    clock.now += 1
    assert signer.verify(DOWNLOAD, query) is None


@pytest.mark.parametrize("field, value", [("path", r"D:\root\other.bin"), ("exp", "9999999999"), ("by", "admin")])
def test_tampered_fields_fail(signer, field, value):
    query = download_query(signer)
    query[field] = value
    assert signer.verify(DOWNLOAD, query) is None


def test_link_from_another_launch_fails(signer, clock):
    assert LinkSigner(clock=clock).verify(DOWNLOAD, download_query(signer)) is None


def test_download_link_is_refused_for_uploads(signer):
    assert signer.verify(UPLOAD, download_query(signer)) is None


def test_repeated_query_keys_fail(signer):
    query = download_query(signer)
    pairs = [*query.items(), ("path", r"D:\root\other.bin")]
    assert signer.verify(DOWNLOAD, pairs) is None


def upload_query(signer, overwrite=False, max_bytes=None, path=r"D:\root\log.txt"):
    return {"path": path, **signer.sign(UPLOAD, path, "laptop", 600, overwrite=overwrite, max_bytes=max_bytes)}


def test_upload_link_carries_overwrite_and_max_bytes(signer):
    grant = signer.verify(UPLOAD, upload_query(signer, overwrite=True, max_bytes=5))
    assert grant is not None
    assert (grant.direction, grant.overwrite, grant.max_bytes) == (UPLOAD, True, 5)


def test_upload_link_is_refused_on_download_and_cannot_flip_overwrite(signer):
    assert signer.verify(DOWNLOAD, upload_query(signer)) is None
    query = upload_query(signer)
    query["overwrite"] = "true"
    assert signer.verify(UPLOAD, query) is None


def test_consumed_link_fails_but_others_still_work(signer):
    used, other = upload_query(signer), upload_query(signer, path=r"D:\root\other.txt")
    grant = signer.verify(UPLOAD, used)

    signer.consume(grant)

    assert signer.verify(UPLOAD, used) is None
    assert signer.verify(UPLOAD, other) is not None


def test_missing_or_garbled_fields_fail(signer):
    query = download_query(signer)
    assert signer.verify(DOWNLOAD, {k: v for k, v in query.items() if k != "exp"}) is None
    assert signer.verify(DOWNLOAD, {**query, "exp": "soon"}) is None
