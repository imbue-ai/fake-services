"""The real launcher on small seeds. Skipped on a machine without node or the launcher's npm
packages (`just minds-evals-external-services-install`)."""

import json
import time
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from typing import Any

import httpx
import pytest

from imbue.minds_evals.conftest import EXTERNAL_SERVICES_TEST_RATE_LIMIT_WINDOW_SECONDS
from imbue.minds_evals.conftest import EXTERNAL_SERVICES_TEST_SHIFT_SECONDS
from imbue.minds_evals.conftest import RunningExternalServices
from imbue.minds_evals.errors import TaskAuthoringError
from imbue.minds_evals.external_services import SERVICES
from imbue.minds_evals.external_services import parse_service_seed
from imbue.minds_evals.external_services import parse_service_update
from imbue.minds_evals.external_services import service_named
from imbue.minds_evals.testing import EXTERNAL_SERVICES_REFUSED_UPDATE_FIXTURE
from imbue.minds_evals.testing import EXTERNAL_SERVICES_SEED_FIXTURE
from imbue.minds_evals.testing import EXTERNAL_SERVICES_UPDATE_FIXTURE
from imbue.minds_evals.testing import external_services_document
from imbue.minds_evals.testing import external_services_fixture_text
from imbue.mngr.utils.polling import wait_for

_SLACK_TOKEN = "xoxb-fake-redwood-slack-token"
_LINEAR_TOKEN = "lin_api_fakeredwoodlineartoken"
_SLACK_HEADERS = {"Authorization": "Bearer {}".format(_SLACK_TOKEN)}


def _slack(services: RunningExternalServices, method: str, **params: str) -> dict[str, Any]:
    return httpx.get("{}/api/{}".format(services.slack_url, method), params=params, headers=_SLACK_HEADERS).json()


def _linear(services: RunningExternalServices, query: str, token: str = _LINEAR_TOKEN) -> httpx.Response:
    return httpx.post(
        "{}/graphql".format(services.linear_url), json={"query": query}, headers={"Authorization": token}
    )


def _control(services: RunningExternalServices, route: str) -> Any:
    return httpx.get("{}/_control/{}".format(services.control_url, route)).json()


def _add(services: RunningExternalServices, service_name: str, update_text: str) -> httpx.Response:
    return httpx.post("{}/_control/records/{}".format(services.control_url, service_name), content=update_text)


def _issue_titles(services: RunningExternalServices) -> dict[str, str]:
    answer = _linear(services, "{ issues { nodes { identifier title } } }")
    return {issue["identifier"]: issue["title"] for issue in answer.json()["data"]["issues"]["nodes"]}


def _channel_id(services: RunningExternalServices, name: str) -> str:
    channels = _slack(services, "conversations.list", types="public_channel,private_channel")["channels"]
    return next(channel["id"] for channel in channels if channel["name"] == name)


def _shifted(ts: str) -> str:
    seconds, fraction = ts.split(".")
    return "{}.{}".format(int(seconds) + EXTERNAL_SERVICES_TEST_SHIFT_SECONDS, fraction)


def _shifted_iso(stamp: str) -> str:
    """A seed's timestamp as the launcher serves it under the test's shift."""
    moved = datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(
        seconds=EXTERNAL_SERVICES_TEST_SHIFT_SECONDS
    )
    return moved.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _shifted_seconds(ts: str) -> int:
    return int(ts.split(".")[0]) + EXTERNAL_SERVICES_TEST_SHIFT_SECONDS


# Every key under which a fake serves a record's date, and the instant each names in epoch seconds.
_DATE_KEYS = ("createdAt", "updatedAt", "completedAt", "startDate", "targetDate", "created", "updated", "last_set")


def _served_dates(answer: Any) -> Iterator[float]:
    if isinstance(answer, dict):
        for key, value in answer.items():
            if key in _DATE_KEYS and isinstance(value, str):
                yield datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            elif key in _DATE_KEYS and isinstance(value, int):
                yield float(value)
            else:
                yield from _served_dates(value)
    elif isinstance(answer, list):
        for item in answer:
            yield from _served_dates(item)
    else:
        # A value outside a date key is no date, and holds none.
        return


def _assert_no_date_is_near_the_wall_clock(*answers: Any) -> None:
    dates = [stamp for answer in answers for stamp in _served_dates(answer)]
    assert dates
    for stamp in dates:
        assert abs(stamp - time.time()) > 24 * 60 * 60, datetime.fromtimestamp(stamp).isoformat()


_LINEAR_DATES_QUERY = (
    "{ organization { createdAt updatedAt } users { nodes { email createdAt updatedAt } } "
    "teams { nodes { key createdAt updatedAt states { nodes { name createdAt updatedAt } } } } "
    "issueLabels { nodes { name createdAt updatedAt } } projects { nodes { name createdAt updatedAt } } "
    "cycles { nodes { name createdAt updatedAt } } }"
)


def _by(nodes: list[dict[str, Any]], key: str) -> dict[str, tuple[str, str]]:
    return {node[key]: (node["createdAt"], node["updatedAt"]) for node in nodes}


def test_the_stores_hold_the_seed_and_nothing_of_the_emulators_own(external_services: RunningExternalServices) -> None:
    identity = _slack(external_services, "auth.test")
    channels = _slack(external_services, "conversations.list")["channels"]
    members = _slack(external_services, "users.list")["members"]
    viewer = _linear(
        external_services, "{ viewer { id name email } organization { name urlKey } users { nodes { email } } }"
    )

    assert (identity["user"], identity["user_id"], identity["team"]) == (
        "morgan.reyes",
        "U0WLQRXH5FM",
        "Redwood Inference",
    )
    assert identity["url"] == "https://redwood.slack.com/"
    # The token's user posted in eng-oncall and never in announcements.
    assert {channel["name"]: channel["is_member"] for channel in channels} == {
        "announcements": False,
        "eng-oncall": True,
    }
    assert next(channel["id"] for channel in channels if channel["name"] == "announcements") == "C0Z2IPZAXKN"
    assert sorted(member["name"] for member in members) == ["claire", "morgan.reyes", "pagerbot"]
    data = viewer.json()["data"]
    assert data["viewer"] == {
        "id": "5485bf92-2976-5129-9b07-bd4c79ca0790",
        "name": "Morgan Reyes",
        "email": "morgan.reyes@redwood.example",
    }
    assert data["organization"] == {"name": "Redwood Inference", "urlKey": "redwood"}
    assert sorted(user["email"] for user in data["users"]["nodes"]) == [
        "morgan.reyes@redwood.example",
        "thomas.baird@redwood.example",
    ]
    # Nothing a workspace agent reads names the harness it is being measured by.
    for answer in (identity, channels, members, data):
        assert "eval" not in json.dumps(answer).lower()
        assert "emulate" not in json.dumps(answer).lower()


def _user_ids(services: RunningExternalServices) -> dict[str, str]:
    return {member["name"]: member["id"] for member in _slack(services, "users.list")["members"]}


def _members(services: RunningExternalServices, channel_name: str) -> set[str]:
    by_id = {user_id: name for name, user_id in _user_ids(services).items()}
    answer = _slack(services, "conversations.members", channel=_channel_id(services, channel_name))
    return {by_id[user_id] for user_id in answer["members"]}


def test_a_channels_members_are_the_users_who_posted_in_it_and_those_it_names(
    external_services: RunningExternalServices,
) -> None:
    seeded = {name: _members(external_services, name) for name in ("announcements", "eng-oncall")}
    added = _add(
        external_services,
        "slack",
        json.dumps(
            {
                "users": [{"name": "olivia_chen", "real_name": "Olivia Chen"}],
                "channels": [
                    {"name": "leads", "is_private": True, "members": ["claire"]},
                    {"name": "quiet", "members": ["pagerbot"]},
                ],
                "messages": [
                    {"channel": "eng-oncall", "user": "olivia_chen", "ts": "1774050000.000000", "text": "Drafted."},
                    {"channel": "leads", "user": "morgan.reyes", "ts": "1774060000.000000", "text": "Sync?"},
                ],
            }
        ),
    )

    assert added.status_code == 200, added.text
    assert seeded == {"announcements": {"claire"}, "eng-oncall": {"pagerbot", "claire", "morgan.reyes"}}
    assert _members(external_services, "eng-oncall") == {"pagerbot", "claire", "morgan.reyes", "olivia_chen"}
    assert _members(external_services, "leads") == {"claire", "morgan.reyes"}
    assert _members(external_services, "quiet") == {"pagerbot"}
    user_ids = _user_ids(external_services)
    channels = {channel["name"]: channel for channel in _slack(external_services, "conversations.list")["channels"]}
    assert {name: (channel["creator"], channel["num_members"]) for name, channel in channels.items()} == {
        "announcements": (user_ids["claire"], 1),
        "eng-oncall": (user_ids["pagerbot"], 4),
        "quiet": (user_ids["pagerbot"], 1),
    }
    # A public channel reads the same to a token whose user never posted in it, as with a Slack user
    # token; the private channel is listed only to its members.
    history = _slack(external_services, "conversations.history", channel=channels["announcements"]["id"])
    assert [message["user"] for message in history["messages"]] == [user_ids["claire"]]
    private = _slack(external_services, "conversations.list", types="private_channel")["channels"]
    assert [channel["name"] for channel in private] == ["leads"]


