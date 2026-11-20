"""The real launcher, run from this checkout's installed packages, on the fixture seeds.

Every fixture here starts one launcher process for one test and stops it afterwards. The launcher is
told it started `TEST_SHIFT_SECONDS` after the seeds' anchor, so the tests know every served date.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

import pytest

REPO_DIR: Final[Path] = Path(__file__).resolve().parent.parent
FIXTURES_DIR: Final[Path] = Path(__file__).resolve().parent / "fixtures"
LAUNCHER_PATH: Final[Path] = REPO_DIR / "launcher" / "launcher.mjs"
CURL_SHIM_PATH: Final[Path] = REPO_DIR / "curl_shim.sh"
# What every seeded timestamp is shifted by in a launcher test: the launcher is told it started this
# long after the seed's anchor. Years rather than days, so that no served date can fall near the wall
# clock on any day a test runs: a test that checks no record carries the process's clock asserts
# exactly that, and a shift of months would make it fail on the days the shifted seed dates come round.
TEST_SHIFT_SECONDS: Final[int] = 10 * 365 * 24 * 60 * 60
# How far past a whole number of days the launcher is told it started in a test of the whole-day
# shift, which the launcher drops.
TEST_PART_DAY_SECONDS: Final[int] = 7 * 60 * 60 + 30 * 60
# How long every rate limit window lasts in a test that waits one out.
TEST_RATE_LIMIT_WINDOW_SECONDS: Final[int] = 2
# What Linear's hourly request bucket holds in a test that fills it, so that filling it takes a few
# dozen requests.
TEST_LINEAR_REQUEST_LIMIT: Final[int] = 25
# Spaced so that a test's sequence of requests meets each fault once: the transient error at request
# 50, the unreadable page at 60, the episode at 70, and nothing else before 100.
TEST_FAULTS: Final[dict[str, int]] = {
    "after_requests": 2,
    "rate_limit_episode_every": 70,
    "rate_limit_episode_seconds": 2,
    "transient_error_every": 50,
    "unreadable_every": 60,
}
_LAUNCHER_READY_TIMEOUT_SECONDS: Final[float] = 15.0
_LAUNCHER_STOP_TIMEOUT_SECONDS: Final[float] = 5.0


def fixture_text(kind: str, service_name: str) -> str:
    """One fixture file: `kind` is `seed`, `update` or `refused_update`."""
    return (FIXTURES_DIR / kind / "{}.json".format(service_name)).read_text()


def fixture_document(kind: str, service_name: str) -> dict[str, Any]:
    return json.loads(fixture_text(kind, service_name))


@dataclass(frozen=True)
class RunningFakes:
    """A launcher serving a directory of seeds on ports of its own, for the length of one test."""

    control_url: str
    service_urls: dict[str, str]
    access_log_path: Path

    @property
    def slack_url(self) -> str:
        return self.service_urls["slack"]

    @property
    def linear_url(self) -> str:
        return self.service_urls["linear"]

    @property
    def google_url(self) -> str:
        return self.service_urls["google"]


def _read_ready_line(stdout, ports: list[dict[str, int]]) -> None:
    """Watches the launcher's stdout for the one line it prints once every port listens."""
    for line in stdout:
        if line.lstrip().startswith("{"):
            ports.append(json.loads(line)["ports"])
            return


def _wait_until(condition: Callable[[], bool], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)


