import os
import sys

import pytest

from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.paths import PathPolicy

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path rules")


def code_of(fn, *args):
    with pytest.raises(FileManagerError) as exc:
        fn(*args)
    return exc.value.code


def test_path_inside_root_resolves(root):
    (root / "a.txt").write_text("x")
    policy = PathPolicy([str(root)])
    assert policy.resolve(str(root / "a.txt")) == os.path.realpath(root / "a.txt")


def test_nonexistent_path_inside_root_is_allowed(root):
    policy = PathPolicy([str(root)])
    assert policy.resolve(str(root / "new" / "file.txt")).endswith("file.txt")


def test_root_itself_is_allowed(root):
    assert PathPolicy([str(root)]).resolve(str(root)) == os.path.realpath(root)


def test_forward_slashes_are_accepted(root):
    (root / "a.txt").write_text("x")
    policy = PathPolicy([str(root)])
    assert policy.resolve(str(root / "a.txt").replace("\\", "/")) == os.path.realpath(root / "a.txt")


def test_relative_path_is_invalid(root):
    assert code_of(PathPolicy([str(root)]).resolve, "a.txt") == "INVALID_PATH"


def test_empty_and_nul_paths_are_invalid(root):
    policy = PathPolicy([str(root)])
    assert code_of(policy.resolve, "") == "INVALID_PATH"
    assert code_of(policy.resolve, str(root / "a\0b")) == "INVALID_PATH"


def test_outside_root_is_rejected(root, tmp_path):
    assert code_of(PathPolicy([str(root)]).resolve, str(tmp_path / "other.txt")) == "OUTSIDE_ROOTS"


def test_dotdot_traversal_is_rejected(root):
    assert code_of(PathPolicy([str(root)]).resolve, str(root) + os.sep + ".." + os.sep + "x") == "OUTSIDE_ROOTS"


def test_sibling_with_common_prefix_is_rejected(root, tmp_path):
    sibling = tmp_path / "root2"
    sibling.mkdir()
    assert code_of(PathPolicy([str(root)]).resolve, str(sibling / "a")) == "OUTSIDE_ROOTS"


def test_link_escaping_root_is_rejected(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside)
    assert code_of(PathPolicy([str(root)]).resolve, str(link / "secret.txt")) == "OUTSIDE_ROOTS"


def test_no_follow_resolves_link_entry_itself(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside)
    policy = PathPolicy([str(root)])
    assert policy.resolve(str(link), follow=False) == os.path.join(os.path.realpath(root), "link")


def test_second_root_is_honoured(root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert PathPolicy([str(root), str(other)]).resolve(str(other / "a")).endswith("a")


def test_set_roots_replaces_the_roots(root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    policy = PathPolicy([str(root)])
    policy.set_roots([str(other)])
    assert policy.roots == (os.path.realpath(other),)
    assert policy.resolve(str(other / "a")).endswith("a")
    assert policy.is_root(os.path.realpath(other))
    assert not policy.is_root(os.path.realpath(root))
    assert code_of(policy.resolve, str(root / "a")) == "OUTSIDE_ROOTS"


def test_snapshot_keeps_the_roots_it_was_taken_with(root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    policy = PathPolicy([str(root)])
    snapshot = policy.snapshot()
    policy.set_roots([str(other)])
    assert snapshot.roots == (os.path.realpath(root),)
    assert snapshot.is_root(os.path.realpath(root))
    assert snapshot.resolve(str(root / "a")).endswith("a")
    assert code_of(snapshot.resolve, str(other / "a")) == "OUTSIDE_ROOTS"


NO_ROOTS_MESSAGE = "No Allowed Roots configured yet; ask the operator to add one in the config UI."


def test_empty_roots_reject_every_path_with_no_allowed_roots(root):
    with pytest.raises(FileManagerError) as exc:
        PathPolicy([]).resolve(str(root / "a"))
    assert exc.value.code == "NO_ALLOWED_ROOTS"
    assert exc.value.message == NO_ROOTS_MESSAGE


def test_set_roots_to_empty_rejects_with_no_allowed_roots(root):
    policy = PathPolicy([str(root)])
    policy.set_roots([])
    assert policy.roots == ()
    assert code_of(policy.resolve, str(root / "a")) == "NO_ALLOWED_ROOTS"
    assert code_of(policy.resolve, "relative") == "NO_ALLOWED_ROOTS"


@windows_only
def test_case_insensitive_match_on_windows(root):
    policy = PathPolicy([str(root)])
    assert policy.resolve(str(root).upper() + "\\a.txt")


@windows_only
def test_device_namespace_paths_are_invalid(root):
    policy = PathPolicy([str(root)])
    assert code_of(policy.resolve, "\\\\?\\" + str(root / "a")) == "INVALID_PATH"
    assert code_of(policy.resolve, "\\\\.\\PhysicalDrive0") == "INVALID_PATH"


@windows_only
def test_alternate_data_streams_are_invalid(root):
    assert code_of(PathPolicy([str(root)]).resolve, str(root / "a.txt:hidden")) == "INVALID_PATH"


@windows_only
def test_reserved_device_names_are_invalid(root):
    policy = PathPolicy([str(root)])
    assert code_of(policy.resolve, str(root / "CON")) == "INVALID_PATH"
    assert code_of(policy.resolve, str(root / "nul.txt")) == "INVALID_PATH"
    assert code_of(policy.resolve, str(root / "com1" / "x")) == "INVALID_PATH"


@windows_only
@pytest.mark.parametrize("name", [" ", "...", ". ", ".. ", "x.", "x "])
def test_components_ending_in_a_dot_or_space_are_invalid(root, name):
    policy = PathPolicy([str(root)])
    assert code_of(policy.resolve, str(root) + "\\" + name) == "INVALID_PATH"
    assert code_of(policy.resolve, str(root) + "\\" + name + "\\a.txt") == "INVALID_PATH"


def test_is_root(root):
    policy = PathPolicy([str(root)])
    assert policy.is_root(policy.resolve(str(root)))
    assert not policy.is_root(policy.resolve(str(root / "a")))


def test_existing_and_existing_file(root):
    (root / "a.txt").write_text("x")
    (root / "d").mkdir()
    policy = PathPolicy([str(root)])
    assert policy.existing(str(root / "d")) == os.path.realpath(root / "d")
    assert policy.existing_file(str(root / "a.txt")) == os.path.realpath(root / "a.txt")
    assert code_of(policy.existing, str(root / "missing")) == "NOT_FOUND"
    assert code_of(policy.existing_file, str(root / "missing")) == "NOT_FOUND"
    assert code_of(policy.existing_file, str(root / "d")) == "NOT_A_FILE"


def test_file_target_needs_a_parent_and_no_directory_in_the_way(root):
    (root / "d").mkdir()
    policy = PathPolicy([str(root)])
    assert policy.file_target(str(root / "new.txt")) == os.path.realpath(root / "new.txt")
    assert code_of(policy.file_target, str(root / "d")) == "NOT_A_FILE"
    assert code_of(policy.file_target, str(root / "no" / "f")) == "NOT_FOUND"