def test_timestamps_are_shifted_by_the_start_minus_the_anchor(external_services: RunningExternalServices) -> None:
    channel_id = _channel_id(external_services, "eng-oncall")

    history = _slack(external_services, "conversations.history", channel=channel_id)
    issue = _linear(
        external_services, '{ issue(id: "ENG-2") { createdAt updatedAt comments { nodes { createdAt } } } }'
    )

    assert _control(external_services, "health")["shift_ms"] == EXTERNAL_SERVICES_TEST_SHIFT_SECONDS * 1000
    # Newest first, the reply folded into its thread, each ts the seed's plus the shift.
    assert [message["ts"] for message in history["messages"]] == [
        _shifted("1774000000.000000"),
        _shifted("1773900000.000000"),
    ]
    assert history["messages"][1]["reply_count"] == 1
    assert issue.json()["data"]["issue"] == {
        "createdAt": "2026-10-05T09:00:00.000Z",
        "updatedAt": "2026-10-06T12:00:00.000Z",
        "comments": {"nodes": [{"createdAt": "2026-10-06T12:00:00.000Z"}]},
    }


def test_every_linear_date_is_the_seeds_or_derived_from_its_records(
    external_services: RunningExternalServices,
) -> None:
    """Only issues and comments state their dates in the fixture seed, besides Morgan's creation: a
    project, cycle, user, label, team and state is dated from the issues and comments around it, and
    the organization from the earliest record of all. Morgan was last updated by the first issue
    they created."""
    data = _linear(external_services, _LINEAR_DATES_QUERY).json()["data"]

    first_issue = _shifted_iso("2026-03-02T09:00:00.000Z")
    assert data["organization"] == {"createdAt": _shifted_iso("2026-01-05T09:00:00.000Z")} | {
        "updatedAt": _shifted_iso("2026-01-05T09:00:00.000Z")
    }
    assert _by(data["users"]["nodes"], "email") == {
        "morgan.reyes@redwood.example": (_shifted_iso("2026-01-05T09:00:00.000Z"), first_issue),
        "thomas.baird@redwood.example": (first_issue, first_issue),
    }
    (team,) = data["teams"]["nodes"]
    assert (team["createdAt"], team["updatedAt"]) == (first_issue, first_issue)
    assert set(_by(team["states"]["nodes"], "name").values()) == {(first_issue, first_issue)}
    assert _by(data["issueLabels"]["nodes"], "name") == {"telemetry": (first_issue, first_issue)}
    # The project's latest record is ENG-2's update and its comment; the cycle's is ENG-1's update.
    assert _by(data["projects"]["nodes"], "name") == {
        "Usage insights": (first_issue, _shifted_iso("2026-03-20T12:00:00.000Z"))
    }
    assert _by(data["cycles"]["nodes"], "name") == {
        "2026-W11": (_shifted_iso("2026-03-09T00:00:00.000Z"), _shifted_iso("2026-03-12T17:00:00.000Z"))
    }
    _assert_no_date_is_near_the_wall_clock(data)


def test_an_update_moves_a_linear_records_update_forward_and_never_its_creation_back(
    external_services: RunningExternalServices,
) -> None:
    before = _linear(external_services, _LINEAR_DATES_QUERY).json()["data"]
    added = _add(
        external_services,
        "linear",
        json.dumps(
            {
                "labels": [{"name": "billing", "created_at": "2025-12-01T10:00:00.000Z"}],
                "issues": [
                    {
                        "identifier": "ENG-30",
                        "team": "ENG",
                        "title": "Backdated import",
                        "state": "In Progress",
                        "assignee": "Thomas Baird",
                        "project": "Usage insights",
                        "cycle": "2026-W11",
                        "created_at": "2026-02-01T09:00:00.000Z",
                        "updated_at": "2026-03-20T23:00:00.000Z",
                    }
                ],
            }
        ),
    )
    after = _linear(external_services, _LINEAR_DATES_QUERY).json()["data"]

    assert added.status_code == 200, added.text
    # The new issue is older than everything the project, the cycle, Thomas and the team were dated
    # from, and newer than their updates: each keeps its creation and moves its update forward.
    newest = _shifted_iso("2026-03-20T23:00:00.000Z")
    assert _by(after["projects"]["nodes"], "name")["Usage insights"] == (
        _by(before["projects"]["nodes"], "name")["Usage insights"][0],
        newest,
    )
    assert _by(after["cycles"]["nodes"], "name")["2026-W11"] == (
        _by(before["cycles"]["nodes"], "name")["2026-W11"][0],
        newest,
    )
    assert _by(after["users"]["nodes"], "email") == _by(before["users"]["nodes"], "email")
    assert (after["teams"]["nodes"][0]["createdAt"], after["organization"]) == (
        before["teams"]["nodes"][0]["createdAt"],
        before["organization"],
    )
    # A record the update adds takes the date its file states.
    assert _by(after["issueLabels"]["nodes"], "name")["billing"] == (_shifted_iso("2025-12-01T10:00:00.000Z"),) * 2
    _assert_no_date_is_near_the_wall_clock(after)


def test_a_record_an_update_adds_takes_the_dates_its_file_states(
    external_services: RunningExternalServices,
) -> None:
    """A team keeps its stated dates though placing an issue on it advances its issue sequence, and a
    stated last update earlier than the creation is served as the creation."""
    added = _add(
        external_services,
        "linear",
        json.dumps(
            {
                "teams": [
                    {
                        "key": "OPS",
                        "name": "Operations",
                        "states": [{"name": "Todo", "type": "unstarted", "position": 1}],
                        "created_at": "2025-11-03T09:00:00.000Z",
                        "updated_at": "2026-02-14T09:00:00.000Z",
                    }
                ],
                "labels": [
                    {
                        "name": "billing",
                        "created_at": "2025-12-01T10:00:00.000Z",
                        "updated_at": "2025-11-01T10:00:00.000Z",
                    }
                ],
                "issues": [
                    {
                        "identifier": "OPS-7",
                        "team": "OPS",
                        "title": "Rotate the pager",
                        "state": "Todo",
                        "created_at": "2026-03-10T09:00:00.000Z",
                    }
                ],
            }
        ),
    )
    data = _linear(external_services, _LINEAR_DATES_QUERY).json()["data"]

    assert added.status_code == 200, added.text
    assert _by(data["teams"]["nodes"], "key")["OPS"] == (
        _shifted_iso("2025-11-03T09:00:00.000Z"),
        _shifted_iso("2026-02-14T09:00:00.000Z"),
    )
    assert _by(data["issueLabels"]["nodes"], "name")["billing"] == (_shifted_iso("2025-12-01T10:00:00.000Z"),) * 2


