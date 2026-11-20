"""The curl shim, run with a stand-in curl that records what it was asked to do."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import CURL_SHIM_PATH

# The routes the launcher's default ports give the two services, every host pattern of each.
_SLACK_ROUTES = "slack.com 4401\n*.slack.com 4401\n"
_ALL_ROUTES = _SLACK_ROUTES + "api.linear.app 4402\nuploads.linear.app 4402\ngmail.googleapis.com 4403\nwww.googleapis.com 4403\n*.googleapis.com 4403\n"


def _install_shim(tmp_path: Path, routes: str | None, router_line: str | None) -> Path:
    """The shim in a directory of its own, with the two files it reads from beside itself; either is
    left out when it is None."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shutil.copy2(CURL_SHIM_PATH, shim_dir / "curl_shim.sh")
    if routes is not None:
        (shim_dir / "curl_routes").write_text(routes)
    if router_line is not None:
        (shim_dir / "curl_router").write_text("{}\n".format(router_line))
    return shim_dir / "curl_shim.sh"


def _write_recording_program(program_path: Path, exit_status: int = 0) -> Path:
    """A stand-in for a curl that records its arguments, one per line, beside itself."""
    recorded_path = program_path.parent / "{}.recorded".format(program_path.name)
    program_path.parent.mkdir(parents=True, exist_ok=True)
    program_path.write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$@\" > {}\nexit {}\n".format(shlex.quote(str(recorded_path)), exit_status)
    )
    program_path.chmod(0o755)
    return recorded_path


def _run_shim(shim_path: Path, arguments: list[str], first_path_dir: Path | None = None) -> int:
    env = dict(os.environ)
    if first_path_dir is not None:
        env["PATH"] = "{}{}{}".format(first_path_dir, os.pathsep, env["PATH"])
    return subprocess.run(["bash", str(shim_path), *arguments], env=env, check=False).returncode


def test_without_routes_the_shim_is_the_router_under_another_name(tmp_path: Path) -> None:
    recorded_path = _write_recording_program(tmp_path / "router", exit_status=22)
    shim_path = _install_shim(tmp_path, routes=None, router_line=str(tmp_path / "router"))
    arguments = ["-s", "-H", "Authorization: Bearer a token with spaces", "https://slack.com/api/auth.test", "-d", ""]

    return_code = _run_shim(shim_path, arguments)

    # The router's own status is the shim's, as the caller reads it.
    assert return_code == 22
    assert recorded_path.read_text().splitlines() == arguments


def test_a_faked_host_is_pointed_at_its_fake_and_the_host_it_was_for_is_named(tmp_path: Path) -> None:
    recorded_path = _write_recording_program(tmp_path / "router")
    shim_path = _install_shim(tmp_path, routes=_ALL_ROUTES, router_line=str(tmp_path / "router"))

    return_code = _run_shim(
        shim_path,
        [
            "-s",
            "-H",
            "Authorization: Bearer a token with spaces",
            "-d",
            '{"query": "{ viewer { name } }"}',
            "https://api.linear.app/graphql?x=1#top",
        ],
    )

    assert return_code == 0
    assert recorded_path.read_text().splitlines() == [
        "-s",
        "-H",
        "Authorization: Bearer a token with spaces",
        "-d",
        '{"query": "{ viewer { name } }"}',
        "http://127.0.0.1:4402/graphql?x=1#top",
        "-H",
        "X-Forwarded-Host: api.linear.app",
    ]


@pytest.mark.parametrize(
    ("url", "rewritten", "forwarded_host"),
    [
        (
            "https://slack.com/api/conversations.history?channel=C1&limit=2",
            "http://127.0.0.1:4401/api/conversations.history?channel=C1&limit=2",
            "slack.com",
        ),
        ("https://slack.com", "http://127.0.0.1:4401", "slack.com"),
        (
            "https://files.slack.com/files-pri/T1-F1/report.pdf",
            "http://127.0.0.1:4401/files-pri/T1-F1/report.pdf",
            "files.slack.com",
        ),
        (
            "https://redwood.enterprise.slack.com/api/auth.test",
            "http://127.0.0.1:4401/api/auth.test",
            "redwood.enterprise.slack.com",
        ),
        ("https://uploads.linear.app/abc", "http://127.0.0.1:4402/abc", "uploads.linear.app"),
        # The same host however a client spells it: its default port stated, another case, plain http.
        ("https://slack.com:443/api/auth.test", "http://127.0.0.1:4401/api/auth.test", "slack.com"),
        ("https://slack.com:443", "http://127.0.0.1:4401", "slack.com"),
        ("http://slack.com/api/auth.test", "http://127.0.0.1:4401/api/auth.test", "slack.com"),
        ("http://slack.com:80/api/auth.test?x=1", "http://127.0.0.1:4401/api/auth.test?x=1", "slack.com"),
        ("https://SLACK.COM/api/Auth.Test?Q=Upper", "http://127.0.0.1:4401/api/Auth.Test?Q=Upper", "slack.com"),
        ("HTTPS://Files.Slack.Com:443/A/b", "http://127.0.0.1:4401/A/b", "files.slack.com"),
        ("https://API.linear.app/graphql", "http://127.0.0.1:4402/graphql", "api.linear.app"),
    ],
)
def test_every_host_pattern_of_a_service_is_routed(tmp_path: Path, url: str, rewritten: str, forwarded_host: str) -> None:
    recorded_path = _write_recording_program(tmp_path / "router")
    shim_path = _install_shim(tmp_path, routes=_ALL_ROUTES, router_line=str(tmp_path / "router"))

    assert _run_shim(shim_path, ["-s", url]) == 0
    assert recorded_path.read_text().splitlines() == [
        "-s",
        rewritten,
        "-H",
        "X-Forwarded-Host: {}".format(forwarded_host),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com/zen",
        "https://slack.com.evil.example/api/auth.test",
        "https://evilslack.com/api/auth.test",
        "https://linear.app/redwood/issue/ENG-1",
        "https://api.linear.app.evil.example/graphql",
        "https://evil.example/slack.com/api",
        "https://evil.example/?next=https://slack.com/api",
        "https://evil.example#.slack.com",
        "https://slack.com@evil.example/api/auth.test",
        "https://evil.example:443/slack.com",
        # A port that is not the scheme's own is some other server.
        "https://slack.com:8443/api/auth.test",
        "https://slack.com:80/api/auth.test",
        "http://slack.com:443/api/auth.test",
        "https://slack.com:4430/api/auth.test",
        "ftp://slack.com/api",
        "slack.com/api/auth.test",
    ],
)
def test_every_other_host_is_left_alone_however_much_it_looks_like_a_faked_one(tmp_path: Path, url: str) -> None:
    recorded_path = _write_recording_program(tmp_path / "router")
    shim_path = _install_shim(tmp_path, routes=_ALL_ROUTES, router_line=str(tmp_path / "router"))

    assert _run_shim(shim_path, ["-s", url, "-o", "out.json"]) == 0
    assert recorded_path.read_text().splitlines() == ["-s", url, "-o", "out.json"]


