import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeInterfaces:
    """Fixed Local PC addresses for Addressing; tests change them in place."""

    def __init__(self) -> None:
        self.own: set[str] = set()
        self.source: str | None = None
        self.default: str | None = None

    def local_ipv4s(self) -> frozenset[str]:
        return frozenset(self.own)

    def route_source(self, ip: str) -> str | None:
        return self.source

    def default_ipv4(self) -> str | None:
        return self.default


@pytest.fixture
def interfaces() -> FakeInterfaces:
    return FakeInterfaces()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def write_config(tmp_path: Path):
    def _write(body: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    return _write