def test_a_project_serves_its_lead_and_dates_from_the_seed_or_its_issues(
    external_services: RunningExternalServices,
) -> None:
    added = _add(
        external_services,
        "linear",
        json.dumps(
            {
                "projects": [
                    {
                        "name": "Billing cleanup",
                        "state": "completed",
                        "description": "Invoices without drift.",
                        "lead": "thomas.baird@redwood.example",
                        "start_date": "2026-03-01",
                        "target_date": "2026-03-31",
                        "created_at": "2026-02-20T09:00:00.000Z",
                    },
                    {"name": "Legacy exports", "state": "canceled", "created_at": "2026-01-12T09:00:00.000Z"},
                    {"name": "Usage alerts", "state": "started", "created_at": "2026-02-25T09:00:00.000Z"},
                ],
                "issues": [
                    {
                        "identifier": "ENG-42",
                        "team": "ENG",
                        "title": "Alert on quota",
                        "state": "Done",
                        "project": "Usage alerts",
                        "created_at": "2026-03-05T09:00:00.000Z",
                        "updated_at": "2026-03-06T15:00:00.000Z",
                    },
                    {
                        "identifier": "ENG-40",
                        "team": "ENG",
                        "title": "Round line totals",
                        "state": "Done",
                        "project": "Billing cleanup",
                        "created_at": "2026-03-03T09:00:00.000Z",
                        "updated_at": "2026-03-18T15:00:00.000Z",
                    },
                    {
                        "identifier": "ENG-41",
                        "team": "ENG",
                        "title": "Drop the legacy rounding",
                        "state": "Done",
                        "project": "Billing cleanup",
                        "created_at": "2026-03-04T09:00:00.000Z",
                        "updated_at": "2026-03-19T15:00:00.000Z",
                    },
                ],
            }
        ),
    )
    answer = _linear(
        external_services,
        "{ projects { nodes { name description state lead { name } startDate targetDate completedAt canceledAt "
        "createdAt updatedAt progress } } "
        'team(id: "ENG") { issueCount cycles { nodes { name progress } } } }',
    )

    assert added.status_code == 200, added.text
    assert answer.status_code == 200 and "errors" not in answer.json(), answer.text
    data = answer.json()["data"]
    projects = {project.pop("name"): project for project in data["projects"]["nodes"]}
    # The seed's project states none of these: its dates are its issues', it has no lead, and one of
    # its two issues is still in progress.
    assert projects["Usage insights"] == {
        "description": "",
        "state": "started",
        "lead": None,
        "startDate": _shifted_iso("2026-03-02T09:00:00.000Z")[:10],
        "targetDate": _shifted_iso("2026-03-13T00:00:00.000Z")[:10],
        "completedAt": None,
        "canceledAt": None,
        "createdAt": _shifted_iso("2026-03-02T09:00:00.000Z"),
        "updatedAt": _shifted_iso("2026-03-20T12:00:00.000Z"),
        "progress": 0.5,
    }
    assert projects["Billing cleanup"] == {
        "description": "Invoices without drift.",
        "state": "completed",
        "lead": {"name": "Thomas Baird"},
        "startDate": _shifted_iso("2026-03-01T00:00:00.000Z")[:10],
        "targetDate": _shifted_iso("2026-03-31T00:00:00.000Z")[:10],
        "completedAt": _shifted_iso("2026-03-19T15:00:00.000Z"),
        "canceledAt": None,
        "createdAt": _shifted_iso("2026-02-20T09:00:00.000Z"),
        "updatedAt": _shifted_iso("2026-03-19T15:00:00.000Z"),
        "progress": 1,
    }
    # A project has a completedAt or canceledAt only in that state, whatever its issues are.
    assert (projects["Usage alerts"]["completedAt"], projects["Usage alerts"]["canceledAt"]) == (None, None)
    assert (projects["Legacy exports"]["completedAt"], projects["Legacy exports"]["canceledAt"]) == (
        None,
        projects["Legacy exports"]["updatedAt"],
    )
    assert projects["Legacy exports"]["updatedAt"] == _shifted_iso("2026-01-12T09:00:00.000Z")
    assert data["team"] == {"issueCount": 5, "cycles": {"nodes": [{"name": "2026-W11", "progress": 1}]}}
    assert _control(external_services, "health")["unsupported_calls"] == {}


def test_every_slack_date_is_the_seeds_or_derived_from_its_messages(
    external_services: RunningExternalServices,
) -> None:
    added = _add(
        external_services,
        "slack",
        json.dumps(
            {
                "users": [
                    {"name": "olivia_chen", "real_name": "Olivia Chen"},
                    {"name": "sam", "real_name": "Sam Ito", "created": "2026-02-10T08:00:00.000Z"},
                ],
                "channels": [
                    {"name": "planning", "created": "2026-02-02T09:30:00.000Z"},
                    {"name": "quiet", "members": ["sam"]},
                ],
                "messages": [
                    {"channel": "planning", "user": "olivia_chen", "ts": "1774050000.000000", "text": "Q2 draft."},
                ],
            }
        ),
    )
    channels = {
        channel["name"]: channel
        for channel in _slack(external_services, "conversations.list", types="public_channel,private_channel")[
            "channels"
        ]
    }
    users = {member["name"]: member for member in _slack(external_services, "users.list")["members"]}

    assert added.status_code == 200, added.text
    # A channel is created at its first message unless its file says otherwise, and one with no
    # message at the earliest message of all; its topic and purpose were set when it was created.
    planning_created = int(datetime.fromisoformat("2026-02-02T09:30:00+00:00").timestamp())
    expected_created = {
        "announcements": _shifted_seconds("1769123456.000000"),
        "eng-oncall": _shifted_seconds("1773900000.000000"),
        "planning": planning_created + EXTERNAL_SERVICES_TEST_SHIFT_SECONDS,
        "quiet": _shifted_seconds("1769123456.000000"),
    }
    assert {name: channel["created"] for name, channel in channels.items()} == expected_created
    assert {name: channel["topic"]["last_set"] for name, channel in channels.items()} == expected_created
    assert {name: channel["purpose"]["last_set"] for name, channel in channels.items()} == expected_created
    # A user was last updated at their first message unless their file says otherwise, and one
    # without a message at the earliest message of all.
    sam_created = int(datetime.fromisoformat("2026-02-10T08:00:00+00:00").timestamp())
    assert {name: user["updated"] for name, user in users.items()} == {
        "claire": _shifted_seconds("1769123456.000000"),
        "pagerbot": _shifted_seconds("1773900000.000000"),
        "morgan.reyes": _shifted_seconds("1774000000.000000"),
        "olivia_chen": _shifted_seconds("1774050000.000000"),
        "sam": sam_created + EXTERNAL_SERVICES_TEST_SHIFT_SECONDS,
    }
    info = _slack(external_services, "users.info", user=users["claire"]["id"])
    assert info["user"]["updated"] == _shifted_seconds("1769123456.000000")
    _assert_no_date_is_near_the_wall_clock(channels, users)


def test_a_shift_to_the_trials_start_is_a_whole_number_of_days(
    external_services_started_mid_day: RunningExternalServices,
) -> None:
    """The launcher is told it started part of a day past a whole number of days after the anchor,
    and drops that part, so every timestamp keeps its weekday and its time of day."""
    services = external_services_started_mid_day
    issue = _linear(services, '{ issue(id: "ENG-2") { createdAt } }')
    history = _slack(services, "conversations.history", channel=_channel_id(services, "eng-oncall"))

    assert _control(services, "health")["shift_ms"] == EXTERNAL_SERVICES_TEST_SHIFT_SECONDS * 1000
    assert issue.json()["data"]["issue"]["createdAt"] == _shifted_iso("2026-03-19T09:00:00.000Z")
    assert history["messages"][0]["ts"] == _shifted("1774000000.000000")


def test_history_takes_oldest_and_latest(external_services: RunningExternalServices) -> None:
    channel_id = _channel_id(external_services, "eng-oncall")
    boundary = _shifted("1773900000.000000")

    newer = _slack(external_services, "conversations.history", channel=channel_id, oldest=boundary)
    inclusive = _slack(
        external_services, "conversations.history", channel=channel_id, oldest=boundary, inclusive="true"
    )
    older = _slack(
        external_services, "conversations.history", channel=channel_id, latest=_shifted("1774000000.000000")
    )

    assert [message["ts"] for message in newer["messages"]] == [_shifted("1774000000.000000")]
    assert len(inclusive["messages"]) == 2
    assert [message["ts"] for message in older["messages"]] == [boundary]
    first_page = _slack(
        external_services, "conversations.history", channel=channel_id, oldest=boundary, inclusive="true", limit="1"
    )
    second_page = _slack(
        external_services,
        "conversations.history",
        channel=channel_id,
        oldest=boundary,
        inclusive="true",
        limit="1",
        cursor=first_page["response_metadata"]["next_cursor"],
    )
    stale = _slack(external_services, "conversations.history", channel=channel_id, oldest=boundary, cursor="1.000000")
    assert [message["ts"] for message in first_page["messages"] + second_page["messages"]] == [
        _shifted("1774000000.000000"),
        boundary,
    ]
    assert (second_page["has_more"], stale) == (False, {"ok": False, "error": "invalid_cursor"})


def test_search_finds_messages_by_text_and_channel(external_services: RunningExternalServices) -> None:
    found = _slack(external_services, "search.messages", query="latency in:#eng-oncall")

    assert found["messages"]["total"] == 2
    assert {match["channel"]["name"] for match in found["messages"]["matches"]} == {"eng-oncall"}
    assert _slack(external_services, "search.messages", query="latency in:#announcements")["messages"]["total"] == 0


def test_linear_answers_a_nested_filter_ordered_by_update(external_services: RunningExternalServices) -> None:
    answer = _linear(
        external_services,
        '{ issues(filter: {team: {key: {eq: "ENG"}}, state: {type: {neq: "completed"}}, '
        'assignee: {name: {containsIgnoreCase: "baird"}}}, orderBy: updatedAt) '
        "{ nodes { identifier estimate state { name } inverseRelations { nodes { type issue { identifier } } } } } }",
    )

    assert answer.status_code == 200
    assert answer.json()["data"]["issues"]["nodes"] == [
        {
            "identifier": "ENG-2",
            "estimate": 3,
            "state": {"name": "In Progress"},
            "inverseRelations": {"nodes": [{"type": "blocks", "issue": {"identifier": "ENG-1"}}]},
        }
    ]


def test_a_date_filter_takes_a_duration_from_the_trials_present(external_services: RunningExternalServices) -> None:
    """ENG-1 was created 18 days and 15 hours before the anchor and ENG-2 a day and 15 hours before,
    and the anchor is shifted to the trial's start."""

    def created_within(duration: str) -> list[str]:
        answer = _linear(
            external_services,
            '{ issues(filter: {createdAt: {gt: "%s"}}) { nodes { identifier } } }' % duration,
        )
        return sorted(issue["identifier"] for issue in answer.json()["data"]["issues"]["nodes"])

    assert created_within("-P1W") == ["ENG-2"]
    assert created_within("-P2W4DT12H") == ["ENG-2"]
    assert created_within("-P2W4DT16H") == ["ENG-1", "ENG-2"]
    assert created_within("-P3W") == ["ENG-1", "ENG-2"]
    assert created_within("-P1M") == ["ENG-1", "ENG-2"]
    assert created_within("P1D") == []
    assert _control(external_services, "health")["unsupported_calls"] == {}


