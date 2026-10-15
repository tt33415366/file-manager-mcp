import os

import pytest

from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.files import FileOps
from file_manager_mcp.paths import PathPolicy


@pytest.fixture
def ops(root):
    return FileOps(PathPolicy([str(root)]))


def p(root, *parts):
    return str(root.joinpath(*parts))


def code_of(fn, *args, **kwargs):
    with pytest.raises(FileManagerError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


# list_roots / list_dir / stat


def test_list_roots(ops, root):
    assert ops.list_roots() == {"roots": [os.path.realpath(root)]}


def test_list_roots_is_empty_without_roots():
    assert FileOps(PathPolicy([])).list_roots() == {"roots": []}


def test_list_dir_sorted_with_types(ops, root):
    (root / "b.txt").write_bytes(b"12345")
    (root / "a").mkdir()

    result = ops.list_dir(p(root))

    assert [(e["name"], e["type"]) for e in result["entries"]] == [("a", "dir"), ("b.txt", "file")]
    assert result["entries"][1]["size"] == 5
    assert result["entries"][0]["size"] == 0
    assert result["entries"][1]["modified"].endswith("+00:00")
    assert result["next_cursor"] is None


def test_list_dir_paginates(ops, root):
    for i in range(5):
        (root / f"f{i}").write_text("")

    first = ops.list_dir(p(root), limit=2)
    second = ops.list_dir(p(root), cursor=first["next_cursor"], limit=2)
    third = ops.list_dir(p(root), cursor=second["next_cursor"], limit=2)

    assert [e["name"] for e in first["entries"]] == ["f0", "f1"]
    assert [e["name"] for e in second["entries"]] == ["f2", "f3"]
    assert [e["name"] for e in third["entries"]] == ["f4"]
    assert third["next_cursor"] is None


def test_list_dir_rejects_bad_limit_and_cursor(ops, root):
    assert code_of(ops.list_dir, p(root), limit=0) == "INVALID_ARGUMENT"
    assert code_of(ops.list_dir, p(root), limit=1001) == "INVALID_ARGUMENT"
    assert code_of(ops.list_dir, p(root), cursor="abc") == "INVALID_ARGUMENT"


def test_list_dir_errors(ops, root):
    (root / "f").write_text("")
    assert code_of(ops.list_dir, p(root, "missing")) == "NOT_FOUND"
    assert code_of(ops.list_dir, p(root, "f")) == "NOT_A_DIRECTORY"


def test_stat_existing_and_missing(ops, root):
    (root / "f").write_bytes(b"abc")

    s = ops.stat(p(root, "f"))
    assert s["exists"] is True and s["type"] == "file" and s["size"] == 3
    assert ops.stat(p(root, "nope")) == {"path": p(root, "nope"), "exists": False}


# read_text


def test_read_text_whole_file(ops, root):
    (root / "f.txt").write_text("hello", encoding="utf-8")
    assert ops.read_text(p(root, "f.txt")) == {
        "content": "hello",
        "offset": 0,
        "bytes_read": 5,
        "total_size": 5,
        "eof": True,
    }


def test_read_text_chunks_without_splitting_utf8(ops, root):
    (root / "f.txt").write_bytes("aé€".encode("utf-8"))  # 1 + 2 + 3 bytes

    first = ops.read_text(p(root, "f.txt"), max_bytes=4)
    second = ops.read_text(p(root, "f.txt"), offset=first["offset"] + first["bytes_read"], max_bytes=4)

    assert first["content"] == "aé" and first["bytes_read"] == 3 and first["eof"] is False
    assert second["content"] == "€" and second["eof"] is True


def test_read_text_offset_past_eof(ops, root):
    (root / "f.txt").write_text("hi")
    r = ops.read_text(p(root, "f.txt"), offset=10)
    assert r["content"] == "" and r["eof"] is True and r["bytes_read"] == 0


def test_read_text_rejects_binary(ops, root):
    (root / "b.bin").write_bytes(b"\xff\xfe\x00\x80")
    with pytest.raises(FileManagerError) as exc:
        ops.read_text(p(root, "b.bin"))
    assert exc.value.code == "NOT_TEXT"
    assert "prepare_download" in exc.value.message


def test_read_text_limits_and_errors(ops, root):
    (root / "d").mkdir()
    assert code_of(ops.read_text, p(root, "f"), max_bytes=262145) == "INVALID_ARGUMENT"
    assert code_of(ops.read_text, p(root, "f"), offset=-1) == "INVALID_ARGUMENT"
    assert code_of(ops.read_text, p(root, "missing")) == "NOT_FOUND"
    assert code_of(ops.read_text, p(root, "d")) == "NOT_A_FILE"


def test_read_text_other_encoding(ops, root):
    (root / "g.txt").write_bytes("äö".encode("latin-1"))
    assert ops.read_text(p(root, "g.txt"), encoding="latin-1")["content"] == "äö"


# write_text


def test_write_text_create_and_refuse_existing(ops, root):
    assert ops.write_text(p(root, "n.txt"), "hé") == {"path": p(root, "n.txt"), "bytes_written": 3}
    assert (root / "n.txt").read_bytes() == "hé".encode("utf-8")
    assert code_of(ops.write_text, p(root, "n.txt"), "x") == "ALREADY_EXISTS"


def test_write_text_overwrite_and_append(ops, root):
    (root / "f.txt").write_text("old")
    ops.write_text(p(root, "f.txt"), "new", mode="overwrite")
    ops.write_text(p(root, "f.txt"), "+", mode="append")
    assert (root / "f.txt").read_text() == "new+"
    assert [x.name for x in root.iterdir()] == ["f.txt"]


def test_write_text_parents(ops, root):
    assert code_of(ops.write_text, p(root, "a", "b.txt"), "x") == "NOT_FOUND"
    ops.write_text(p(root, "a", "b.txt"), "x", create_parents=True)
    assert (root / "a" / "b.txt").read_text() == "x"


def test_write_text_too_large_and_bad_mode(ops, root):
    with pytest.raises(FileManagerError) as exc:
        ops.write_text(p(root, "big.txt"), "x" * (1024 * 1024 + 1))
    assert exc.value.code == "TOO_LARGE" and "prepare_upload" in exc.value.message
    assert code_of(ops.write_text, p(root, "f.txt"), "x", mode="bogus") == "INVALID_ARGUMENT"


def test_write_text_onto_directory_or_root(ops, root):
    (root / "d").mkdir()
    assert code_of(ops.write_text, p(root, "d"), "x", mode="overwrite") == "NOT_A_FILE"
    assert code_of(ops.write_text, p(root), "x", mode="overwrite") == "NOT_A_FILE"


def test_write_outside_root_is_rejected(ops, tmp_path):
    assert code_of(ops.write_text, str(tmp_path / "x.txt"), "x") == "OUTSIDE_ROOTS"


# make_dir / move / copy / delete


def test_make_dir_creates_parents_and_is_idempotent(ops, root):
    ops.make_dir(p(root, "a", "b"))
    ops.make_dir(p(root, "a", "b"))
    assert (root / "a" / "b").is_dir()
    (root / "f").write_text("")
    assert code_of(ops.make_dir, p(root, "f")) == "NOT_A_DIRECTORY"


def test_move_file_and_overwrite_rules(ops, root):
    (root / "a").write_text("A")
    (root / "b").write_text("B")
    assert code_of(ops.move, p(root, "a"), p(root, "b")) == "ALREADY_EXISTS"
    ops.move(p(root, "a"), p(root, "b"), overwrite=True)
    assert (root / "b").read_text() == "A" and not (root / "a").exists()
    assert code_of(ops.move, p(root, "missing"), p(root, "c")) == "NOT_FOUND"


def test_move_directory(ops, root):
    (root / "d").mkdir()
    (root / "d" / "f").write_text("x")
    ops.move(p(root, "d"), p(root, "e"))
    assert (root / "e" / "f").read_text() == "x"


def test_move_refuses_root_and_directory_overwrite(ops, root):
    (root / "d1").mkdir()
    (root / "d2").mkdir()
    assert code_of(ops.move, p(root), p(root, "x")) == "INVALID_ARGUMENT"
    assert code_of(ops.move, p(root, "d1"), p(root, "d2"), overwrite=True) == "INVALID_ARGUMENT"


def test_copy_file_and_directory(ops, root):
    (root / "f").write_text("x")
    (root / "d").mkdir()
    (root / "d" / "g").write_text("y")

    ops.copy(p(root, "f"), p(root, "f2"))
    ops.copy(p(root, "d"), p(root, "d2"))

    assert (root / "f2").read_text() == "x"
    assert (root / "d2" / "g").read_text() == "y"
    assert code_of(ops.copy, p(root, "f"), p(root, "f2")) == "ALREADY_EXISTS"
    (root / "f").write_text("z")
    ops.copy(p(root, "f"), p(root, "f2"), overwrite=True)
    assert (root / "f2").read_text() == "z"


def test_delete_file_and_directories(ops, root):
    (root / "f").write_text("")
    (root / "empty").mkdir()
    (root / "full").mkdir()
    (root / "full" / "x").write_text("")

    ops.delete(p(root, "f"))
    ops.delete(p(root, "empty"))
    assert code_of(ops.delete, p(root, "full")) == "DIRECTORY_NOT_EMPTY"
    ops.delete(p(root, "full"), recursive=True)

    assert list(root.iterdir()) == []
    assert code_of(ops.delete, p(root, "f")) == "NOT_FOUND"


def test_delete_link_removes_link_not_target(root, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = root / "link"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target)
    ops = FileOps(PathPolicy([str(root)]))

    assert ops.list_dir(p(root))["entries"][0]["type"] == "symlink"
    ops.delete(p(root, "link"), recursive=True)

    assert not os.path.lexists(link)
    assert (target / "keep.txt").read_text() == "keep"


def test_delete_refuses_allowed_root(ops, root):
    assert code_of(ops.delete, p(root), recursive=True) == "INVALID_ARGUMENT"
    assert root.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Win32 strips trailing dots and spaces")
def test_allowed_root_named_with_a_trailing_space_or_dots_is_not_moved_or_deleted(root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (root / "keep.txt").write_text("keep")
    ops = FileOps(PathPolicy([str(root), str(other)]))

    assert code_of(ops.move, str(root) + "\\ ", p(other, "stolen")) == "INVALID_PATH"
    assert code_of(ops.delete, str(root) + "\\...", recursive=True) == "INVALID_PATH"
    assert (root / "keep.txt").read_text() == "keep"


def test_copy_or_move_directory_into_itself_is_invalid(ops, root):
    (root / "d").mkdir()
    assert code_of(ops.copy, p(root, "d"), p(root, "d", "inner")) == "INVALID_ARGUMENT"
    assert code_of(ops.move, p(root, "d"), p(root, "d", "inner")) == "INVALID_ARGUMENT"
    assert list((root / "d").iterdir()) == []


def test_write_text_unencodable_content_is_invalid_argument(ops, root):
    assert code_of(ops.write_text, p(root, "a.txt"), "é", encoding="ascii") == "INVALID_ARGUMENT"


def test_list_dir_cursor_of_non_ascii_digits_is_invalid_argument(ops, root):
    assert code_of(ops.list_dir, p(root), cursor="²") == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "call",
    [
        lambda ops, root, other: ops.delete(p(root), recursive=True),
        lambda ops, root, other: ops.move(p(root), p(other, "moved")),
    ],
    ids=["delete", "move"],
)
def test_root_guard_uses_the_roots_the_call_started_with(root, tmp_path, monkeypatch, call):
    other = tmp_path / "other"
    other.mkdir()
    (root / "keep.txt").write_text("keep")
    policy = PathPolicy([str(root)])
    real_lexists = os.path.lexists

    def swap_roots_then_lexists(path):
        policy.set_roots([str(other)])
        return real_lexists(path)

    monkeypatch.setattr(os.path, "lexists", swap_roots_then_lexists)
    with pytest.raises(FileManagerError) as exc:
        call(FileOps(policy), root, other)
    monkeypatch.undo()
    assert exc.value.code == "INVALID_ARGUMENT"
    assert (root / "keep.txt").read_text() == "keep"


# search


def test_search_by_name_glob(ops, root):
    (root / "a.py").write_text("")
    (root / "sub").mkdir()
    (root / "sub" / "B.PY").write_text("")
    (root / "sub" / "c.txt").write_text("")

    result = ops.search(p(root), name_glob="*.py")

    names = sorted(os.path.basename(m["path"]) for m in result["matches"])
    expected = ["B.PY", "a.py"] if os.name == "nt" else ["a.py"]
    assert names == sorted(expected)
    assert result["truncated"] is False


def test_search_by_content(ops, root):
    (root / "a.txt").write_text("one\nneedle here\nthree", encoding="utf-8")
    (root / "b.txt").write_text("nothing", encoding="utf-8")
    (root / "c.bin").write_bytes(b"\xff\xfeneedle")

    result = ops.search(p(root), content_regex="needle")

    assert result["matches"] == [{"path": p(root, "a.txt"), "line": 2, "text": "needle here"}]


def test_search_truncates_at_limit(ops, root):
    for i in range(5):
        (root / f"f{i}.txt").write_text("")
    result = ops.search(p(root), limit=3)
    assert len(result["matches"]) == 3 and result["truncated"] is True


def test_search_rejects_bad_regex(ops, root):
    assert code_of(ops.search, p(root), content_regex="(") == "INVALID_ARGUMENT"


def test_search_does_not_descend_through_a_directory_link(ops, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle")
    link = root / "link"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside)

    assert ops.search(p(root), content_regex="needle")["matches"] == []
    assert ops.search(p(root))["matches"] == [{"path": p(root, "link")}]


def test_search_does_not_read_through_a_file_link(ops, root, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("needle")
    try:
        (root / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks needs privilege on this machine")

    assert ops.search(p(root), content_regex="needle")["matches"] == []