def _running_fakes(
    run_dir: Path,
    seed_dir: Path,
    part_day_seconds: int = 0,
    rate_limit_window_seconds: int | None = None,
    rate_limit_by_bucket: Mapping[str, int] | None = None,
) -> Iterator[RunningFakes]:
    """The real launcher on one directory of seeds. It is told it started `TEST_SHIFT_SECONDS` plus
    `part_day_seconds` after the seeds' anchor, given `rate_limit_window_seconds`, that every rate
    limit window lasts that long, and that each bucket `rate_limit_by_bucket` names by its
    "<service>:<bucket>" name holds the limit given there in place of its own."""
    run_dir.mkdir()
    access_log_path = run_dir / "access_log.jsonl"
    stderr_path = run_dir / "launcher.stderr"
    # The launcher refuses a directory whose seeds state different anchors, so any one of them names it.
    anchor = json.loads(sorted(seed_dir.glob("*.json"))[0].read_text())["anchor"]
    start_ms = (datetime.fromisoformat(anchor).timestamp() + TEST_SHIFT_SECONDS + part_day_seconds) * 1000
    env = {
        **os.environ,
        "SEED_DIR": str(seed_dir),
        "CONTROL_PORT": "0",
        "SLACK_PORT": "0",
        "LINEAR_PORT": "0",
        "GOOGLE_PORT": "0",
        "ACCESS_LOG": str(access_log_path),
        "START_MS": str(round(start_ms)),
        "RATE_LIMIT_BUCKET_LIMITS": json.dumps(dict(rate_limit_by_bucket or {})),
    }
    if rate_limit_window_seconds is not None:
        env["RATE_LIMIT_WINDOW_SECONDS"] = str(rate_limit_window_seconds)
    ports: list[dict[str, int]] = []
    with stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            ["node", str(LAUNCHER_PATH)], cwd=run_dir, env=env, stdout=subprocess.PIPE, stderr=stderr, text=True
        )
    try:
        threading.Thread(target=_read_ready_line, args=(process.stdout, ports), daemon=True).start()
        _wait_until(lambda: bool(ports) or process.poll() is not None, _LAUNCHER_READY_TIMEOUT_SECONDS)
        assert ports, "the launcher did not serve (exit status {}): {}".format(process.poll(), stderr_path.read_text())
        urls = {name: "http://127.0.0.1:{}".format(port) for name, port in ports[0].items()}
        yield RunningFakes(control_url=urls.pop("control"), service_urls=urls, access_log_path=access_log_path)
    finally:
        process.terminate()
        try:
            process.wait(timeout=_LAUNCHER_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def _rewritten_seed_dir(
    tmp_path: Path, dirname: str, rewrite: Callable[[str, dict[str, Any]], dict[str, Any] | None]
) -> Path:
    """A directory of the fixture seeds, each passed through `rewrite(service, document)` first; a
    seed `rewrite` returns None for is left out."""
    seed_dir = tmp_path / dirname
    seed_dir.mkdir()
    for seed_path in sorted((FIXTURES_DIR / "seed").glob("*.json")):
        document = rewrite(seed_path.stem, json.loads(seed_path.read_text()))
        if document is not None:
            (seed_dir / seed_path.name).write_text(json.dumps(document))
    return seed_dir


@pytest.fixture(scope="session", autouse=True)
def launcher_install() -> None:
    """Node and the launcher's npm packages, which `npm ci` installs into this checkout."""
    if shutil.which("node") is None or not (REPO_DIR / "node_modules" / "@emulators" / "core").is_dir():
        pytest.fail("the launcher needs node on PATH and its packages installed: run `npm ci` in {}".format(REPO_DIR))


@pytest.fixture
def fakes(tmp_path: Path) -> Iterator[RunningFakes]:
    """The real launcher on the fixture seeds of every service."""
    yield from _running_fakes(tmp_path / "fakes", FIXTURES_DIR / "seed")


@pytest.fixture
def fakes_started_mid_day(tmp_path: Path) -> Iterator[RunningFakes]:
    """The real launcher on the fixture seeds of every service, told it started part of a day past
    `TEST_SHIFT_SECONDS` after their anchor."""
    yield from _running_fakes(tmp_path / "fakes-mid-day", FIXTURES_DIR / "seed", part_day_seconds=TEST_PART_DAY_SECONDS)


@pytest.fixture
def slack_only_fakes(tmp_path: Path) -> Iterator[RunningFakes]:
    """The real launcher on a directory holding the Slack fixture seed and no other, with the seed
    leaving `time_shift` out, so its timestamps are served as written."""
    seed_dir = _rewritten_seed_dir(
        tmp_path,
        "slack-only-seeds",
        lambda service, seed: (
            {key: value for key, value in seed.items() if key != "time_shift"} if service == "slack" else None
        ),
    )
    yield from _running_fakes(tmp_path / "slack-only-fakes", seed_dir)


@pytest.fixture
def unnamed_google_fakes(tmp_path: Path) -> Iterator[RunningFakes]:
    """The real launcher on the Google fixture seed alone, with every user's name left out."""
    seed_dir = _rewritten_seed_dir(
        tmp_path,
        "unnamed-google-seeds",
        lambda service, seed: (
            {**seed, "users": [{"email": user["email"]} for user in seed["users"]]} if service == "google" else None
        ),
    )
    yield from _running_fakes(tmp_path / "unnamed-google-fakes", seed_dir)


@pytest.fixture
def fakes_with_short_rate_limit_windows(tmp_path: Path) -> Iterator[RunningFakes]:
    """The real launcher on the fixture seeds of every service, every rate limit window lasting
    `TEST_RATE_LIMIT_WINDOW_SECONDS`."""
    yield from _running_fakes(
        tmp_path / "fakes-short-windows", FIXTURES_DIR / "seed", rate_limit_window_seconds=TEST_RATE_LIMIT_WINDOW_SECONDS
    )


@pytest.fixture
def fakes_with_a_small_linear_request_bucket(tmp_path: Path) -> Iterator[RunningFakes]:
    """The real launcher on the fixture seeds of every service, Linear's hourly request bucket
    holding `TEST_LINEAR_REQUEST_LIMIT` requests."""
    yield from _running_fakes(
        tmp_path / "fakes-small-linear-request-bucket",
        FIXTURES_DIR / "seed",
        rate_limit_by_bucket={"linear:requests": TEST_LINEAR_REQUEST_LIMIT},
    )


@pytest.fixture
def unlimited_fakes(tmp_path: Path) -> Iterator[RunningFakes]:
    """The real launcher on the fixture seeds of every service, each stating `"rate_limits": "none"`."""
    seed_dir = _rewritten_seed_dir(tmp_path, "unlimited-seeds", lambda service, seed: {**seed, "rate_limits": "none"})
    yield from _running_fakes(tmp_path / "unlimited-fakes", seed_dir)


@pytest.fixture
def faulted_fakes(tmp_path: Path) -> Iterator[RunningFakes]:
    """The real launcher on the fixture seeds of every service, the Slack seed stating `TEST_FAULTS`
    and the others leaving the schedule to it."""
    seed_dir = _rewritten_seed_dir(
        tmp_path, "faulted-seeds", lambda service, seed: {**seed, "faults": TEST_FAULTS} if service == "slack" else seed
    )
    yield from _running_fakes(tmp_path / "faulted-fakes", seed_dir)