def test_the_active_cycle_is_the_one_spanning_the_trials_present(external_services: RunningExternalServices) -> None:
    """The launcher is told the trial started 200 days after the anchor, so the cycle spanning the
    anchor is active, whatever day the wall clock reads."""
    added = _add(
        external_services,
        "linear",
        json.dumps(
            {
                "cycles": [
                    {
                        "team": "ENG",
                        "name": "2026-W12",
                        "number": 12,
                        "starts_at": "2026-03-20T00:00:00.000Z",
                        "ends_at": "2026-03-27T00:00:00.000Z",
                    }
                ]
            }
        ),
    )

    answer = _linear(
        external_services,
        '{ team(id: "ENG") { activeCycle { name } } cycles(filter: {isActive: {eq: true}}) { nodes { name } } }',
    )

    assert added.status_code == 200, added.text
    assert answer.json()["data"] == {
        "team": {"activeCycle": {"name": "2026-W12"}},
        "cycles": {"nodes": [{"name": "2026-W12"}]},
    }


def test_only_a_seeded_token_authenticates(external_services: RunningExternalServices) -> None:
    slack_answer = httpx.get(
        "{}/api/conversations.list".format(external_services.slack_url), headers={"Authorization": "Bearer xoxb-other"}
    )
    linear_answer = _linear(external_services, "{ viewer { name } }", token="lin_api_other")
    anonymous = httpx.get("{}/api/conversations.list".format(external_services.slack_url))

    assert slack_answer.json()["ok"] is False
    assert anonymous.json() == {"ok": False, "error": "not_authed"}
    assert linear_answer.status_code == 401


def test_an_update_lands_on_its_own_service_with_the_seeds_shift(external_services: RunningExternalServices) -> None:
    slack_added = _add(
        external_services, "slack", external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "slack")
    )
    after_slack = _control(external_services, "health")["counts"]
    linear_added = _add(
        external_services, "linear", external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "linear")
    )

    assert (slack_added.status_code, slack_added.json()["added"]) == (200, {"slack_messages": 1})
    # Each service's update is its own request: Slack's left Linear holding what it was seeded with.
    assert (after_slack["slack"]["messages"], after_slack["linear"]["issues"]) == (5, 2)
    assert (linear_added.status_code, linear_added.json()["added"]) == (
        200,
        {"linear_issues": 1, "linear_comments": 0},
    )
    history = _slack(external_services, "conversations.history", channel=_channel_id(external_services, "eng-oncall"))
    assert history["messages"][0]["ts"] == _shifted("1774050000.000000")
    issue = _linear(external_services, '{ issue(id: "ENG-3") { title createdAt labels { nodes { name } } } }')
    assert issue.json()["data"]["issue"] == {
        "title": "Invoice line alignment",
        "createdAt": "2026-10-06T18:00:00.000Z",
        "labels": {"nodes": [{"name": "billing"}]},
    }


def test_a_refused_update_leaves_its_store_as_it_was(external_services: RunningExternalServices) -> None:
    before = _control(external_services, "health")["counts"]

    refused = _add(
        external_services,
        "linear",
        external_services_fixture_text(EXTERNAL_SERVICES_REFUSED_UPDATE_FIXTURE, "linear"),
    )

    assert refused.status_code == 400
    assert "linear state not found: No Such State" in refused.json()["error"]
    # The update's first issue was placeable and went in before its second was refused, and is gone again.
    assert _control(external_services, "health")["counts"] == before
    assert sorted(_issue_titles(external_services)) == ["ENG-1", "ENG-2"]
    # The store still takes an update after one was rolled back.
    retried = _add(
        external_services, "linear", external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "linear")
    )
    assert retried.status_code == 200
    assert _control(external_services, "health")["counts"]["linear"]["issues"] == before["linear"]["issues"] + 1


def test_an_update_goes_to_a_served_service_by_name_and_nowhere_else(
    external_services: RunningExternalServices,
) -> None:
    update_text = external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "slack")
    before = _control(external_services, "health")["counts"]

    unnamed = httpx.post("{}/_control/records".format(external_services.control_url), content=update_text)
    unknown = _add(external_services, "github", update_text)

    assert (unnamed.status_code, unnamed.json()) == (404, {"error": "unknown control route"})
    assert (unknown.status_code, unknown.json()["error"]) == (
        400,
        "the github service was not seeded, so it takes no update",
    )
    assert _control(external_services, "health")["counts"] == before


def test_the_control_endpoints_are_on_the_control_port_only(external_services: RunningExternalServices) -> None:
    """A workspace's requests are pointed at the service ports, so nothing there may dump the stores,
    read the log, or add records."""
    before = _control(external_services, "health")["counts"]
    update_text = external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "slack")

    answers = [
        httpx.get("{}/_control/{}".format(service_url, route), headers=headers)
        for service_url, headers in (
            (external_services.slack_url, _SLACK_HEADERS),
            (external_services.linear_url, {"Authorization": _LINEAR_TOKEN}),
        )
        for route in ("health", "state", "log")
    ]
    answers.append(
        httpx.post(
            "{}/_control/records/slack".format(external_services.slack_url),
            content=update_text,
            headers=_SLACK_HEADERS,
        )
    )

    for answer in answers:
        assert answer.status_code == 404, (answer.request.url, answer.text[:200])
        assert not {"counts", "shift_ms", "added", "dumped_at"} & set(answer.json())
    after = _control(external_services, "health")["counts"]
    assert (after["slack"], after["linear"]) == (before["slack"], before["linear"])
    # Unlike a request to the control port, each of these reached a fake and is in its log.
    assert after["requests"] == {"slack": 4, "linear": 3}


def test_a_directory_holding_one_seed_serves_that_service_alone(
    slack_only_external_services: RunningExternalServices,
) -> None:
    health = _control(slack_only_external_services, "health")
    dump = _control(slack_only_external_services, "state")
    refused = _add(
        slack_only_external_services,
        "linear",
        external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "linear"),
    )

    assert sorted(slack_only_external_services.service_urls) == ["slack"]
    assert health["services"] == ["slack"]
    assert sorted(health["counts"]) == ["requests", "slack"]
    assert health["counts"]["requests"] == {"slack": 0}
    assert "linear" not in dump
    assert (refused.status_code, refused.json()["error"]) == (
        400,
        "the linear service was not seeded, so it takes no update",
    )
    assert _slack(slack_only_external_services, "auth.test")["user"] == "morgan.reyes"


def test_a_seed_that_leaves_the_time_shift_out_is_served_as_written(
    slack_only_external_services: RunningExternalServices,
) -> None:
    added = _add(
        slack_only_external_services,
        "slack",
        external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "slack"),
    )
    history = _slack(
        slack_only_external_services,
        "conversations.history",
        channel=_channel_id(slack_only_external_services, "eng-oncall"),
    )

    assert added.status_code == 200
    assert _control(slack_only_external_services, "health")["shift_ms"] == 0
    assert _control(slack_only_external_services, "state")["shift_ms"] == 0
    # The update's message is the newest, and it too keeps the timestamp its file states.
    assert [message["ts"] for message in history["messages"]] == [
        "1774050000.000000",
        "1774000000.000000",
        "1773900000.000000",
    ]


def test_a_request_for_a_services_other_host_is_not_found_and_counted(
    external_services: RunningExternalServices,
) -> None:
    """The curl shim points every host of a service at its fake and names the host the request was
    for; only the API host is served."""
    slack_file = httpx.get(
        "{}/files-pri/T1-F1/report.pdf".format(external_services.slack_url),
        headers={**_SLACK_HEADERS, "X-Forwarded-Host": "files.slack.com"},
    )
    # The path is one the API host serves, so only the named host can be what refuses it.
    sibling_api = httpx.get(
        "{}/api/auth.test".format(external_services.slack_url),
        headers={**_SLACK_HEADERS, "X-Forwarded-Host": "redwood.slack.com"},
    )
    linear_upload = httpx.post(
        "{}/graphql".format(external_services.linear_url),
        json={"query": "{ viewer { name } }"},
        headers={"Authorization": _LINEAR_TOKEN, "X-Forwarded-Host": "uploads.linear.app"},
    )
    primary = httpx.get(
        "{}/api/auth.test".format(external_services.slack_url),
        headers={**_SLACK_HEADERS, "X-Forwarded-Host": "slack.com"},
    )

    assert (slack_file.status_code, slack_file.json()) == (404, {"ok": False, "error": "unknown_method"})
    assert (sibling_api.status_code, sibling_api.json()) == (404, {"ok": False, "error": "unknown_method"})
    assert (linear_upload.status_code, linear_upload.json()) == (404, {"errors": [{"message": "Not Found"}]})
    assert (primary.status_code, primary.json()["user"]) == (200, "morgan.reyes")
    expected_counts = {
        "slack:host:files.slack.com": 1,
        "slack:host:redwood.slack.com": 1,
        "linear:host:uploads.linear.app": 1,
    }
    assert _control(external_services, "health")["unsupported_calls"] == expected_counts
    assert _control(external_services, "state")["unsupported_calls"] == expected_counts
    assert [(line["url"], line["unsupported"]) for line in _control(external_services, "log")] == [
        ("https://files.slack.com/files-pri/T1-F1/report.pdf", "slack:host:files.slack.com"),
        ("https://redwood.slack.com/api/auth.test", "slack:host:redwood.slack.com"),
        ("https://uploads.linear.app/graphql", "linear:host:uploads.linear.app"),
        ("https://slack.com/api/auth.test", None),
    ]


