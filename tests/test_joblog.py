import pytest

from file_manager_mcp import joblog
from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.joblog import OutputBuffer, StreamLog


@pytest.fixture
def log(tmp_path):
    return StreamLog(str(tmp_path / "out.log"))


def test_read_everything_when_it_fits(log):
    log.append("héllo\n".encode())
    r = log.read(0)
    assert (r.text, r.next, r.lost, r.more) == ("héllo\n", 7, 0, False)


def test_a_limit_inside_a_character_leaves_it_for_the_next_read(log):
    log.append("é".encode() * 10)  # 20 bytes
    first = log.read(0, limit=5)
    assert (first.text, first.next, first.more) == ("éé", 4, True)
    second = log.read(first.next, limit=100)
    assert (second.text, second.next, second.more) == ("é" * 8, 20, False)


def test_an_incomplete_character_at_the_open_end_waits_for_more_output(log):
    log.append(b"ab\xe4\xb8")
    waiting = log.read(0)
    assert (waiting.text, waiting.next, waiting.more) == ("ab", 2, False)
    log.append(b"\xad")
    assert (log.read(2).text, log.read(2).next) == ("中", 5)


def test_an_incomplete_character_at_a_finished_streams_end_is_replaced_once(log):
    log.append(b"ab\xe4\xb8")
    log.close()
    r = log.read(0)
    assert (r.text, r.next, r.more) == ("ab\ufffd", 4, False)
    again = log.read(r.next)
    assert (again.text, again.next) == ("", 4)


def test_invalid_bytes_are_replaced_one_for_one_like_python_does(log):
    log.append(b"\xff\xff")
    log.close()
    assert log.read(0).text == b"\xff\xff".decode("utf-8", "replace") == "\ufffd\ufffd"


def test_offsets_are_bytes_not_characters(log):
    log.append("中文abc".encode())
    r = log.read(3)
    assert (r.text, r.next) == ("文abc", 9)


def test_an_offset_past_the_end_is_invalid(log):
    log.append(b"abc")
    with pytest.raises(FileManagerError) as exc:
        log.read(4)
    assert exc.value.code == "INVALID_ARGUMENT"


@pytest.fixture
def gapped(tmp_path, monkeypatch):
    """16 bytes produced, the first 6 on disk, the memory buffer holding the first and last 4: bytes 6-11 are lost."""
    monkeypatch.setattr(joblog, "STREAM_LOG_LIMIT", 6)
    log = StreamLog(str(tmp_path / "out.log"))
    log.buffer = OutputBuffer(limit=8)
    log.append(b"0123456789abcdef")
    return log


def test_a_read_ends_at_a_gap_and_the_next_one_skips_it_counting_what_was_lost(gapped):
    first = gapped.read(0)
    assert (first.text, first.next, first.lost, first.more) == ("012345", 6, 0, True)
    second = gapped.read(first.next)
    assert (second.text, second.next, second.lost, second.more) == ("cdef", 16, 6, False)


def test_a_cursor_inside_the_gap_resumes_at_the_tail_window(gapped):
    r = gapped.read(8)
    assert (r.text, r.next, r.lost) == ("cdef", 16, 4)
    assert (gapped.retained, gapped.produced, gapped.truncated) == (6, 16, True)


def test_a_gap_boundary_does_not_wait_for_continuation_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(joblog, "STREAM_LOG_LIMIT", 4)
    log = StreamLog(str(tmp_path / "out.log"))
    log.buffer = OutputBuffer(limit=4)
    log.append(b"ab\xe4\xb8" + b"xxxxxxxx")  # the stored part ends inside a character
    r = log.read(0)
    assert (r.text, r.next, r.more) == ("ab\ufffd", 4, True)
    assert log.read(r.next).lost > 0


def test_nothing_is_stored_after_the_first_byte_that_was_not(tmp_path):
    room = [4]

    def reserve(n):
        allowed = min(n, room[0])
        room[0] -= allowed
        return allowed

    log = StreamLog(str(tmp_path / "out.log"), reserve=reserve)
    log.append(b"0123456789")
    room[0] = 100  # space frees up, but the file must stay a prefix of the stream
    log.append(b"abc")
    assert log.retained == 4 and log.truncated and log.produced == 13
    assert open(log.path, "rb").read() == b"0123"


def test_a_failing_disk_keeps_draining(tmp_path):
    released = []
    log = StreamLog(str(tmp_path / "missing-dir" / "out.log"), release=released.append)
    log.append(b"abc")
    log.append(b"def")
    assert (log.produced, log.retained, log.truncated) == (6, 0, True)
    assert log.read(0).text == "abcdef"  # still readable from the memory buffer


def test_recovered_terminal_stream_reports_the_unstored_part_as_lost(tmp_path):
    path = tmp_path / "out.log"
    path.write_bytes(b"012345")
    log = StreamLog.recovered(str(path), produced=16, truncated=True)
    first = log.read(0)
    assert (first.text, first.next, first.more) == ("012345", 6, True)
    second = log.read(6)
    assert (second.text, second.next, second.lost, second.more) == ("", 16, 10, False)


def test_recovered_interrupted_stream_ends_at_the_files_end(tmp_path):
    path = tmp_path / "out.log"
    path.write_bytes(b"ab\xe4\xb8")
    log = StreamLog.recovered(str(path), produced=None, truncated=False)
    r = log.read(0)
    assert (r.text, r.next, r.lost, r.more) == ("ab\ufffd", 4, 0, False)
    assert log.read(4).next == 4  # the same cursor again does not walk backwards or repeat
    with pytest.raises(FileManagerError):
        log.read(5)


def test_recovered_snapshot_comes_from_the_file(tmp_path):
    path = tmp_path / "out.log"
    path.write_bytes(b"hello")
    log = StreamLog.recovered(str(path), produced=5, truncated=False)
    data, dropped = log.snapshot()
    assert (data, dropped) == (b"hello", 0)


def test_discard_makes_reads_fail_as_reclaimed(log):
    log.append(b"abc")
    log.close()
    assert log.discard() == 3
    with pytest.raises(FileManagerError) as exc:
        log.read(0)
    assert exc.value.code == "LOG_RECLAIMED"
