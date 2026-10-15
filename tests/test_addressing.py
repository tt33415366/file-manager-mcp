import pytest

from file_manager_mcp.addressing import Addressing
from file_manager_mcp.config import ServerConfig
from file_manager_mcp.errors import FileManagerError


def addressing(interfaces=None, **server):
    server = ServerConfig(**{"port": 8765, **server})
    return Addressing(server, interfaces) if interfaces is not None else Addressing(server)


@pytest.mark.parametrize(
    ("host", "public_base_url", "expected"),
    [
        ("127.0.0.1", "", "http://127.0.0.1:8765"),
        ("0.0.0.0", "", "http://127.0.0.1:8765"),
        ("", "", "http://127.0.0.1:8765"),
        ("::1", "", "http://[::1]:8765"),
        ("fe80::1", "", "http://[fe80::1]:8765"),
        ("0.0.0.0", "https://fm.example.com/", "https://fm.example.com"),
        ("127.0.0.1", "https://fm.example.com/base//", "https://fm.example.com/base"),
    ],
)
def test_base_url_is_usable_by_clients(host, public_base_url, expected):
    a = addressing(host=host, public_base_url=public_base_url)
    assert a.base_url == expected
    assert a.mcp_url == expected + "/mcp"


def test_local_url_ignores_public_base_url():
    assert addressing(public_base_url="https://fm.example.com").local_url == "http://127.0.0.1:8765"


def test_link_base_follows_the_request_host_unless_public_base_url_is_set():
    assert addressing().link_base("192.168.1.5:8765") == "http://192.168.1.5:8765"
    assert addressing().link_base(None) == "http://127.0.0.1:8765"
    assert addressing(public_base_url="https://fm.example.com").link_base("x:1") == "https://fm.example.com"


def test_loopback_names_and_allowed_hosts_are_accepted(interfaces):
    a = addressing(interfaces, allowed_hosts=["FM.example.com"])
    for host in ("127.0.0.1", "localhost:8765", "[::1]:8765", "fm.example.com", "LOCALHOST"):
        assert a.accepts_host(host), host
    assert not a.accepts_host("localhost:9999")
    assert not a.accepts_host("evil.example")


def test_own_ip_host_is_accepted_with_or_without_the_server_port(interfaces):
    interfaces.own = {"192.168.1.5"}
    a = addressing(interfaces)
    assert a.accepts_host("192.168.1.5:8765") and a.accepts_host("192.168.1.5")
    assert not a.accepts_host("192.168.1.5:9999")
    assert not a.accepts_host("192.168.1.6:8765")


def test_own_ip_added_later_is_accepted(interfaces):
    a = addressing(interfaces)
    assert not a.accepts_host("192.168.50.1:8765")
    interfaces.own.add("192.168.50.1")
    assert a.accepts_host("192.168.50.1:8765")


def test_server_settings_are_read_live(interfaces):
    server = ServerConfig(port=8765)
    a = Addressing(server, interfaces)
    server.public_base_url = "https://fm.example.com"
    assert a.base_url == "https://fm.example.com"


def test_lan_urls_only_when_listening_on_every_interface(interfaces):
    interfaces.own = {"192.168.1.5", "10.0.0.2"}
    assert addressing(interfaces, host="0.0.0.0").lan_urls() == [
        "http://10.0.0.2:8765/mcp",
        "http://192.168.1.5:8765/mcp",
    ]
    assert addressing(interfaces).lan_urls() == []


def test_lan_default_prefers_the_default_route(interfaces):
    interfaces.own = {"192.168.50.1", "10.0.0.2"}
    interfaces.default = "192.168.50.1"
    assert addressing(interfaces).lan_default() == "192.168.50.1"
    interfaces.default = None
    assert addressing(interfaces).lan_default() == "10.0.0.2"
    interfaces.own = set()
    assert addressing(interfaces).lan_default() is None


# dut_base


def refused(a, for_host) -> str:
    with pytest.raises(FileManagerError) as exc:
        a.dut_base(for_host)
    assert exc.value.code == "INVALID_ARGUMENT"
    return exc.value.message


@pytest.fixture
def dut_route(interfaces):
    interfaces.own, interfaces.source = {"192.168.50.1"}, "192.168.50.1"
    return interfaces


def test_dut_base_is_the_local_address_routed_to_the_dut(dut_route):
    assert addressing(dut_route, host="0.0.0.0").dut_base("192.168.50.10") == "http://192.168.50.1:8765"


def test_listener_on_the_dut_side_address_is_enough(dut_route):
    assert addressing(dut_route, host="192.168.50.1").dut_base("192.168.50.10") == "http://192.168.50.1:8765"


@pytest.mark.parametrize("listen_host", ["127.0.0.1", "10.0.0.2"])
def test_listener_not_covering_the_dut_side_address_is_refused(dut_route, listen_host):
    assert "0.0.0.0" in refused(addressing(dut_route, host=listen_host), "192.168.50.10")


def test_for_host_that_routes_to_this_pc_is_refused(interfaces):
    interfaces.source = "127.0.0.1"
    assert "this PC" in refused(addressing(interfaces, host="0.0.0.0"), "127.0.0.1")


def test_for_host_reached_from_a_link_local_address_is_refused(dut_route):
    dut_route.source = "169.254.7.1"
    message = refused(addressing(dut_route, host="0.0.0.0"), "169.254.7.2")
    assert "169.254.7.1" in message and "static IPv4" in message


def test_for_host_with_no_route_is_refused(interfaces):
    assert "no route" in refused(addressing(interfaces, host="0.0.0.0"), "192.168.50.10")


def test_unresolvable_for_host_is_refused(interfaces):
    refused(addressing(interfaces, host="0.0.0.0"), "no-such-dut.invalid")