def test_a_public_linear_field_or_argument_the_fake_lacks_is_answered_empty_and_counted(
    external_services: RunningExternalServices,
) -> None:
    """Linear's public schema validates every query, so a field or argument Linear has and the fake
    does not is answered as its type's empty value and counted, while one Linear itself does not have
    is refused as Linear refuses it, uncounted: that is the caller's mistake, not the fake's gap."""
    nullable = _linear(external_services, "{ viewer { name timezone } }")
    _linear(external_services, "{ viewer { timezone } }")
    non_null = _linear(external_services, "{ viewer { guest organization { name } } projects { nodes { slugId } } }")
    root = _linear(external_services, "{ initiatives { nodes { name } pageInfo { hasNextPage } } }")
    argument = _linear(external_services, "{ issues(sort: [{priority: {order: Descending}}]) { nodes { id } } }")
    filtered = _linear(external_services, "{ issues(filter: {slaStatus: {eq: Breached}}) { nodes { id } } }")
    _linear(external_services, "{ viewer { name } }")
    no_such_field = _linear(external_services, "{ viewer { noSuchField } }")
    no_such_argument = _linear(external_services, "{ issues(noSuchArgument: 1) { nodes { id } } }")
    _linear(external_services, "{ viewer { name }")

    assert nullable.status_code == 200 and "errors" not in nullable.json()
    assert nullable.json()["data"]["viewer"]["timezone"] is None
    assert non_null.json()["data"]["viewer"] == {"guest": False, "organization": {"name": ""}}
    project_nodes = non_null.json()["data"]["projects"]["nodes"]
    assert project_nodes and all(node == {"slugId": ""} for node in project_nodes)
    assert root.json() == {"data": {"initiatives": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}
    assert argument.status_code == 200 and argument.json()["data"]["issues"]["nodes"]
    assert filtered.json()["data"] is None
    assert filtered.json()["errors"][0]["message"] == 'Field "slaStatus" is not defined by type "IssueFilter".'
    assert no_such_field.status_code == 400
    assert no_such_field.json()["errors"][0]["message"].startswith('Cannot query field "noSuchField" on type "User"')
    assert no_such_argument.status_code == 400
    assert _control(external_services, "health")["unsupported_calls"] == {
        "linear:field:User.timezone": 2,
        "linear:field:User.guest": 1,
        "linear:field:User.organization": 1,
        "linear:field:Project.slugId": 1,
        "linear:field:Query.initiatives": 1,
        "linear:argument:Query.issues.sort": 1,
        "linear:filter:IssueFilter.slaStatus": 1,
    }
    assert [line["unsupported"] for line in _control(external_services, "log")] == [
        "linear:field:User.timezone",
        "linear:field:User.timezone",
        "linear:field:User.guest",
        "linear:field:Query.initiatives",
        "linear:argument:Query.issues.sort",
        "linear:filter:IssueFilter.slaStatus",
        None,
        None,
        None,
        None,
    ]


def test_an_argument_is_counted_by_the_field_that_ignores_it_and_a_schema_default_is_not(
    external_services: RunningExternalServices,
) -> None:
    """`includeDisabled` is read by no resolver; `includeArchived` is read where issues are listed
    and nowhere else; `includeSubTeams: false` is what the schema gives a caller who says nothing."""
    answer = _linear(
        external_services,
        '{ team(id: "ENG") { members(includeDisabled: true) { nodes { name } } '
        "issues(includeArchived: true, includeSubTeams: false) { nodes { identifier } } "
        "projects(includeArchived: true) { nodes { name } } } }",
    )

    assert answer.status_code == 200 and "errors" not in answer.json()
    assert len(answer.json()["data"]["team"]["issues"]["nodes"]) == 2
    assert _control(external_services, "health")["unsupported_calls"] == {
        "linear:argument:Team.members.includeDisabled": 1,
        "linear:argument:Team.projects.includeArchived": 1,
    }


def test_an_argument_the_fake_serves_is_not_counted_when_no_record_is_listed(
    external_services: RunningExternalServices,
) -> None:
    """The viewer is assigned no issue, so nothing is filtered: the arguments are served all the same."""
    answer = _linear(
        external_services,
        '{ viewer { assignedIssues(filter: {title: {contains: "x"}}, includeArchived: true, orderBy: updatedAt) '
        "{ nodes { id } } } }",
    )

    assert answer.status_code == 200 and "errors" not in answer.json()
    assert answer.json()["data"]["viewer"]["assignedIssues"]["nodes"] == []
    assert _control(external_services, "health")["unsupported_calls"] == {}


def test_a_nested_filter_names_the_type_linear_names(external_services: RunningExternalServices) -> None:
    refused = _linear(
        external_services,
        '{ issues(filter: {assignee: {createdAt: {lt: "P1D"}}, labels: {some: {isGroup: {eq: true}}}}) { nodes { id } } }',
    )

    assert refused.status_code == 200 and refused.json()["data"] is None
    assert refused.json()["errors"][0]["message"] == 'Field "createdAt" is not defined by type "NullableUserFilter".'
    assert _control(external_services, "health")["unsupported_calls"] == {
        "linear:filter:NullableUserFilter.createdAt": 1,
    }


def test_every_graphql_request_goes_through_the_public_schema(external_services: RunningExternalServices) -> None:
    """A mutation is refused as Linear's SDK reads a 403 and counted; a GET carries its document in
    the URL and is validated like a POST. Neither reaches the emulator's own schema."""
    before = _control(external_services, "health")["counts"]["linear"]

    refused = _linear(
        external_services,
        'mutation { issueCreate(input: {teamId: "ENG", title: "x"}) { success } commentCreate(input: {body: "y"}) { success } }',
    )
    got = httpx.get(
        "{}/graphql".format(external_services.linear_url),
        params={"query": "{ viewer { name } }"},
        headers={"Authorization": _LINEAR_TOKEN},
    )
    invalid_get = httpx.get(
        "{}/graphql".format(external_services.linear_url),
        params={"query": "{ viewer { noSuchField } }"},
        headers={"Authorization": _LINEAR_TOKEN},
    )
    unvalidated_write = _linear(external_services, "mutation { noSuchMutation { success } }")

    assert refused.status_code == 403
    assert refused.json()["errors"][0]["extensions"]["type"] == "forbidden"
    assert "data" not in refused.json()
    assert (got.status_code, got.json()["data"]["viewer"]["name"]) == (200, "Morgan Reyes")
    assert invalid_get.status_code == 400
    assert unvalidated_write.status_code == 400
    assert _control(external_services, "health")["counts"]["linear"] == before
    assert _control(external_services, "health")["unsupported_calls"] == {
        "linear:mutation:issueCreate": 1,
        "linear:mutation:commentCreate": 1,
    }
    assert [line["unsupported"] for line in _control(external_services, "log")] == [
        "linear:mutation:issueCreate",
        None,
        None,
        None,
    ]


def test_a_write_is_refused_only_when_it_is_the_operation_that_would_run(
    external_services: RunningExternalServices,
) -> None:
    document = (
        'mutation Write { issueCreate(input: {teamId: "ENG", title: "x"}) { success } } query Read { viewer { name } }'
    )

    def post(operation_name: str) -> httpx.Response:
        return httpx.post(
            "{}/graphql".format(external_services.linear_url),
            json={"query": document, "operationName": operation_name},
            headers={"Authorization": _LINEAR_TOKEN},
        )

    unknown = post("Nope")
    read = post("Read")
    write = post("Write")

    assert unknown.status_code == 400
    assert unknown.json()["errors"][0]["message"] == 'Unknown operation named "Nope".'
    assert read.json()["data"]["viewer"]["name"] == "Morgan Reyes"
    through_fragment = _linear(
        external_services,
        'mutation { ...Archive } fragment Archive on Mutation { issueArchive(id: "ENG-1") { success } }',
    )

    assert write.status_code == 403
    assert through_fragment.status_code == 403
    assert _control(external_services, "health")["unsupported_calls"] == {
        "linear:mutation:issueCreate": 1,
        "linear:mutation:issueArchive": 1,
    }


def test_a_query_missing_its_required_variable_is_refused_with_400(external_services: RunningExternalServices) -> None:
    answer = _linear(external_services, "query One($id: String!) { issue(id: $id) { identifier } }")

    assert answer.status_code == 400
    assert "data" not in answer.json()
    assert answer.json()["errors"][0]["message"].startswith('Variable "$id" has invalid value')


def test_a_get_carries_its_variables_as_json_in_the_url(external_services: RunningExternalServices) -> None:
    def get(variables: str) -> httpx.Response:
        return httpx.get(
            "{}/graphql".format(external_services.linear_url),
            params={"query": "query One($id: String!) { issue(id: $id) { identifier } }", "variables": variables},
            headers={"Authorization": _LINEAR_TOKEN},
        )

    found = get('{"id": "ENG-1"}')
    malformed = get("id=ENG-1")

    assert (found.status_code, found.json()["data"]["issue"]["identifier"]) == (200, "ENG-1")
    assert (malformed.status_code, malformed.json()["errors"][0]["message"]) == (400, "Variables are invalid JSON.")


def test_a_slack_method_the_fake_does_not_have_is_answered_as_slack_does_and_counted(
    external_services: RunningExternalServices,
) -> None:
    asked = httpx.get(
        "{}/api/admin.users.list".format(external_services.slack_url), params={"limit": "5"}, headers=_SLACK_HEADERS
    )
    posted = httpx.post("{}/api/admin.users.list".format(external_services.slack_url), headers=_SLACK_HEADERS)
    other = httpx.get("{}/api/no.such.method".format(external_services.slack_url), headers=_SLACK_HEADERS)
    # A method the fake has, a call that fails for the caller's own reason, and a path that is no method.
    _slack(external_services, "auth.test")
    missing_argument = _slack(external_services, "conversations.history")
    not_a_method = httpx.get("{}/no/such/path".format(external_services.slack_url), headers=_SLACK_HEADERS)

    for answer in (asked, posted, other):
        assert (answer.status_code, answer.json()) == (200, {"ok": False, "error": "unknown_method"})
    assert missing_argument["ok"] is False and missing_argument["error"] != "unknown_method"
    assert not_a_method.status_code == 404
    assert _control(external_services, "health")["unsupported_calls"] == {
        "slack:admin.users.list": 2,
        "slack:no.such.method": 1,
    }
    assert [(line["status"], line["error"], line["unsupported"]) for line in _control(external_services, "log")] == [
        (200, "unknown_method", "slack:admin.users.list"),
        (200, "unknown_method", "slack:admin.users.list"),
        (200, "unknown_method", "slack:no.such.method"),
        (200, None, None),
        (200, missing_argument["error"], None),
        (404, None, None),
    ]


def test_every_gap_of_one_query_is_counted_once_and_the_log_names_the_first(
    external_services: RunningExternalServices,
) -> None:
    answer = _linear(external_services, "{ viewer { timezone guest } users { nodes { timezone } } }")

    assert answer.json()["data"]["viewer"] == {"timezone": None, "guest": False}
    assert _control(external_services, "health")["unsupported_calls"] == {
        "linear:field:User.timezone": 1,
        "linear:field:User.guest": 1,
    }
    assert [line["unsupported"] for line in _control(external_services, "log")] == ["linear:field:User.timezone"]


def test_introspection_shows_linears_own_schema(external_services: RunningExternalServices) -> None:
    """A workspace agent that introspects sees the fields the real API has, the fake's gaps among them,
    and never a type the fake made up."""
    project = _linear(external_services, '{ __type(name: "Project") { fields { name } } }')
    filters = _linear(external_services, '{ __type(name: "IssueFilter") { kind inputFields { name } } }')

    assert {"lead", "slugId", "status", "issues", "name"} <= {
        f["name"] for f in project.json()["data"]["__type"]["fields"]
    }
    assert filters.json()["data"]["__type"]["kind"] == "INPUT_OBJECT"
    assert {"slaStatus", "title", "state"} <= {f["name"] for f in filters.json()["data"]["__type"]["inputFields"]}


def test_an_update_that_is_not_json_is_refused_as_one(external_services: RunningExternalServices) -> None:
    before = _control(external_services, "health")["counts"]

    for body in ("not json", "", '{"issues": ['):
        refused = _add(external_services, "linear", body)
        assert (refused.status_code, refused.json()) == (400, {"ok": False, "error": "the update is not JSON"})

    assert _control(external_services, "health")["counts"] == before


def test_the_launcher_takes_every_fixture_the_checks_accept(external_services: RunningExternalServices) -> None:
    """The checks promise that a file they accept is one the launcher takes. The launcher is already
    serving the seeds the checks accepted; each update follows them."""
    for service in SERVICES:
        seed = parse_service_seed(
            service, external_services_fixture_text(EXTERNAL_SERVICES_SEED_FIXTURE, service.name), "seed"
        )
        update_text = external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, service.name)
        parse_service_update(service, update_text, seed.known, "update")

        added = _add(external_services, service.name, update_text)

        assert added.status_code == 200, (service.name, added.text)


