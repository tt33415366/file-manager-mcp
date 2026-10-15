import json

import pytest

from file_manager_mcp.tokens import TokenError, TokenStore


def test_add_returns_prefixed_token_and_stores_only_digest(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")

    token = store.add("cursor-cloud")

    assert token.startswith("fmcp_")
    assert len(token) > 40
    raw = (tmp_path / "tokens.json").read_text(encoding="utf-8")
    assert token not in raw
    entry = json.loads(raw)["tokens"][0]
    assert entry["name"] == "cursor-cloud"
    assert len(entry["sha256"]) == 64
    assert entry["created"]


def test_verify_returns_name_for_valid_token(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    a = store.add("a")
    b = store.add("b")

    reloaded = TokenStore(tmp_path / "tokens.json")
    assert reloaded.verify(a) == "a"
    assert reloaded.verify(b) == "b"


def test_verify_rejects_unknown_and_empty_tokens(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.add("a")

    assert store.verify("fmcp_wrong") is None
    assert store.verify("") is None


def test_duplicate_name_is_rejected(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.add("a")
    with pytest.raises(TokenError, match="already exists"):
        store.add("a")


def test_remove_revokes_token(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    token = store.add("a")

    store.remove("a")

    assert TokenStore(tmp_path / "tokens.json").verify(token) is None
    with pytest.raises(TokenError, match="no token"):
        store.remove("a")


def test_list_returns_names_and_dates(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.add("a")
    store.add("b")

    assert [e.name for e in store.list()] == ["a", "b"]


def test_missing_file_means_no_tokens(tmp_path):
    store = TokenStore(tmp_path / "nope" / "tokens.json")
    assert store.list() == []
    assert store.verify("fmcp_x") is None


def test_add_does_not_resurrect_a_token_removed_by_another_store(tmp_path):
    path = tmp_path / "tokens.json"
    shared = TokenStore(path)
    revoked = shared.add("laptop")

    TokenStore(path).remove("laptop")
    shared.add("phone")

    assert TokenStore(path).verify(revoked) is None
    assert [e.name for e in TokenStore(path).list()] == ["phone"]


def test_remove_keeps_a_token_added_by_another_store(tmp_path):
    path = tmp_path / "tokens.json"
    shared = TokenStore(path)
    shared.add("phone")

    cli_token = TokenStore(path).add("cli")
    shared.remove("phone")

    assert TokenStore(path).verify(cli_token) == "cli"


def test_verify_sees_a_removal_by_another_store(tmp_path):
    path = tmp_path / "tokens.json"
    shared = TokenStore(path)
    token = shared.add("laptop")
    shared.add("phone")
    assert shared.verify(token) == "laptop"

    TokenStore(path).remove("laptop")

    assert shared.verify(token) is None


def test_verify_sees_a_token_added_by_another_store(tmp_path):
    path = tmp_path / "tokens.json"
    shared = TokenStore(path)
    assert shared.verify("fmcp_x") is None

    token = TokenStore(path).add("cli")

    assert shared.verify(token) == "cli"
    assert [e.name for e in shared.list()] == ["cli"]
