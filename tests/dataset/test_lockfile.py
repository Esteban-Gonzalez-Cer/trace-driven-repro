import pytest

from tracerepro.dataset.lockfile import LockError, parse_lock


def test_parses_pins_with_markers_and_comments() -> None:
    pins = parse_lock(
        "# header\nWerkzeug==3.1.8\n\ncolorama==0.4.6 ; sys_platform == 'win32'\n"
        "    #   via click\nrequests[socks]==2.33.0\n"
    )
    assert [(p.key, p.version, p.marker) for p in pins] == [
        ("werkzeug", "3.1.8", None),
        ("colorama", "0.4.6", "sys_platform == 'win32'"),
        ("requests", "2.33.0", None),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "flask>=3.0",
        "flask",
        "flask==3.*",
        "flask @ https://example.com/flask.tar.gz",
        "--index-url https://evil.example/simple",
        "--extra-index-url https://evil.example/simple",
        "-e git+https://github.com/x/y",
        "-r other.txt",
        "--hash=sha256:abc",
    ],
)
def test_rejects_unpinned_or_options(line: str) -> None:
    with pytest.raises(LockError):
        parse_lock(f"pytest==9.0.2\n{line}\n")


def test_rejects_duplicate_pins() -> None:
    with pytest.raises(LockError, match="duplicate"):
        parse_lock("pytest==9.0.2\nPyTest==9.0.1\n")