def test_the_launcher_takes_each_way_of_naming_a_record_the_checks_accept(
    external_services: RunningExternalServices,
) -> None:
    """Linear's references may be made more than one way, and the checks and the launcher have to
    agree on each: a state by its type, a person by email, a cycle by its number, a label of the
    issue's own team, and the default states of a team listed without any."""
    linear = service_named("linear")
    known = parse_service_seed(
        linear, external_services_fixture_text(EXTERNAL_SERVICES_SEED_FIXTURE, "linear"), "seed"
    ).known
    issue = {"team": "ENG", "title": "Named another way", "created_at": "2026-03-20T21:00:00.000Z"}
    updates: list[dict[str, Any]] = [
        {"issues": [{**issue, "identifier": "ENG-10", "state": "started"}]},
        {
            "issues": [
                {
                    **issue,
                    "identifier": "ENG-15",
                    "state": "Planned",
                    "created_at": "2026-03-20T23:00:00+02:00",
                    "updated_at": "2026-03-20T14:30:00.123456-07:00",
                    "due_date": "2026-03-27",
                }
            ]
        },
        {
            "issues": [
                {
                    **issue,
                    "identifier": "ENG-11",
                    "state": "Planned",
                    "assignee": "thomas.baird@redwood.example",
                    "creator": "Morgan Reyes",
                    "cycle": "11",
                }
            ],
            "comments": [
                {
                    "issue": "ENG-11",
                    "user": "morgan.reyes@redwood.example",
                    "body": "Noted.",
                    "created_at": "2026-03-20T22:00:00.000Z",
                }
            ],
        },
        {
            "labels": [{"name": "eng-only", "team": "ENG"}],
            "issues": [{**issue, "identifier": "ENG-12", "state": "Done", "labels": ["eng-only", "telemetry"]}],
        },
        {
            "teams": [{"key": "OPS", "name": "Operations"}, {"key": "ENG", "name": "Engineering"}],
            "issues": [
                {**issue, "identifier": "OPS-1", "team": "OPS", "state": "Todo"},
                {**issue, "identifier": "ENG-13", "state": "Backlog"},
                {
                    **issue,
                    "identifier": "ENG-14-follow-up",
                    "state": "In Progress",
                    "relations": [{"type": "blocks", "issue": "OPS-1"}],
                },
            ],
        },
    ]

    for update in updates:
        known = parse_service_update(linear, json.dumps(update), known, "update")
        added = _add(external_services, "linear", json.dumps(update))
        assert added.status_code == 200, (update, added.text)

    assert sorted(_issue_titles(external_services)) == sorted(
        ["ENG-1", "ENG-2", "ENG-10", "ENG-11", "ENG-12", "ENG-13", "ENG-14-follow-up", "ENG-15", "OPS-1"]
    )
    # An offset and a fraction are read as the instant they state, then shifted like any other.
    stamped = _linear(external_services, '{ issue(id: "ENG-15") { createdAt updatedAt dueDate } }')
    assert stamped.json()["data"]["issue"] == {
        "createdAt": "2026-10-06T21:00:00.000Z",
        "updatedAt": "2026-10-06T21:30:00.123Z",
        "dueDate": "2026-10-13",
    }


_PLACEABLE_ISSUE = {
    "identifier": "ENG-20",
    "team": "ENG",
    "title": "Refers to something",
    "state": "Planned",
    "created_at": "2026-03-20T21:00:00.000Z",
}
_PLACEABLE_COMMENT = {"issue": "ENG-1", "body": "Noted.", "created_at": "2026-03-20T22:00:00.000Z"}
_PLACEABLE_MESSAGE = {"channel": "eng-oncall", "user": "claire", "ts": "1774060000.000000", "text": "Noted."}


