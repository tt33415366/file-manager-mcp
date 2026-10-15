import pytest

from file_manager_mcp import interfaces


@pytest.fixture
def scan(monkeypatch):
    found = {"addrs": []}
    clock = {"now": 100.0}
    monkeypatch.setattr(interfaces, "_scan", lambda: found["addrs"])
    monkeypatch.setattr(interfaces, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(interfaces, "_cache", None)
    return found, clock


def test_local_ipv4s_excludes_loopback_and_link_local(scan):
    found, _ = scan
    found["addrs"] = ["127.0.0.1", "127.5.5.5", "169.254.3.4", "192.168.1.5", "10.0.0.2", "192.168.1.5"]

    assert interfaces.local_ipv4s() == frozenset({"192.168.1.5", "10.0.0.2"})


def test_local_ipv4s_picks_up_new_addresses_after_the_cache_expires(scan):
    found, clock = scan
    found["addrs"] = ["192.168.1.5"]
    assert interfaces.local_ipv4s() == {"192.168.1.5"}

    found["addrs"] = ["192.168.1.5", "192.168.50.1"]
    assert interfaces.local_ipv4s() == {"192.168.1.5"}

    clock["now"] += interfaces.CACHE_SECONDS
    assert interfaces.local_ipv4s() == {"192.168.1.5", "192.168.50.1"}


def test_route_source_of_loopback_is_loopback():
    assert interfaces.route_source("127.0.0.1") == "127.0.0.1"


def test_scan_on_this_machine_returns_ipv4_strings():
    for addr in interfaces._scan():
        assert addr.count(".") == 3