@pytest.mark.parametrize(
    "option",
    [
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
        "--json",
        "-H",
        "--header",
        "-F",
        "--form",
        "--form-string",
        "-e",
        "--referer",
        "-A",
        "--user-agent",
        "-b",
        "--cookie",
    ],
)
def test_an_options_value_is_never_read_as_a_url(tmp_path: Path, option: str) -> None:
    """A body or a header that happens to be a faked host's URL is what the caller is sending, to
    whichever host the request is for."""
    recorded_path = _write_recording_program(tmp_path / "router")
    shim_path = _install_shim(tmp_path, routes=_ALL_ROUTES, router_line=str(tmp_path / "router"))
    value = "https://slack.com/archives/C1/p1774000000000000"

    assert _run_shim(shim_path, [option, value, "https://api.github.com/zen"]) == 0
    assert recorded_path.read_text().splitlines() == [option, value, "https://api.github.com/zen"]
    # The request's own URL is still pointed at the fake, wherever it stands among the options.
    assert _run_shim(shim_path, [option, value, "https://slack.com/api/chat.postMessage", option, value]) == 0
    assert recorded_path.read_text().splitlines() == [
        option,
        value,
        "http://127.0.0.1:4401/api/chat.postMessage",
        option,
        value,
        "-H",
        "X-Forwarded-Host: slack.com",
    ]


def test_a_url_given_with_curls_own_url_option_is_routed(tmp_path: Path) -> None:
    recorded_path = _write_recording_program(tmp_path / "router")
    shim_path = _install_shim(tmp_path, routes=_ALL_ROUTES, router_line=str(tmp_path / "router"))

    assert _run_shim(shim_path, ["-X", "POST", "--url", "https://api.linear.app/graphql"]) == 0
    assert recorded_path.read_text().splitlines() == [
        "-X",
        "POST",
        "--url",
        "http://127.0.0.1:4402/graphql",
        "-H",
        "X-Forwarded-Host: api.linear.app",
    ]


def test_only_the_hosts_the_routes_name_are_routed(tmp_path: Path) -> None:
    recorded_path = _write_recording_program(tmp_path / "router")
    shim_path = _install_shim(tmp_path, routes=_SLACK_ROUTES, router_line=str(tmp_path / "router"))

    assert _run_shim(shim_path, ["https://api.linear.app/graphql"]) == 0
    assert recorded_path.read_text().splitlines() == ["https://api.linear.app/graphql"]


@pytest.mark.parametrize("router_state", ["no router file", "a router that is not there", "a router that cannot be run"])
def test_the_systems_curl_runs_when_the_named_router_cannot(tmp_path: Path, router_state: str) -> None:
    """A program that names no curl of its own has the system curl run, and so does the shim, still
    pointing the faked hosts at their fakes."""
    recorded_path = _write_recording_program(tmp_path / "bin" / "curl")
    router_path = tmp_path / "router"
    if router_state == "a router that cannot be run":
        _write_recording_program(router_path)
        router_path.chmod(0o644)
    shim_path = _install_shim(
        tmp_path, routes=_ALL_ROUTES, router_line=None if router_state == "no router file" else str(router_path)
    )

    return_code = _run_shim(shim_path, ["-s", "https://slack.com/api/auth.test"], first_path_dir=tmp_path / "bin")

    assert return_code == 0
    assert recorded_path.read_text().splitlines() == [
        "-s",
        "http://127.0.0.1:4401/api/auth.test",
        "-H",
        "X-Forwarded-Host: slack.com",
    ]
    assert not (tmp_path / "router.recorded").exists()


def test_with_neither_file_the_shim_passes_through_to_the_systems_curl(tmp_path: Path) -> None:
    recorded_path = _write_recording_program(tmp_path / "bin" / "curl")
    shim_path = _install_shim(tmp_path, routes=None, router_line=None)

    assert _run_shim(shim_path, ["https://slack.com/api/auth.test"], first_path_dir=tmp_path / "bin") == 0
    assert recorded_path.read_text().splitlines() == ["https://slack.com/api/auth.test"]