@pytest.mark.parametrize(
    ("service_name", "update"),
    [
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "team": "OPS"}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "state": "Shipped"}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "assignee": "Nobody Here"}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "creator": "nobody@redwood.example"}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "project": "No such project"}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "cycle": "2026-W99"}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "labels": ["telemetry", "no-such-label"]}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "identifier": "ENG-2"}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "relations": [{"type": "blocks", "issue": "ENG-99"}]}]}),
        ("linear", {"comments": [{**_PLACEABLE_COMMENT, "issue": "ENG-99"}]}),
        ("linear", {"comments": [{**_PLACEABLE_COMMENT, "user": "Nobody Here"}]}),
        ("slack", {"messages": [{**_PLACEABLE_MESSAGE, "channel": "no-such-channel"}]}),
        ("slack", {"messages": [{**_PLACEABLE_MESSAGE, "user": "nobody"}]}),
        ("slack", {"messages": [{**_PLACEABLE_MESSAGE, "thread_ts": "1.000000"}]}),
        ("slack", {"channels": [{"name": "leads", "is_private": True, "members": ["nobody"]}]}),
        ("linear", {"projects": [{"name": "Billing cleanup", "lead": "Nobody Here"}]}),
        # A user or a cycle listed again under what the fake already keeps one by is not added, so
        # the name or number only the second entry gives is no record's to refer to.
        (
            "linear",
            {
                "users": [{"name": "T. Baird", "email": "thomas.baird@redwood.example"}],
                "issues": [{**_PLACEABLE_ISSUE, "assignee": "T. Baird"}],
            },
        ),
        (
            "linear",
            {
                "cycles": [
                    {
                        "team": "ENG",
                        "name": "2026-W11",
                        "number": 12,
                        "starts_at": "2026-03-09T00:00:00Z",
                        "ends_at": "2026-03-16T00:00:00Z",
                    }
                ],
                "issues": [{**_PLACEABLE_ISSUE, "cycle": "12"}],
            },
        ),
        # Python's ISO 8601 reader takes each of these, and the launcher's reader takes neither.
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "created_at": "20260320T210000Z"}]}),
        ("linear", {"issues": [{**_PLACEABLE_ISSUE, "due_date": "soon"}]}),
        ("linear", {"comments": [{**_PLACEABLE_COMMENT, "created_at": "20260320T220000Z"}]}),
    ],
)
def test_the_launcher_refuses_what_the_checks_refuse_for_a_reference(
    external_services: RunningExternalServices, service_name: str, update: dict[str, Any]
) -> None:
    """Each of these names a record the fake does not hold. The checks refuse it when the task is
    loaded, and the launcher, which would otherwise refuse it mid-trial, agrees and keeps nothing."""
    service = service_named(service_name)
    known = parse_service_seed(
        service, external_services_fixture_text(EXTERNAL_SERVICES_SEED_FIXTURE, service_name), "seed"
    ).known
    before = _control(external_services, "health")["counts"]

    with pytest.raises(TaskAuthoringError):
        parse_service_update(service, json.dumps(update), known, "update")
    refused = _add(external_services, service_name, json.dumps(update))

    assert refused.status_code == 400, refused.text
    assert refused.json()["ok"] is False
    assert _control(external_services, "health")["counts"] == before


def test_the_access_log_holds_one_line_per_request_and_no_token(external_services: RunningExternalServices) -> None:
    _control(external_services, "health")
    httpx.get(
        "{}/api/conversations.list".format(external_services.slack_url),
        params={"limit": "1", "token": _SLACK_TOKEN},
        headers={**_SLACK_HEADERS, "User-Agent": "roadmap-app/1.0", "X-Eval-Caller": "app"},
    )
    _linear(external_services, "{ teams { nodes { key } } }")
    _linear(
        external_services,
        'mutation { issueCreate(input: {teamId: "ENG", title: "x"}) { success } }',
        token="lin_api_other",
    )

    log_text = external_services.access_log_path.read_text()
    slack_line, linear_line, refused_line = (json.loads(line) for line in log_text.splitlines())

    assert _SLACK_TOKEN not in log_text and _LINEAR_TOKEN not in log_text
    assert " ".join(slack_line) == (
        "ts seq service method url body content_type token_user token_sha256_8 user_agent caller "
        "status ok error unsupported rate_limited result_count response_bytes duration_ms"
    )
    assert slack_line["url"] == "https://slack.com/api/conversations.list?limit=1&token=[redacted]"
    assert (slack_line["seq"], slack_line["service"], slack_line["method"]) == (1, "slack", "GET")
    assert (slack_line["token_user"], slack_line["user_agent"], slack_line["caller"]) == (
        "morgan.reyes",
        "roadmap-app/1.0",
        "app",
    )
    assert (slack_line["status"], slack_line["ok"], slack_line["result_count"]) == (200, True, 1)
    assert slack_line["response_bytes"] > 0
    assert (slack_line["rate_limited"], linear_line["rate_limited"]) == (None, None)
    assert (linear_line["url"], linear_line["result_count"]) == ("https://api.linear.app/graphql", 1)
    assert json.loads(linear_line["body"]) == {"query": "{ teams { nodes { key } } }"}
    assert (refused_line["status"], refused_line["ok"], refused_line["token_user"]) == (401, False, None)
    assert _control(external_services, "log") == [slack_line, linear_line, refused_line]


def test_the_state_dump_redacts_every_token(external_services: RunningExternalServices) -> None:
    dump_text = httpx.get("{}/_control/state".format(external_services.control_url)).text
    dump = json.loads(dump_text)

    assert _SLACK_TOKEN not in dump_text and _LINEAR_TOKEN not in dump_text
    assert "[redacted]" in dump_text
    assert dump["counts"]["slack"] == {"users": 3, "channels": 2, "messages": 4}
    assert dump["counts"]["linear"]["issues"] == len(
        external_services_document(EXTERNAL_SERVICES_SEED_FIXTURE, "linear")["issues"]
    )
    # Relations are a collection of the Linear store's own, dumped and restored with the rest.
    relations = dump["linear"]["collections"]["linear.issue_relations"]["items"]
    assert [relation["type"] for relation in relations] == ["blocks"]


def test_no_answer_says_what_is_serving_it(external_services: RunningExternalServices) -> None:
    """A workspace agent must not be able to tell a fake from the service, least of all from an
    error: these are the answers a client gets for asking something that is not served."""
    answers = [
        _linear(external_services, "{ issues(filter: {slaStatus: {eq: Breached}}) { nodes { id } } }"),
        _linear(external_services, '{ issues(filter: {title: {regex: "x"}}) { nodes { id } } }'),
        _linear(external_services, '{ issues(filter: {createdAt: {gt: "last week"}}) { nodes { id } } }'),
        _linear(external_services, "{ noSuchField }"),
        _linear(external_services, "{ viewer { name }", token="lin_api_other"),
        _linear(external_services, 'mutation { issueCreate(input: {teamId: "NOPE", title: "x"}) { success } }'),
        httpx.get("{}/".format(external_services.linear_url), headers={"Authorization": _LINEAR_TOKEN}),
        httpx.get("{}/".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/no/such/path".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/api/no.such.method".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/api/conversations.history".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/api/auth.test".format(external_services.slack_url)),
        httpx.get(
            "{}/files-pri/T1-F1/report.pdf".format(external_services.slack_url),
            headers={**_SLACK_HEADERS, "X-Forwarded-Host": "files.slack.com"},
        ),
        httpx.get(
            "{}/abc".format(external_services.linear_url),
            headers={"Authorization": _LINEAR_TOKEN, "X-Forwarded-Host": "uploads.linear.app"},
        ),
        httpx.get("{}/_control/health".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/_control/state".format(external_services.linear_url), headers={"Authorization": _LINEAR_TOKEN}),
    ]

    for answer in answers:
        visible = (answer.text + json.dumps(dict(answer.headers))).lower()
        for word in ("emulat", "fake", "eval", "probe", "mock"):
            assert word not in visible, (word, answer.request.url, answer.text[:300])
    # An unsupported filter reads as a query Linear validated and refused.
    refused = answers[0].json()["errors"][0]
    assert refused["message"] == 'Field "slaStatus" is not defined by type "IssueFilter".'
    assert refused["extensions"]["code"] == "GRAPHQL_VALIDATION_FAILED"
    assert "html" not in answers[7].headers["content-type"]


# What a burst of requests is sent through: one connection, so the burst is as quick as the fake.
def _slack_burst(services: RunningExternalServices, method: str, count: int, **params: str) -> list[httpx.Response]:
    with httpx.Client(headers=_SLACK_HEADERS) as client:
        return [client.get("{}/api/{}".format(services.slack_url, method), params=params) for _ in range(count)]


def _replies_params(services: RunningExternalServices) -> dict[str, str]:
    return {"channel": _channel_id(services, "eng-oncall"), "ts": _shifted("1773900000.000000")}


# Tier 3, 50 a minute, and the burst allowance of a fifth more.
_TIER_3_LIMIT = 60


def test_a_burst_past_a_slack_methods_tier_is_refused_as_slack_refuses_it(
    external_services: RunningExternalServices,
) -> None:
    burst = _slack_burst(
        external_services, "conversations.replies", _TIER_3_LIMIT + 2, **_replies_params(external_services)
    )
    history = _slack(external_services, "conversations.history", channel=_channel_id(external_services, "eng-oncall"))

    assert [answer.json()["ok"] for answer in burst[:_TIER_3_LIMIT]] == [True] * _TIER_3_LIMIT
    for refused in burst[_TIER_3_LIMIT:]:
        assert refused.status_code == 429
        assert refused.json() == {"ok": False, "error": "ratelimited"}
        assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert "retry-after" not in burst[0].headers
    # Slack limits each method on its own: the methods around the burst still answer.
    assert history["ok"] is True
    expected = {"slack:conversations.replies": 2}
    assert _control(external_services, "health")["rate_limited"] == expected
    assert _control(external_services, "state")["rate_limited"] == expected
    assert _control(external_services, "health")["rate_limits"] == "real"
    log = _control(external_services, "log")
    replies_lines = [line for line in log if "conversations.replies" in line["url"]]
    assert [(line["status"], line["error"], line["rate_limited"]) for line in replies_lines[-3:]] == [
        (200, None, None),
        (429, "ratelimited", "slack:conversations.replies"),
        (429, "ratelimited", "slack:conversations.replies"),
    ]


def test_a_slack_method_answers_again_once_its_retry_after_has_passed(
    external_services_with_short_rate_limit_windows: RunningExternalServices,
) -> None:
    services = external_services_with_short_rate_limit_windows
    # users.list is Tier 2: 20 a minute and 4 more.
    burst = _slack_burst(services, "users.list", 25)
    retry_after = int(burst[-1].headers["retry-after"])
    # A refused request draws on nothing, so asking until one is answered waits the window out.
    wait_for(
        lambda: httpx.get("{}/api/users.list".format(services.slack_url), headers=_SLACK_HEADERS).status_code == 200,
        timeout=EXTERNAL_SERVICES_TEST_RATE_LIMIT_WINDOW_SECONDS + 3,
        poll_interval=0.1,
        error_message="users.list was still refused after its window",
    )

    assert [answer.status_code for answer in burst] == [200] * 24 + [429]
    assert 1 <= retry_after <= EXTERNAL_SERVICES_TEST_RATE_LIMIT_WINDOW_SECONDS


def test_a_seed_that_asks_for_no_rate_limits_serves_an_unlimited_burst(
    unlimited_external_services: RunningExternalServices,
) -> None:
    burst = _slack_burst(
        unlimited_external_services,
        "conversations.replies",
        3 * _TIER_3_LIMIT,
        **_replies_params(unlimited_external_services),
    )
    linear_answer = _linear(unlimited_external_services, "{ viewer { id } }")

    assert {answer.status_code for answer in burst} == {200}
    assert "x-complexity" not in linear_answer.headers
    health = _control(unlimited_external_services, "health")
    assert (health["rate_limits"], health["rate_limited"]) == ("none", {})


def _launcher_started_ms() -> float:
    anchor = external_services_document(EXTERNAL_SERVICES_SEED_FIXTURE, "linear")["anchor"]
    return (
        datetime.fromisoformat(anchor.replace("Z", "+00:00")).timestamp() + EXTERNAL_SERVICES_TEST_SHIFT_SECONDS
    ) * 1000


def test_every_linear_answer_carries_linears_rate_limit_headers(external_services: RunningExternalServices) -> None:
    answer = _linear(external_services, "{ viewer { name } }")
    # Linear's own examples: a user and three fields of its created issues, 50 by default, is 66
    # points, and 14 with `first: 10`.
    default_page = _linear(external_services, "{ viewer { createdIssues { nodes { id title createdAt } } } }")
    ten = _linear(external_services, "{ viewer { createdIssues(first: 10) { nodes { id title createdAt } } } }")
    by_variable = httpx.post(
        "{}/graphql".format(external_services.linear_url),
        json={
            "query": "query Q($n: Int) { viewer { createdIssues(first: $n) { nodes { id title createdAt } } } }",
            "variables": {"n": 10},
        },
        headers={"Authorization": _LINEAR_TOKEN},
    )
    by_default = _linear(
        external_services,
        "query Q($n: Int = 10) { viewer { createdIssues(first: $n) { nodes { id title createdAt } } } }",
    )

    headers = answer.headers
    assert answer.status_code == 200
    # Linear's own example: a user with one property is 1.1 points, rounded up.
    assert headers["x-complexity"] == "2"
    assert (headers["x-ratelimit-requests-limit"], headers["x-ratelimit-requests-remaining"]) == ("2500", "2499")
    assert (headers["x-ratelimit-complexity-limit"], headers["x-ratelimit-complexity-remaining"]) == (
        "3000000",
        "2999998",
    )
    # Each reset is when the bucket is whole again, in epoch milliseconds on the trial's clock: a
    # request drains away in an hour's 2,500th, two points in an hour's 1,500,000th.
    started_ms = _launcher_started_ms()
    requests_reset = int(headers["x-ratelimit-requests-reset"])
    complexity_reset = int(headers["x-ratelimit-complexity-reset"])
    assert started_ms < complexity_reset < requests_reset < started_ms + 120_000
    assert 1400 <= requests_reset - complexity_reset <= 1440
    assert "retry-after" not in headers
    assert [
        default_page.headers["x-complexity"],
        ten.headers["x-complexity"],
        by_variable.headers["x-complexity"],
        by_default.headers["x-complexity"],
    ] == [
        "66",
        "14",
        "14",
        "14",
    ]
    assert default_page.headers["x-ratelimit-requests-remaining"] == "2498"


# A query of exactly the per-query maximum: 100 teams, each 1 point for itself and 90 issues of 1.1.
_MAX_COMPLEXITY_QUERY = "{ teams(first: 100) { nodes { issues(first: 90) { nodes { id } } } } }"


def test_a_linear_query_past_the_per_query_maximum_is_refused_and_costs_nothing(
    external_services: RunningExternalServices,
) -> None:
    at_maximum = _linear(external_services, _MAX_COMPLEXITY_QUERY)
    past_maximum = _linear(
        external_services, "{ teams(first: 100) { nodes { issues(first: 90) { nodes { id title } } } } }"
    )
    after = _linear(external_services, "{ viewer { id } }")

    assert (at_maximum.status_code, at_maximum.headers["x-complexity"]) == (200, "10000")
    assert (past_maximum.status_code, past_maximum.headers["x-complexity"]) == (429, "10900")
    error = past_maximum.json()["errors"][0]
    assert error["extensions"]["code"] == "RATELIMITED"
    assert "10000" in error["message"]
    # Waiting never admits it, so no wait is named.
    assert "retry-after" not in past_maximum.headers
    # The refusal reports the user's standing without itself, and the next answer is the second counted.
    assert past_maximum.headers["x-ratelimit-requests-remaining"] == "2499"
    assert after.headers["x-ratelimit-requests-remaining"] == "2498"
    assert _control(external_services, "health")["rate_limited"] == {"linear:complexity": 1}


# The answers up to the first refusal, and how many seconds sending them took.
def _linear_until_refused(
    services: RunningExternalServices, query: str, at_most: int
) -> tuple[list[httpx.Response], float]:
    answers: list[httpx.Response] = []
    began = time.monotonic()
    with httpx.Client(headers={"Authorization": _LINEAR_TOKEN}) as client:
        while len(answers) < at_most and (not answers or answers[-1].status_code == 200):
            answers.append(client.post("{}/graphql".format(services.linear_url), json={"query": query}))
    return answers, time.monotonic() - began


def test_linear_refuses_a_user_past_its_hourly_complexity(external_services: RunningExternalServices) -> None:
    answers, elapsed_seconds = _linear_until_refused(external_services, _MAX_COMPLEXITY_QUERY, 400)
    refused = answers[-1]

    # 3,000,000 points an hour hold 300 queries of 10,000, and one more drains away every 12 seconds.
    assert 300 <= len(answers) - 1 <= 300 + elapsed_seconds / 12 + 1
    assert refused.status_code == 429
    assert refused.json()["errors"][0]["extensions"]["code"] == "RATELIMITED"
    assert "complexity" in refused.json()["errors"][0]["message"]
    # The bucket drains at 3,000,000 points an hour, so room for one more query takes seconds.
    assert 1 <= int(refused.headers["retry-after"]) <= 13
    assert int(refused.headers["x-ratelimit-complexity-remaining"]) < 10000
    assert _control(external_services, "health")["rate_limited"] == {"linear:complexity": 1}
    assert _control(external_services, "log")[-1]["rate_limited"] == "linear:complexity"


def test_linear_refuses_a_user_past_its_hourly_requests(external_services: RunningExternalServices) -> None:
    answers, elapsed_seconds = _linear_until_refused(external_services, "{ viewer { id } }", 3000)
    refused = answers[-1]

    # 2,500 requests an hour, and one more for each 1.44 seconds the burst took to send.
    assert 2500 <= len(answers) - 1 <= 2500 + elapsed_seconds / 1.44 + 1
    assert refused.status_code == 429
    assert refused.json()["errors"][0]["extensions"]["code"] == "RATELIMITED"
    assert refused.headers["x-ratelimit-requests-remaining"] == "0"
    # One request drains away every 1.44 seconds.
    assert 1 <= int(refused.headers["retry-after"]) <= 2
    assert _control(external_services, "health")["rate_limited"] == {"linear:requests": 1}
