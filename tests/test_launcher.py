"""The real launcher on small seeds. Skipped on a machine without node or the launcher's npm
packages (`just minds-evals-external-services-install`)."""

import base64
import email
import json
import time
from collections.abc import Iterator
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Any

import httpx2
import pytest

from imbue.minds_evals.conftest import EXTERNAL_SERVICES_TEST_RATE_LIMIT_WINDOW_SECONDS
from imbue.minds_evals.conftest import EXTERNAL_SERVICES_TEST_SHIFT_SECONDS
from imbue.minds_evals.conftest import RunningExternalServices
from imbue.minds_evals.errors import TaskAuthoringError
from imbue.minds_evals.external_services import SERVICES
from imbue.minds_evals.external_services import parse_service_seed
from imbue.minds_evals.external_services import parse_service_update
from imbue.minds_evals.external_services import probe_identities
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
    return httpx2.get("{}/api/{}".format(services.slack_url, method), params=params, headers=_SLACK_HEADERS).json()


def _linear(
    services: RunningExternalServices,
    query: str,
    token: str = _LINEAR_TOKEN,
    variables: dict[str, Any] | None = None,
) -> httpx2.Response:
    body: dict[str, Any] = {"query": query} if variables is None else {"query": query, "variables": variables}
    return httpx2.post("{}/graphql".format(services.linear_url), json=body, headers={"Authorization": token})


def _control(services: RunningExternalServices, route: str) -> Any:
    return httpx2.get("{}/_control/{}".format(services.control_url, route)).json()


def _add(services: RunningExternalServices, service_name: str, update_text: str) -> httpx2.Response:
    return httpx2.post("{}/_control/records/{}".format(services.control_url, service_name), content=update_text)


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


def _shifted_date(day: str) -> str:
    """A seed's calendar date as the launcher serves it under the test's shift."""
    return (date.fromisoformat(day) + timedelta(seconds=EXTERNAL_SERVICES_TEST_SHIFT_SECONDS)).isoformat()


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


_GOOGLE_TOKEN = "ya29.fake-redwood-google-token-morgan"
_THOMAS_GOOGLE_TOKEN = "ya29.fake-redwood-google-token-thomas"
_MORGAN = "morgan.reyes@redwood.example"


def _gmail_get(
    services: RunningExternalServices, path: str, token: str | None = _GOOGLE_TOKEN, **params: str
) -> httpx2.Response:
    headers = {} if token is None else {"Authorization": "Bearer {}".format(token)}
    return httpx2.get("{}/gmail/v1/users/{}".format(services.google_url, path), params=params, headers=headers)


def _gmail(services: RunningExternalServices, path: str, **params: str) -> Any:
    answer = _gmail_get(services, path, **params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _message_keys(services: RunningExternalServices, mailbox: str = _MORGAN) -> dict[str, str]:
    """Each seeded message's Gmail id by its key, as the state dump holds them."""
    items = _control(services, "state")["google"]["collections"]["google.messages"]["items"]
    return {item["seed_key"]: item["gmail_id"] for item in items if item["user_email"] == mailbox}


def _listed_ids(services: RunningExternalServices, kind: str = "messages", **params: str) -> list[str]:
    return [entry["id"] for entry in _gmail(services, "me/{}".format(kind), **params).get(kind, [])]


def _calendar_get(
    services: RunningExternalServices, path: str, token: str | None = _GOOGLE_TOKEN, **params: str
) -> httpx2.Response:
    headers = {} if token is None else {"Authorization": "Bearer {}".format(token)}
    return httpx2.get("{}/calendar/v3/{}".format(services.google_url, path), params=params, headers=headers)


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {header["name"]: header["value"] for header in message["payload"]["headers"]}


def _shifted_ms(stamp: str) -> int:
    return round((datetime.fromisoformat(stamp).timestamp() + EXTERNAL_SERVICES_TEST_SHIFT_SECONDS) * 1000)


def _shifted_date_header(stamp: str) -> str:
    """A seed's date as a Date header serves it under the test's shift: in the offset the seed states,
    with the day unpadded as Gmail writes it."""
    moment = datetime.fromisoformat(stamp) + timedelta(seconds=EXTERNAL_SERVICES_TEST_SHIFT_SECONDS)
    return "{:%a}, {} {:%b %Y %H:%M:%S %z}".format(moment, moment.day, moment)


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
        "createdAt": _shifted_iso("2026-03-19T09:00:00.000Z"),
        "updatedAt": _shifted_iso("2026-03-20T12:00:00.000Z"),
        "comments": {"nodes": [{"createdAt": _shifted_iso("2026-03-20T12:00:00.000Z")}]},
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
    slack_answer = httpx2.get(
        "{}/api/conversations.list".format(external_services.slack_url), headers={"Authorization": "Bearer xoxb-other"}
    )
    linear_answer = _linear(external_services, "{ viewer { name } }", token="lin_api_other")
    anonymous = httpx2.get("{}/api/conversations.list".format(external_services.slack_url))

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
        "createdAt": _shifted_iso("2026-03-20T18:00:00.000Z"),
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

    unnamed = httpx2.post("{}/_control/records".format(external_services.control_url), content=update_text)
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
        httpx2.get("{}/_control/{}".format(service_url, route), headers=headers)
        for service_url, headers in (
            (external_services.slack_url, _SLACK_HEADERS),
            (external_services.linear_url, {"Authorization": _LINEAR_TOKEN}),
        )
        for route in ("health", "state", "log")
    ]
    answers.append(
        httpx2.post(
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
    assert after["requests"] == {"slack": 4, "linear": 3, "google": 0}


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
    slack_file = httpx2.get(
        "{}/files-pri/T1-F1/report.pdf".format(external_services.slack_url),
        headers={**_SLACK_HEADERS, "X-Forwarded-Host": "files.slack.com"},
    )
    # The path is one the API host serves, so only the named host can be what refuses it.
    sibling_api = httpx2.get(
        "{}/api/auth.test".format(external_services.slack_url),
        headers={**_SLACK_HEADERS, "X-Forwarded-Host": "redwood.slack.com"},
    )
    linear_upload = httpx2.post(
        "{}/graphql".format(external_services.linear_url),
        json={"query": "{ viewer { name } }"},
        headers={"Authorization": _LINEAR_TOKEN, "X-Forwarded-Host": "uploads.linear.app"},
    )
    primary = httpx2.get(
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
    got = httpx2.get(
        "{}/graphql".format(external_services.linear_url),
        params={"query": "{ viewer { name } }"},
        headers={"Authorization": _LINEAR_TOKEN},
    )
    invalid_get = httpx2.get(
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

    def post(operation_name: str) -> httpx2.Response:
        return httpx2.post(
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
    def get(variables: str) -> httpx2.Response:
        return httpx2.get(
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
    asked = httpx2.get(
        "{}/api/admin.users.list".format(external_services.slack_url), params={"limit": "5"}, headers=_SLACK_HEADERS
    )
    posted = httpx2.post("{}/api/admin.users.list".format(external_services.slack_url), headers=_SLACK_HEADERS)
    other = httpx2.get("{}/api/no.such.method".format(external_services.slack_url), headers=_SLACK_HEADERS)
    # A method the fake has, a call that fails for the caller's own reason, and a path that is no method.
    _slack(external_services, "auth.test")
    missing_argument = _slack(external_services, "conversations.history")
    not_a_method = httpx2.get("{}/no/such/path".format(external_services.slack_url), headers=_SLACK_HEADERS)

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
        "createdAt": _shifted_iso("2026-03-20T21:00:00.000Z"),
        "updatedAt": _shifted_iso("2026-03-20T21:30:00.123Z"),
        "dueDate": _shifted_date("2026-03-27"),
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
_PLACEABLE_EMAIL = {
    "key": "placeable",
    "mailbox": "morgan.reyes@redwood.example",
    "from": "Priya Shah <priya@northwind.example>",
    "to": "morgan.reyes@redwood.example",
    "subject": "Noted",
    "date": "2026-03-20T15:30:00-07:00",
    "body": "Noted.",
}
_PLACEABLE_EVENT = {
    "key": "placeable-event",
    "owner": "morgan.reyes@redwood.example",
    "summary": "Noted",
    "start": "2026-03-23T09:00:00-07:00",
    "end": "2026-03-23T09:30:00-07:00",
}


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
        ("google", {"messages": [{**_PLACEABLE_EMAIL, "mailbox": "nobody@redwood.example"}]}),
        ("google", {"messages": [{**_PLACEABLE_EMAIL, "in_reply_to": "no-such-message"}]}),
        ("google", {"messages": [{**_PLACEABLE_EMAIL, "labels": ["Vendors", "No such label"]}]}),
        ("google", {"messages": [{**_PLACEABLE_EMAIL, "key": "invoice-question"}]}),
        ("google", {"messages": [{**_PLACEABLE_EMAIL, "date": "2026-03-20T15:30:00"}]}),
        ("google", {"labels": [{"mailbox": "morgan.reyes@redwood.example", "name": "vendors"}]}),
        ("google", {"labels": [{"mailbox": "nobody@redwood.example", "name": "Finance"}]}),
        ("google", {"users": [{"email": "thomas.baird@redwood.example"}]}),
        ("google", {"events": [{**_PLACEABLE_EVENT, "owner": "nobody@redwood.example"}]}),
        ("google", {"events": [{**_PLACEABLE_EVENT, "key": "vendor-sync"}]}),
        ("google", {"events": [{**_PLACEABLE_EVENT, "start": "2026-03-23T09:00:00"}]}),
        ("google", {"events": [{**_PLACEABLE_EVENT, "end": "2026-03-23T08:00:00-07:00"}]}),
        ("google", {"events": [{**_PLACEABLE_EVENT, "start": "2026-03-23", "end": "2026-03-23T10:00:00-07:00"}]}),
        ("google", {"events": [{**_PLACEABLE_EVENT, "start": "2026-03-23", "end": "2026-03-23"}]}),
        ("google", {"events": [{**_PLACEABLE_EVENT, "attendees": [{"name": "No address"}]}]}),
        (
            "google",
            {"events": [{**_PLACEABLE_EVENT, "attendees": [{"email": "a@x.example", "response_status": "maybe"}]}]},
        ),
        (
            "google",
            {"events": [{**_PLACEABLE_EVENT, "attendees": [{"email": "a@x.example"}, {"email": "A@x.example"}]}]},
        ),
        # Thomas has no Vendors label of his own: a label belongs to one mailbox.
        (
            "google",
            {"messages": [{**_PLACEABLE_EMAIL, "mailbox": "thomas.baird@redwood.example", "labels": ["Vendors"]}]},
        ),
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
    httpx2.get(
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
        "ts seq phase service method url body content_type token_user token_sha256_8 user_agent caller "
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


def _mark_phase(services: RunningExternalServices, body: Any) -> httpx2.Response:
    return httpx2.post("{}/_control/phase".format(services.control_url), json=body)


def test_a_phase_mark_stamps_every_later_request_and_counts_it_apart(
    external_services: RunningExternalServices,
) -> None:
    """The verification flows drive the agent's own app, whose requests look like any other: the
    phase the driver marks is what tells them from the ones the app made while the agent worked."""
    assert _control(external_services, "health")["phases"] == {
        "agent": {"requests": {"slack": 0, "linear": 0, "google": 0}, "unsupported_calls": {}, "rate_limited": {}}
    }
    _slack(external_services, "auth.test")
    httpx2.get("{}/api/admin.users.list".format(external_services.slack_url), headers=_SLACK_HEADERS)

    marked = _mark_phase(external_services, {"name": "verification"})

    _slack(external_services, "auth.test")
    httpx2.get("{}/api/admin.users.list".format(external_services.slack_url), headers=_SLACK_HEADERS)
    _linear(external_services, "{ teams { nodes { key } } }")
    assert (marked.status_code, marked.json()) == (200, {"ok": True, "phase": "verification", "previous": "agent"})
    log_lines = _control(external_services, "log")
    assert [(line["phase"], line["service"]) for line in log_lines] == [
        ("agent", "slack"),
        ("agent", "slack"),
        ("verification", "slack"),
        ("verification", "slack"),
        ("verification", "linear"),
    ]
    file_lines = [json.loads(line) for line in external_services.access_log_path.read_text().splitlines()]
    assert [line["phase"] for line in file_lines] == [line["phase"] for line in log_lines]
    health = _control(external_services, "health")
    # The totals keep their shape, and each phase's counts sit beside them.
    assert health["counts"]["requests"] == {"slack": 4, "linear": 1, "google": 0}
    assert health["unsupported_calls"] == {"slack:admin.users.list": 2}
    assert health["phase"] == "verification"
    assert health["phases"] == {
        "agent": {
            "requests": {"slack": 2, "linear": 0, "google": 0},
            "unsupported_calls": {"slack:admin.users.list": 1},
            "rate_limited": {},
        },
        "verification": {
            "requests": {"slack": 2, "linear": 1, "google": 0},
            "unsupported_calls": {"slack:admin.users.list": 1},
            "rate_limited": {},
        },
    }
    state = _control(external_services, "state")
    assert (state["phase"], state["phases"]) == (health["phase"], health["phases"])

    # A later step's conversation is the agent's again, and adds to the agent's counts.
    _mark_phase(external_services, {"name": "agent"})
    _slack(external_services, "auth.test")
    assert _control(external_services, "health")["phases"]["agent"]["requests"] == {
        "slack": 3,
        "linear": 0,
        "google": 0,
    }
    assert _control(external_services, "log")[-1]["phase"] == "agent"


@pytest.mark.parametrize("body", [{"name": "Verification phase"}, {"name": ""}, {"name": 3}, {}])
def test_a_phase_mark_that_names_no_identifier_is_refused_and_changes_nothing(
    external_services: RunningExternalServices, body: Any
) -> None:
    refused = _mark_phase(external_services, body)

    assert refused.status_code == 400 and refused.json()["ok"] is False
    assert _control(external_services, "health")["phase"] == "agent"
    assert list(_control(external_services, "health")["phases"]) == ["agent"]


def test_a_phase_named_like_an_object_key_is_counted_like_any_other(
    external_services: RunningExternalServices,
) -> None:
    marked = _mark_phase(external_services, {"name": "constructor"})
    answered = _slack(external_services, "auth.test")

    assert marked.status_code == 200
    assert answered["ok"] is True
    phases = _control(external_services, "health")["phases"]
    assert phases["constructor"] == {
        "requests": {"slack": 1, "linear": 0, "google": 0},
        "unsupported_calls": {},
        "rate_limited": {},
    }


def test_the_state_dump_redacts_every_token(external_services: RunningExternalServices) -> None:
    dump_text = httpx2.get("{}/_control/state".format(external_services.control_url)).text
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
        httpx2.get("{}/".format(external_services.linear_url), headers={"Authorization": _LINEAR_TOKEN}),
        httpx2.get("{}/".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx2.get("{}/no/such/path".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx2.get("{}/api/no.such.method".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx2.get("{}/api/conversations.history".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx2.get("{}/api/auth.test".format(external_services.slack_url)),
        httpx2.get(
            "{}/files-pri/T1-F1/report.pdf".format(external_services.slack_url),
            headers={**_SLACK_HEADERS, "X-Forwarded-Host": "files.slack.com"},
        ),
        httpx2.get(
            "{}/abc".format(external_services.linear_url),
            headers={"Authorization": _LINEAR_TOKEN, "X-Forwarded-Host": "uploads.linear.app"},
        ),
        httpx2.get("{}/_control/health".format(external_services.slack_url), headers=_SLACK_HEADERS),
        httpx2.get("{}/_control/state".format(external_services.linear_url), headers={"Authorization": _LINEAR_TOKEN}),
        _gmail_get(external_services, "me/settings/vacation"),
        _gmail_get(external_services, "me/messages/0000000000000000"),
        _gmail_get(external_services, "me/profile", token=None),
        _gmail_get(external_services, "me/profile", token="ya29.other"),
        _gmail_get(external_services, "thomas.baird@redwood.example/profile"),
        _gmail_get(external_services, "me/messages", pageToken="x"),
        _gmail_get(external_services, "me/messages", q="nothing-says-this"),
        _gmail_get(external_services, "me/threads", q="invoice"),
        _gmail_get(external_services, "me/messages/8b5677a1c796501d", format="raw"),
        _gmail_get(external_services, "me/threads/8b5677a1c796501d"),
        _gmail_get(external_services, "me/history", startHistoryId="1"),
        _gmail_get(external_services, "me/labels"),
        _gmail_get(external_services, "me/settings/sendAs"),
        _gmail_get(external_services, "me/drafts"),
        _calendar_get(external_services, "users/me/calendarList"),
        _calendar_get(external_services, "calendars/primary"),
        _calendar_get(external_services, "calendars/primary/events", maxResults="1"),
        _calendar_get(external_services, "calendars/primary/events", orderBy="startTime"),
        _calendar_get(external_services, "calendars/primary/events", timeMin="yesterday"),
        _calendar_get(external_services, "calendars/primary/events", pageToken="x"),
        _calendar_get(external_services, "calendars/primary/events/0000000000"),
        _calendar_get(external_services, "calendars/thomas.baird@redwood.example/events"),
        _calendar_get(external_services, "colors"),
        httpx2.get("{}/".format(external_services.google_url)),
        httpx2.get("{}/o/oauth2/v2/auth".format(external_services.google_url)),
        httpx2.get("{}/oauth2/v2/userinfo".format(external_services.google_url)),
        httpx2.get(
            "{}/gmail/v1/users/me/profile".format(external_services.google_url),
            headers={"X-Forwarded-Host": "accounts.googleapis.com"},
        ),
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
def _slack_burst(services: RunningExternalServices, method: str, count: int, **params: str) -> list[httpx2.Response]:
    with httpx2.Client(headers=_SLACK_HEADERS) as client:
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


def test_a_rate_limited_request_is_counted_under_the_phase_it_was_made_in(
    external_services: RunningExternalServices,
) -> None:
    params = _replies_params(external_services)
    agent_burst = _slack_burst(external_services, "conversations.replies", _TIER_3_LIMIT + 1, **params)
    _mark_phase(external_services, {"name": "verification"})
    # A bucket knows no phase, so the window the agent filled refuses these too.
    verification_burst = _slack_burst(external_services, "conversations.replies", 2, **params)

    assert [answer.status_code for answer in agent_burst[_TIER_3_LIMIT:] + verification_burst] == [429] * 3
    health = _control(external_services, "health")
    assert health["rate_limited"] == {"slack:conversations.replies": 3}
    assert health["phases"]["agent"]["rate_limited"] == {"slack:conversations.replies": 1}
    assert health["phases"]["verification"]["rate_limited"] == {"slack:conversations.replies": 2}
    assert _control(external_services, "state")["phases"] == health["phases"]
    assert [line["phase"] for line in _control(external_services, "log") if line["rate_limited"]] == [
        "agent",
        "verification",
        "verification",
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
        lambda: httpx2.get("{}/api/users.list".format(services.slack_url), headers=_SLACK_HEADERS).status_code == 200,
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
    by_variable = _linear(
        external_services,
        "query Q($n: Int) { viewer { createdIssues(first: $n) { nodes { id title createdAt } } } }",
        variables={"n": 10},
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


# A query of exactly the per-query maximum: 100 teams, each 1 point for itself and 66 issues of 1.5.
_MAX_COMPLEXITY_QUERY = (
    "{ teams(first: 100) { nodes { issues(first: 66) { nodes { id title description createdAt updatedAt } } } } }"
)
# What the real API charged these on 2026-10-29 (`X-Complexity`), one for each rule of the scoring: the
# root issues list and an issue's own lists are priced as objects, every other connection multiplies
# its selection by its page size, and a selection of `id` alone is 0.2.
_MEASURED_COMPLEXITIES = (
    ("{ issues(first: 250) { nodes { id } } }", 2),
    ("{ issues(first: 250) { nodes { id title } } }", 3),
    ("{ issues(first: 250) { pageInfo { hasNextPage endCursor } nodes { id title } } }", 4),
    ("{ issues(first: 250) { nodes { id labels { nodes { name } } } } }", 5),
    ("{ issues(first: 250) { nodes { id project { id name } } } }", 4),
    ("{ issues(first: 250) { nodes { id project { id projectMilestones { nodes { id name } } } } } }", 64),
    ("{ issues(first: 250) { nodes { id assignee { id assignedIssues(first: 100) { nodes { id title } } } } } }", 124),
    ("{ projects(first: 50) { nodes { id name } } }", 60),
    ("{ projects(first: 50) { pageInfo { hasNextPage endCursor } nodes { id name } } }", 120),
    ("{ projects(first: 50) { nodes { id projectUpdates(first: 20) { nodes { id body } } } } }", 1255),
    ("{ teams { nodes { id issues(first: 250) { nodes { id } } } } }", 2555),
    ("{ teams { nodes { id members { nodes { id } } issues(first: 250) { nodes { id } } } } }", 3055),
    ("{ cycles(first: 10) { nodes { id issues(first: 250) { nodes { id title } } } } }", 3011),
    (
        "{ issues(first: 250) { nodes { id comments(first: 100) { nodes { id body user { name } } } labels { nodes { name } } "
        "project { id name projectMilestones { nodes { id } } projectUpdates(first: 20) { nodes { id } } } team { key } "
        "assignee { name } } } }",
        25,
    ),
)


def test_a_linear_query_past_the_per_query_maximum_is_refused_and_costs_nothing(
    external_services: RunningExternalServices,
) -> None:
    at_maximum = _linear(external_services, _MAX_COMPLEXITY_QUERY)
    past_maximum = _linear(
        external_services,
        "{ teams(first: 100) { nodes { issues(first: 67) { nodes { id title description createdAt updatedAt } } } } }",
    )
    after = _linear(external_services, "{ viewer { id } }")

    assert (at_maximum.status_code, at_maximum.headers["x-complexity"]) == (200, "10000")
    # Refused as Linear refuses it: an input error naming the score, with no standing headers.
    assert past_maximum.status_code == 400
    error = past_maximum.json()["errors"][0]
    assert (error["message"], error["extensions"]["code"]) == ("Query too complex", "INPUT_ERROR")
    assert error["extensions"]["userPresentableMessage"] == (
        "The query is too complex. Complexity: 10150. Maximum allowed complexity: 10000."
    )
    assert not {
        name for name in past_maximum.headers if name.startswith(("x-complexity", "x-ratelimit", "retry-after"))
    }
    # The refusal is not counted, so the next answer is the second counted.
    assert at_maximum.headers["x-ratelimit-requests-remaining"] == "2499"
    assert after.headers["x-ratelimit-requests-remaining"] == "2498"
    assert _control(external_services, "health")["rate_limited"] == {"linear:complexity": 1}


def test_linear_scores_a_query_as_the_real_api_charges_it(external_services: RunningExternalServices) -> None:
    scored = [
        (query, int(_linear(external_services, query).headers["x-complexity"])) for query, _ in _MEASURED_COMPLEXITIES
    ]

    assert scored == list(_MEASURED_COMPLEXITIES)


# The answers up to the first refusal, and how many seconds sending them took.
def _linear_until_refused(
    services: RunningExternalServices, query: str, at_most: int
) -> tuple[list[httpx2.Response], float]:
    answers: list[httpx2.Response] = []
    began = time.monotonic()
    with httpx2.Client(headers={"Authorization": _LINEAR_TOKEN}) as client:
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


# 2,500 requests take about 6 seconds alone, and longer beside a parallel suite.
@pytest.mark.timeout(30)
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


def test_the_google_store_holds_the_seed_and_nothing_of_the_emulators_own(
    external_services: RunningExternalServices,
) -> None:
    profile = _gmail(external_services, "me/profile")
    labels = _gmail(external_services, "me/labels")["labels"]
    dump = _control(external_services, "state")["google"]["collections"]

    assert profile == {"emailAddress": _MORGAN, "messagesTotal": 4, "threadsTotal": 2, "historyId": "1000005"}
    assert sorted(user["email"] for user in dump["google.users"]["items"]) == [
        _MORGAN,
        "thomas.baird@redwood.example",
    ]
    # No file or default mailbox of the package's own, one primary calendar per account, the seed's
    # events, and no record names the package.
    for collection in ("google.drive_items", "google.drafts"):
        assert dump.get(collection, {"items": []})["items"] == [], collection
    assert sorted(calendar["google_id"] for calendar in dump["google.calendars"]["items"]) == [
        _MORGAN,
        "thomas.baird@redwood.example",
    ]
    assert sorted(event["seed_key"] for event in dump["google.calendar_events"]["items"]) == [
        "finance-review",
        "offsite",
        "thomas-planning",
        "vendor-sync",
    ]
    assert "testuser" not in json.dumps(dump) and "emulate" not in json.dumps(dump).lower()
    assert [label["name"] for label in labels if label["type"] == "user"] == ["Vendors"]
    assert {label["id"] for label in labels} >= {"INBOX", "SENT", "UNREAD", "STARRED", "IMPORTANT"}
    # Every record is dated from the seed's messages, never by the box's clock.
    stamps = [
        datetime.fromisoformat(item[key].replace("Z", "+00:00")).timestamp()
        for collection in dump.values()
        for item in collection["items"]
        for key in ("created_at", "updated_at")
    ]
    assert stamps
    for stamp in stamps:
        assert abs(stamp - time.time()) > 24 * 60 * 60, datetime.fromtimestamp(stamp).isoformat()


def test_a_gmail_message_keeps_its_seeds_date_and_offset_shifted_to_the_trial(
    external_services: RunningExternalServices,
) -> None:
    keys = _message_keys(external_services)

    question = _gmail(external_services, "me/messages/{}".format(keys["invoice-question"]))
    reply = _gmail(external_services, "me/messages/{}".format(keys["invoice-reply"]), format="metadata")

    # 200 days after 2026-03-16, in the offset each sender's clock kept.
    assert _headers(question)["Date"] == _shifted_date_header("2026-03-16T09:15:00-07:00")
    assert _headers(reply)["Date"] == _shifted_date_header("2026-03-16T17:40:00+01:00")
    assert question["internalDate"] == str(_shifted_ms("2026-03-16T09:15:00-07:00"))
    assert reply["internalDate"] == str(_shifted_ms("2026-03-16T17:40:00+01:00"))
    for message in (question, reply):
        assert abs(int(message["internalDate"]) / 1000 - time.time()) > 24 * 60 * 60


def test_a_gmail_thread_holds_its_replies_with_their_headers_and_derived_labels(
    external_services: RunningExternalServices,
) -> None:
    keys = _message_keys(external_services)
    vendors = next(
        label["id"] for label in _gmail(external_services, "me/labels")["labels"] if label["name"] == "Vendors"
    )

    thread = _gmail(external_services, "me/threads/{}".format(keys["invoice-question"]))
    raw = _gmail(external_services, "me/messages/{}".format(keys["invoice-reply"]), format="raw")

    # A thread takes the id of its first message, and holds its replies oldest first.
    assert thread["id"] == keys["invoice-question"]
    messages = thread["messages"]
    assert [message["id"] for message in messages] == [
        keys["invoice-question"],
        keys["invoice-reply"],
        keys["invoice-followup"],
    ]
    # Received mail is in the INBOX and sent mail is SENT; the rest is what the seed states.
    assert [message["labelIds"] for message in messages] == [
        ["INBOX", vendors, "IMPORTANT"],
        ["SENT"],
        ["INBOX", vendors, "UNREAD", "STARRED"],
    ]
    question_headers, reply_headers, followup_headers = (_headers(message) for message in messages)
    assert question_headers["Message-ID"] == "<4411-question@northwind.example>"
    assert reply_headers["In-Reply-To"] == "<4411-question@northwind.example>"
    assert reply_headers["Message-ID"].endswith("@redwood.example>")
    assert followup_headers["In-Reply-To"] == reply_headers["Message-ID"]
    assert followup_headers["References"] == "<4411-question@northwind.example> {}".format(reply_headers["Message-ID"])
    # A message with HTML serves both its parts, and its raw form is a well-formed MIME message.
    assert [part["mimeType"] for part in messages[1]["payload"]["parts"]] == ["text/plain", "text/html"]
    decoded = base64.urlsafe_b64decode(raw["raw"] + "==").decode()
    parsed = email.message_from_string(decoded)
    assert (parsed["Date"], parsed.get_content_type()) == (
        _shifted_date_header("2026-03-16T17:40:00+01:00"),
        "multipart/alternative",
    )
    assert [part.get_payload() for part in parsed.walk() if not part.is_multipart()] == [
        "Checking with finance and will get back to you.",
        "<p>Checking with <b>finance</b> and will get back to you.</p>",
    ]
    # Ids and Message-IDs are derived from the seed: the same in every trial.
    assert keys["invoice-question"] == "8b5677a1c796501d"


def test_a_gmail_message_shows_the_mime_headers_of_its_raw_form(external_services: RunningExternalServices) -> None:
    keys = _message_keys(external_services)

    question = _gmail(external_services, "me/messages/{}".format(keys["invoice-question"]))
    reply = _gmail(external_services, "me/messages/{}".format(keys["invoice-reply"]))
    reply_metadata = _gmail(external_services, "me/messages/{}".format(keys["invoice-reply"]), format="metadata")
    named = _gmail(
        external_services,
        "me/messages/{}".format(keys["invoice-reply"]),
        format="metadata",
        metadataHeaders="content-type",
    )
    thread = _gmail(external_services, "me/threads/{}".format(keys["invoice-question"]), format="metadata")
    reply_raw = email.message_from_bytes(
        base64.urlsafe_b64decode(
            _gmail(external_services, "me/messages/{}".format(keys["invoice-reply"]), format="raw")["raw"] + "=="
        )
    )

    # A single-part message, in the order Gmail serves its headers.
    assert [header["name"] for header in question["payload"]["headers"]] == [
        "MIME-Version",
        "Date",
        "Message-ID",
        "Subject",
        "From",
        "To",
        "Cc",
        "Content-Type",
        "Content-Transfer-Encoding",
    ]
    assert (_headers(question)["MIME-Version"], _headers(question)["Content-Type"]) == (
        "1.0",
        'text/plain; charset="UTF-8"',
    )
    # A message with HTML is multipart/alternative, on the boundary its raw form uses, and each part
    # names its own type.
    assert [header["name"] for header in reply["payload"]["headers"]] == [
        "MIME-Version",
        "Date",
        "References",
        "In-Reply-To",
        "Message-ID",
        "Subject",
        "From",
        "To",
        "Content-Type",
    ]
    content_type = 'multipart/alternative; boundary="{}"'.format(reply_raw.get_boundary())
    assert (reply["payload"]["mimeType"], _headers(reply)["Content-Type"]) == ("multipart/alternative", content_type)
    assert _headers(reply)["Content-Type"] == reply_raw["Content-Type"]
    assert [part["headers"] for part in reply["payload"]["parts"]] == [
        [
            {"name": "Content-Type", "value": 'text/{}; charset="UTF-8"'.format(subtype)},
            {"name": "Content-Transfer-Encoding", "value": "8bit"},
        ]
        for subtype in ("plain", "html")
    ]
    assert [dict(part.items()) for part in reply_raw.walk() if not part.is_multipart()] == [
        {header["name"]: header["value"] for header in part["headers"]} for part in reply["payload"]["parts"]
    ]
    # metadata shows the same headers, or those it names whatever their case.
    assert (reply_metadata["payload"]["mimeType"], reply_metadata["payload"]["headers"]) == (
        "multipart/alternative",
        reply["payload"]["headers"],
    )
    assert named["payload"]["headers"] == [{"name": "Content-Type", "value": content_type}]
    assert [message["payload"]["headers"][0] for message in thread["messages"]] == [
        {"name": "MIME-Version", "value": "1.0"}
    ] * 3


def test_a_gmail_label_list_carries_no_counts_and_a_label_get_does(
    external_services: RunningExternalServices,
) -> None:
    labels = _gmail(external_services, "me/labels")["labels"]
    inbox = _gmail(external_services, "me/labels/INBOX")

    for label in labels:
        assert set(label) <= {"id", "name", "messageListVisibility", "labelListVisibility", "type", "color"}, label
        assert {"id", "name", "type"} <= set(label), label
    hidden = {"messageListVisibility": "hide", "labelListVisibility": "labelHide"}
    visibility = {
        label["id"]: {key: value for key, value in label.items() if key.endswith("Visibility")}
        for label in labels
        if label["type"] == "system"
    }
    assert {label: visibility[label] for label in ("INBOX", "SENT", "STARRED", "UNREAD", "IMPORTANT", "SPAM")} == {
        "INBOX": {},
        "SENT": {},
        "STARRED": {},
        "UNREAD": {},
        "IMPORTANT": hidden,
        "SPAM": hidden,
    }
    assert inbox == {
        "id": "INBOX",
        "name": "INBOX",
        "type": "system",
        "messagesTotal": 2,
        "messagesUnread": 1,
        "threadsTotal": 1,
        "threadsUnread": 1,
    }


def test_gmail_lists_and_searches_with_the_operators_it_supports(external_services: RunningExternalServices) -> None:
    keys = _message_keys(external_services)
    question, reply, followup, newsletter = (
        keys[key] for key in ("invoice-question", "invoice-reply", "invoice-followup", "newsletter")
    )

    # Newest first; an archived message is listed, and only the mailbox's own.
    assert _listed_ids(external_services) == [followup, reply, question, newsletter]
    assert _listed_ids(external_services, labelIds="INBOX") == [followup, question]
    assert _listed_ids(external_services, q="from:me") == [reply]
    assert _listed_ids(external_services, q="label:Vendors is:unread") == [followup]
    assert _listed_ids(external_services, q='"corrected invoice"') == [followup]
    # An operator's quoted value is a phrase, matched whatever its case.
    for query in ('subject:"March invoice"', 'subject:"MARCH INVOICE"', '(subject:"march invoice")'):
        assert _listed_ids(external_services, q=query) == [followup, reply, question], query
    assert _listed_ids(external_services, q='subject:"invoice March"') == []
    assert _listed_ids(external_services, q='from:"Priya Shah" label:"Vendors"') == [followup, question]
    assert _listed_ids(external_services, q='subject:"March invoice" -label:"Vendors"') == [reply]
    # A date is midnight Pacific time, as Gmail reads it, and newer_than counts from the trial's present.
    day = _shifted_date("2026-03-16")
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    assert _listed_ids(
        external_services, q="after:{} before:{}".format(day.replace("-", "/"), next_day.replace("-", "/"))
    ) == [reply, question]
    assert _listed_ids(external_services, q="newer_than:3d") == [followup]
    assert _listed_ids(external_services, "threads") == [question, newsletter]
    # Gmail leaves an empty list out, and refuses a malformed page token.
    assert _gmail(external_services, "me/messages", q="nothing-says-this") == {"resultSizeEstimate": 0}
    assert _gmail(external_services, "me/threads", q="nothing-says-this") == {"resultSizeEstimate": 0}
    paged = _gmail(external_services, "me/messages", maxResults="2")
    assert (len(paged["messages"]), _listed_ids(external_services, pageToken=paged["nextPageToken"])) == (
        2,
        [question, newsletter],
    )
    bad_token = _gmail_get(external_services, "me/messages", pageToken="not-a-token")
    assert (bad_token.status_code, bad_token.json()["error"]["status"]) == (400, "INVALID_ARGUMENT")
    # A metadata header is matched whatever its case, as Gmail matches it.
    metadata = _gmail(
        external_services, "me/messages/{}".format(question), format="metadata", metadataHeaders="subject"
    )
    assert metadata["payload"]["headers"] == [{"name": "Subject", "value": "March invoice question"}]
    # A word with a colon that names no Gmail operator is a word to Gmail, and no gap of the fake's.
    assert _gmail(external_services, "me/messages", q="see:(notes) constructor:x https://northwind.example") == {
        "resultSizeEstimate": 0
    }
    assert _control(external_services, "health")["unsupported_calls"] == {}


def test_gmail_reads_a_get_format_whatever_its_case(external_services: RunningExternalServices) -> None:
    keys = _message_keys(external_services)

    message, thread = "me/messages/{}".format(keys["invoice-reply"]), "me/threads/{}".format(keys["invoice-question"])
    gets = [(message, name) for name in ("raw", "minimal", "metadata", "full")]
    gets += [(thread, name) for name in ("minimal", "metadata", "full")]
    for path, format_name in gets:
        upper = _gmail(external_services, path, format=format_name.upper())
        assert upper == _gmail(external_services, path, format=format_name), (path, format_name)
    raw = _gmail(external_services, message, format="RAW")
    assert "raw" in raw and "payload" not in raw
    assert _control(external_services, "health")["unsupported_calls"] == {}


def test_a_gmail_get_costs_what_its_format_costs_whatever_its_case(
    external_services: RunningExternalServices,
) -> None:
    message_id = _message_keys(external_services)["invoice-question"]
    # A RAW get costs 60 of a user's 6,000 units a minute, as raw does: 100 fit, and the 101st does not.
    burst = [
        _gmail_get(external_services, "me/messages/{}".format(message_id), format="RAW").status_code
        for _ in range(101)
    ]

    assert burst == [200] * 100 + [403]


def test_a_gmail_search_operator_the_fake_answers_wrongly_is_counted(
    external_services: RunningExternalServices,
) -> None:
    for query in ("invoice OR newsletter", "-from:priya", "cc:thomas", "{invoice newsletter}", "older_than:1y"):
        assert _gmail_get(external_services, "me/messages", q=query).status_code == 200
    # Gmail reads `or` and `and` in anything but capitals as words; AND is its implicit AND.
    for query in ("invoice or newsletter", "invoice Or newsletter", "and", "invoice AND question"):
        assert _gmail_get(external_services, "me/messages", q=query).status_code == 200
    _gmail_get(external_services, "me/threads", q="subject:(invoice question) has:drive")

    assert _control(external_services, "health")["unsupported_calls"] == {
        "google:gmail:q:OR": 1,
        "google:gmail:q:or-word": 2,
        "google:gmail:q:and-word": 1,
        "google:gmail:q:-": 1,
        "google:gmail:q:cc": 1,
        "google:gmail:q:{}": 1,
        "google:gmail:q:older_than": 1,
        "google:gmail:q:subject:()": 1,
        "google:gmail:q:has": 1,
    }
    assert [line["unsupported"] for line in _control(external_services, "log")][-1] == "google:gmail:q:subject:()"


def test_a_google_path_or_host_the_fake_does_not_serve_is_googles_html_not_found_and_counted(
    external_services: RunningExternalServices,
) -> None:
    headers = {"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)}
    vacation = _gmail_get(external_services, "me/settings/vacation")
    vacation_again = _gmail_get(external_services, _MORGAN + "/settings/vacation")
    no_such_setting = _gmail_get(external_services, "me/settings/doesnotexist")
    no_such_method = _gmail_get(external_services, "me/nothinghere")
    drive = httpx2.get(
        "{}/drive/v3/files".format(external_services.google_url),
        headers={**headers, "X-Forwarded-Host": "www.googleapis.com"},
    )
    oauth = httpx2.get(
        "{}/o/oauth2/v2/auth".format(external_services.google_url),
        headers={**headers, "X-Forwarded-Host": "www.googleapis.com"},
    )
    other_host = httpx2.get(
        "{}/gmail/v1/users/me/profile".format(external_services.google_url),
        headers={**headers, "X-Forwarded-Host": "people.googleapis.com"},
    )
    # Gmail is served on both of its hosts.
    on_www = httpx2.get(
        "{}/gmail/v1/users/me/profile".format(external_services.google_url),
        headers={**headers, "X-Forwarded-Host": "www.googleapis.com"},
    )
    missing_message = _gmail_get(external_services, "me/messages/0000000000000000")

    # Google's frontends answer a path no API method takes with their HTML page, naming the path.
    for answer in (vacation, vacation_again, no_such_setting, no_such_method, drive, oauth, other_host):
        assert answer.status_code == 404
        assert answer.headers["content-type"] == "text/html; charset=UTF-8"
        assert answer.text.startswith("<!DOCTYPE html>")
        assert "<title>Error 404 (Not Found)!!1</title>" in answer.text
        assert "<code>{}</code> was not found on this server.".format(answer.request.url.path) in answer.text
    assert (on_www.status_code, on_www.json()["emailAddress"]) == (200, _MORGAN)
    # A message that is not there is the caller's mistake, as it is against Gmail, and not counted.
    assert (missing_message.status_code, missing_message.json()) == (
        404,
        {
            "error": {
                "code": 404,
                "message": "Requested entity was not found.",
                "errors": [{"message": "Requested entity was not found.", "domain": "global", "reason": "notFound"}],
                "status": "NOT_FOUND",
            }
        },
    )
    assert _control(external_services, "health")["unsupported_calls"] == {
        "google:gmail:GET users/{userId}/settings/vacation": 2,
        "google:gmail:GET users/{userId}/settings/doesnotexist": 1,
        "google:gmail:GET users/{userId}/nothinghere": 1,
        "google:path:/drive/v3": 1,
        "google:path:/o/oauth2": 1,
        "google:host:people.googleapis.com": 1,
    }
    # The access log reads an answer that is not JSON as neither ok nor an error, and keeps its gap.
    vacation_line = _control(external_services, "log")[0]
    assert (vacation_line["status"], vacation_line["ok"], vacation_line["error"], vacation_line["result_count"]) == (
        404,
        None,
        None,
        None,
    )
    assert vacation_line["unsupported"] == "google:gmail:GET users/{userId}/settings/vacation"


def test_a_google_token_reads_only_its_own_mailbox(external_services: RunningExternalServices) -> None:
    thomas_keys = _message_keys(external_services, "thomas.baird@redwood.example")
    morgan_keys = _message_keys(external_services)

    thomas = _gmail(external_services, "me/profile", token=_THOMAS_GOOGLE_TOKEN)
    thomas_listed = _gmail(external_services, "me/messages", token=_THOMAS_GOOGLE_TOKEN)
    across = _gmail_get(external_services, "me/messages/{}".format(thomas_keys["thomas-invoice-copy"]))
    delegated = _gmail_get(external_services, "thomas.baird@redwood.example/messages")
    anonymous = _gmail_get(external_services, "me/profile", token=None)
    unknown = _gmail_get(external_services, "me/profile", token="ya29.other")

    assert (thomas["emailAddress"], thomas["messagesTotal"]) == ("thomas.baird@redwood.example", 1)
    assert [entry["id"] for entry in thomas_listed["messages"]] == [thomas_keys["thomas-invoice-copy"]]
    # Thomas's copy of the question is cc'd mail in his INBOX, a message of his own apart from Morgan's.
    assert thomas_keys["thomas-invoice-copy"] not in morgan_keys.values()
    assert across.status_code == 404
    # Gmail names the caller's own address, not the mailbox it asked for.
    assert (delegated.status_code, delegated.json()) == (
        403,
        {
            "error": {
                "code": 403,
                "message": "Delegation denied for {}".format(_MORGAN),
                "errors": [
                    {"message": "Delegation denied for {}".format(_MORGAN), "domain": "global", "reason": "forbidden"}
                ],
                "status": "PERMISSION_DENIED",
            }
        },
    )
    assert (anonymous.status_code, anonymous.json()["error"]["errors"][0]["reason"]) == (401, "required")
    assert (unknown.status_code, unknown.json()["error"]["errors"][0]["reason"]) == (401, "authError")


def test_a_gmail_update_lands_in_its_thread_as_newer_history(external_services: RunningExternalServices) -> None:
    before = _gmail(external_services, "me/profile")

    added = _add(
        external_services, "google", external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "google")
    )
    after = _gmail(external_services, "me/profile")
    keys = _message_keys(external_services)
    history = _gmail(external_services, "me/history", startHistoryId=before["historyId"])

    assert (added.status_code, added.json()["added"]) == (
        200,
        {"google_users": 1, "gmail_labels": 1, "gmail_messages": 1, "calendar_calendars": 1, "calendar_events": 1},
    )
    assert (after["messagesTotal"], after["threadsTotal"]) == (5, 2)
    assert int(after["historyId"]) > int(before["historyId"])
    assert [entry["messagesAdded"][0]["message"]["id"] for entry in history["history"]] == [keys["corrected-invoice"]]
    thread = _gmail(external_services, "me/threads/{}".format(keys["invoice-question"]), format="minimal")
    assert thread["messages"][-1]["id"] == keys["corrected-invoice"]
    assert thread["messages"][-1]["internalDate"] == str(_shifted_ms("2026-03-20T15:30:00-07:00"))
    assert _listed_ids(external_services, q="label:Finance") == [keys["corrected-invoice"]]
    users = _control(external_services, "state")["google"]["collections"]["google.users"]["items"]
    assert "olivia.chen@redwood.example" in {user["email"] for user in users}


def test_a_burst_past_gmails_per_user_quota_is_refused_as_gmail_refuses_it(
    external_services: RunningExternalServices,
) -> None:
    message_id = _message_keys(external_services)["invoice-question"]
    # A full get costs 60 of a user's 6,000 units a minute: 100 fit, and the 101st does not.
    with httpx2.Client(headers={"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)}) as client:
        burst = [
            client.get("{}/gmail/v1/users/me/messages/{}".format(external_services.google_url, message_id))
            for _ in range(101)
        ]
    # The quota is each user's own, and a cheaper call does not fit either once it is spent.
    cheap = _gmail_get(external_services, "me/profile")
    thomas = _gmail_get(external_services, "me/profile", token=_THOMAS_GOOGLE_TOKEN)

    assert [answer.status_code for answer in burst[:100]] == [200] * 100
    refused = burst[100]
    assert refused.status_code == 403
    assert refused.json() == {
        "error": {
            "code": 403,
            "message": "User Rate Limit Exceeded",
            "errors": [
                {"message": "User Rate Limit Exceeded", "domain": "usageLimits", "reason": "userRateLimitExceeded"}
            ],
            "status": "PERMISSION_DENIED",
        }
    }
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    # Gmail documents no rate limit headers, so an admitted answer carries none.
    assert not {name for name in burst[0].headers if name.startswith(("x-ratelimit", "retry-after"))}
    assert (cheap.status_code, thomas.status_code) == (403, 200)
    assert _control(external_services, "health")["rate_limited"] == {"google:gmail_per_user": 2}


_THOMAS = "thomas.baird@redwood.example"


def _calendar(services: RunningExternalServices, path: str, token: str = _GOOGLE_TOKEN, **params: str) -> Any:
    answer = _calendar_get(services, path, token=token, **params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _event_ids(services: RunningExternalServices) -> dict[str, str]:
    """Each seeded event's id by its key, as the state dump holds them."""
    items = _control(services, "state")["google"]["collections"]["google.calendar_events"]["items"]
    return {item["seed_key"]: item["google_id"] for item in items if item["seed_key"]}


def _listed_summaries(services: RunningExternalServices, token: str = _GOOGLE_TOKEN, **params: str) -> list[str]:
    return [
        event["summary"] for event in _calendar(services, "calendars/primary/events", token=token, **params)["items"]
    ]


def _shifted_date_time(stamp: str) -> str:
    """A seed's instant as Calendar serves an event's dateTime under the test's shift: in the offset
    it was written in, to the second."""
    moved = datetime.fromisoformat(stamp) + timedelta(seconds=EXTERNAL_SERVICES_TEST_SHIFT_SECONDS)
    return moved.isoformat(timespec="seconds").replace("+00:00", "Z")


def _calendar_error(code: int, message: str, reason: str, **extra: str) -> dict[str, Any]:
    return {
        "error": {
            "errors": [{"domain": "global", "reason": reason, "message": message, **extra}],
            "code": code,
            "message": message,
        }
    }


def test_a_calendar_list_and_a_calendar_name_the_account_its_token_belongs_to(
    external_services: RunningExternalServices,
) -> None:
    listed = _calendar(external_services, "users/me/calendarList")
    by_primary = _calendar(external_services, "users/me/calendarList/primary")
    by_address = _calendar(external_services, "users/me/calendarList/{}".format(_MORGAN))
    calendar = _calendar(external_services, "calendars/primary")
    thomas = _calendar(external_services, "calendars/primary", token=_THOMAS_GOOGLE_TOKEN)
    probe = next(
        credential.probe for credential in service_named("google").credentials if credential.name == "google-calendar"
    )

    assert (listed["kind"], [entry["id"] for entry in listed["items"]]) == ("calendar#calendarList", [_MORGAN])
    (entry,) = listed["items"]
    assert entry == by_primary == by_address
    assert {key: entry[key] for key in ("kind", "summary", "timeZone", "accessRole", "primary", "selected")} == {
        "kind": "calendar#calendarListEntry",
        "summary": _MORGAN,
        # Most of Morgan's events are written at -07:00, so Morgan's calendar keeps that zone.
        "timeZone": "Etc/GMT+7",
        "accessRole": "owner",
        "primary": True,
        "selected": True,
    }
    assert entry["defaultReminders"] == [{"method": "popup", "minutes": 10}]
    assert {key: calendar[key] for key in ("kind", "id", "summary", "timeZone")} == {
        "kind": "calendar#calendar",
        "id": _MORGAN,
        "summary": _MORGAN,
        "timeZone": "Etc/GMT+7",
    }
    # Thomas's one event is written in UTC.
    assert (thomas["id"], thomas["timeZone"]) == (_THOMAS, "UTC")
    # The probe that shows a workspace reaches the fake reads the token's account off this answer.
    assert probe_identities(probe, json.dumps(calendar)) == (_MORGAN,)


def test_a_seeded_calendar_event_is_served_as_calendar_serves_one(external_services: RunningExternalServices) -> None:
    ids = _event_ids(external_services)
    event = _calendar(external_services, "calendars/primary/events/{}".format(ids["vendor-sync"]))
    offsite = _calendar(external_services, "calendars/{}/events/{}".format(_MORGAN, ids["offsite"]))
    listed = {item["id"]: item for item in _calendar(external_services, "calendars/primary/events")["items"]}

    created = _shifted_iso("2026-03-17T17:00:00.000Z")
    assert event == {
        "kind": "calendar#event",
        "etag": event["etag"],
        "id": ids["vendor-sync"],
        "status": "confirmed",
        "htmlLink": event["htmlLink"],
        "created": created,
        "updated": created,
        "summary": "Northwind invoice sync",
        "description": "Walk through invoice 4411 with Priya.",
        "location": "Room 4B",
        "creator": {"email": _MORGAN, "self": True},
        "organizer": {"email": _MORGAN, "self": True},
        "start": {"dateTime": _shifted_date_time("2026-03-17T10:00:00-07:00"), "timeZone": "Etc/GMT+7"},
        "end": {"dateTime": _shifted_date_time("2026-03-17T10:30:00-07:00"), "timeZone": "Etc/GMT+7"},
        "iCalUID": "{}@google.com".format(ids["vendor-sync"]),
        "sequence": 0,
        # The organizer is a guest of an event that has guests.
        "attendees": [
            {"email": _MORGAN, "organizer": True, "self": True, "responseStatus": "accepted"},
            {"email": "priya@northwind.example", "displayName": "Priya Shah", "responseStatus": "accepted"},
            {"email": _THOMAS, "displayName": "Thomas Baird", "responseStatus": "tentative"},
        ],
        "reminders": {"useDefault": True},
        "eventType": "default",
    }
    assert listed[ids["vendor-sync"]] == event
    # Ids are lowercase base32hex, as Calendar's are, and the link names the event on its calendar.
    assert all(
        len(event_id) == 26 and set(event_id) <= set("0123456789abcdefghijklmnopqrstuv") for event_id in ids.values()
    )
    eid = event["htmlLink"].removeprefix("https://www.google.com/calendar/event?eid=")
    assert base64.b64decode(eid + "=" * (-len(eid) % 4)).decode() == "{} {}".format(ids["vendor-sync"], _MORGAN)
    assert event["etag"] == '"{}"'.format(_shifted_ms("2026-03-17T17:00:00+00:00") * 2000)
    # An all-day event is its days, the end exclusive; it is after the seed's anchor, so it was
    # created by the time the data ends.
    assert (offsite["start"], offsite["end"]) == (
        {"date": _shifted_date("2026-03-26")},
        {"date": _shifted_date("2026-03-28")},
    )
    assert offsite["created"] == _shifted_iso("2026-03-21T00:00:00.000Z")
    assert "attendees" not in offsite and "location" not in offsite


def test_every_calendar_date_is_the_seeds_or_derived_from_it(external_services: RunningExternalServices) -> None:
    morgan = _calendar(external_services, "calendars/primary/events")
    thomas = _calendar(external_services, "calendars/primary/events", token=_THOMAS_GOOGLE_TOKEN)
    calendars = _calendar(external_services, "users/me/calendarList")

    # An event was created at the earlier of its start and the anchor.
    assert {event["summary"]: event["created"] for event in morgan["items"] + thomas["items"]} == {
        "Northwind invoice sync": _shifted_iso("2026-03-17T17:00:00.000Z"),
        "Q1 finance review": _shifted_iso("2026-03-21T00:00:00.000Z"),
        "Team offsite": _shifted_iso("2026-03-21T00:00:00.000Z"),
        "Planning block": _shifted_iso("2026-03-18T09:00:00.000Z"),
    }
    # A calendar was last changed with its latest event; its account, and so the calendar itself,
    # dates from the account's earliest record, Morgan's an email.
    assert (morgan["updated"], thomas["updated"]) == (
        _shifted_iso("2026-03-21T00:00:00.000Z"),
        _shifted_iso("2026-03-18T09:00:00.000Z"),
    )
    assert calendars["items"][0]["etag"] == '"{}"'.format(_shifted_ms("2026-03-12T06:00:00+00:00") * 2000)
    _assert_no_date_is_near_the_wall_clock(morgan, thomas)


def test_calendar_lists_events_by_time_bounds_order_and_pages(external_services: RunningExternalServices) -> None:
    shifted = _shifted_iso
    everything = _calendar(external_services, "calendars/primary/events")
    # timeMin bounds an event's end and timeMax its start, both exclusively.
    after_sync = _listed_summaries(external_services, timeMin=shifted("2026-03-17T17:30:00.000Z"))
    before_review = _listed_summaries(external_services, timeMax=shifted("2026-03-24T21:00:00.000Z"))
    within = _listed_summaries(
        external_services, timeMin=shifted("2026-03-20T00:00:00.000Z"), timeMax=shifted("2026-03-27T00:00:00.000Z")
    )
    by_start = _listed_summaries(external_services, orderBy="startTime", singleEvents="true")
    by_update = _listed_summaries(external_services, orderBy="updated")
    unordered = _calendar_get(external_services, "calendars/primary/events", orderBy="startTime")
    empty_range = _calendar_get(
        external_services,
        "calendars/primary/events",
        timeMin=shifted("2026-03-20T00:00:00.000Z"),
        timeMax=shifted("2026-03-20T00:00:00.000Z"),
    )
    no_offset = _calendar_get(external_services, "calendars/primary/events", timeMin="2036-03-20T00:00:00")

    assert set(everything) == {
        "kind",
        "etag",
        "summary",
        "updated",
        "timeZone",
        "accessRole",
        "defaultReminders",
        "items",
    }
    assert (everything["kind"], everything["summary"], everything["timeZone"], everything["accessRole"]) == (
        "calendar#events",
        _MORGAN,
        "Etc/GMT+7",
        "owner",
    )
    assert after_sync == ["Q1 finance review", "Team offsite"]
    assert before_review == ["Northwind invoice sync"]
    assert within == ["Q1 finance review", "Team offsite"]
    assert by_start == ["Northwind invoice sync", "Q1 finance review", "Team offsite"]
    # The two events created at the anchor tie on their update, and fall back to their start.
    assert by_update == ["Northwind invoice sync", "Q1 finance review", "Team offsite"]
    assert (unordered.status_code, unordered.json()) == (
        400,
        _calendar_error(400, "The requested ordering is not available for the particular query.", "badRequest"),
    )
    assert (empty_range.status_code, empty_range.json()["error"]["errors"][0]) == (
        400,
        {
            "domain": "calendar",
            "reason": "timeRangeEmpty",
            "message": "The specified time range is empty.",
            "locationType": "parameter",
            "location": "timeMax",
        },
    )
    assert (no_offset.status_code, no_offset.json()) == (400, _calendar_error(400, "Bad Request", "badRequest"))
    # Calendar returns an empty list rather than leaving it out.
    assert (
        _calendar(external_services, "calendars/primary/events", timeMax=shifted("2026-01-01T00:00:00.000Z"))["items"]
        == []
    )


def test_calendar_pages_250_events_at_a_time_unless_asked_otherwise(
    external_services: RunningExternalServices,
) -> None:
    many = [
        {
            "key": "standup-{}".format(day),
            "owner": _MORGAN,
            "summary": "Standup {}".format(day),
            "start": "2026-02-{:02d}T09:00:00-07:00".format(day),
            "end": "2026-02-{:02d}T09:15:00-07:00".format(day),
        }
        for day in range(1, 13)
    ]
    assert _add(external_services, "google", json.dumps({"events": many})).status_code == 200

    whole = _calendar(external_services, "calendars/primary/events")
    first = _calendar(external_services, "calendars/primary/events", maxResults="5")
    second = _calendar(external_services, "calendars/primary/events", maxResults="5", pageToken=first["nextPageToken"])
    last = _calendar(external_services, "calendars/primary/events", maxResults="5", pageToken=second["nextPageToken"])
    pages = [[event["summary"] for event in page["items"]] for page in (first, second, last)]
    malformed = _calendar_get(
        external_services, "calendars/primary/events", pageToken="CiAKGjBpNDd2Nmp2Zml2cXRwYjBpOXA"
    )
    none_asked = _calendar_get(external_services, "calendars/primary/events", maxResults="0")

    # The package pages ten at a time; Calendar 250.
    assert len(whole["items"]) == 15 and "nextPageToken" not in whole
    assert [len(page) for page in pages] == [5, 5, 5]
    assert "nextPageToken" not in last
    assert [summary for page in pages for summary in page] == [event["summary"] for event in whole["items"]]
    assert (malformed.status_code, malformed.json()["error"]["errors"][0]["location"]) == (400, "pageToken")
    assert (none_asked.status_code, none_asked.json()["error"]["errors"][0]["location"]) == (400, "maxResults")


def test_an_inserted_calendar_event_is_listed_and_a_deleted_one_is_cancelled(
    external_services: RunningExternalServices,
) -> None:
    headers = {"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)}
    events_url = "{}/calendar/v3/calendars/primary/events".format(external_services.google_url)
    # A day between the finance review and the offsite, as the trial sees them; Los Angeles keeps
    # daylight time then, as the calendar's own -07:00 does.
    day = _shifted_date("2026-03-25")
    inserted = httpx2.post(
        events_url,
        headers=headers,
        json={
            "summary": "Invoice follow-up",
            "start": {"dateTime": "{}T09:00:00".format(day), "timeZone": "America/Los_Angeles"},
            "end": {"dateTime": "{}T16:30:00Z".format(day)},
            "attendees": [{"email": "priya@northwind.example", "optional": True, "responseStatus": "maybe"}],
            "recurrence": ["RRULE:FREQ=WEEKLY"],
        },
    )
    assert inserted.status_code == 200, inserted.text
    event = inserted.json()
    got = _calendar(external_services, "calendars/primary/events/{}".format(event["id"]))
    listed = _listed_summaries(external_services, orderBy="startTime", singleEvents="true")
    sync_id = _event_ids(external_services)["vendor-sync"]
    deleted = httpx2.delete("{}/{}".format(events_url, sync_id), headers=headers)
    after_delete = _listed_summaries(external_services)
    with_deleted = _calendar(external_services, "calendars/primary/events", showDeleted="true")["items"]
    cancelled = _calendar(external_services, "calendars/primary/events/{}".format(sync_id))
    deleted_again = httpx2.delete("{}/{}".format(events_url, sync_id), headers=headers)
    no_end = httpx2.post(events_url, headers=headers, json={"summary": "x", "start": {"date": day}})
    one_day = {"start": {"date": day}, "end": {"date": _shifted_date("2026-03-26")}}
    tentative = httpx2.post(events_url, headers=headers, json={"summary": "Maybe", "status": "tentative", **one_day})
    born_cancelled = httpx2.post(
        events_url, headers=headers, json={"summary": "Gone", "status": "cancelled", **one_day}
    )

    assert got == event
    assert (event["start"], event["end"]) == (
        {"dateTime": "{}T09:00:00-07:00".format(day), "timeZone": "America/Los_Angeles"},
        {"dateTime": "{}T09:30:00-07:00".format(day), "timeZone": "Etc/GMT+7"},
    )
    assert [(a["email"], a["responseStatus"]) for a in event["attendees"]] == [
        (_MORGAN, "accepted"),
        ("priya@northwind.example", "needsAction"),
    ]
    assert listed == ["Northwind invoice sync", "Q1 finance review", "Invoice follow-up", "Team offsite"]
    assert (deleted.status_code, deleted.text) == (204, "")
    assert "Northwind invoice sync" not in after_delete
    assert {e["summary"]: e["status"] for e in with_deleted}["Northwind invoice sync"] == "cancelled"
    assert cancelled["status"] == "cancelled"
    assert (deleted_again.status_code, deleted_again.json()) == (
        410,
        _calendar_error(410, "Resource has been deleted", "deleted"),
    )
    assert (no_end.status_code, no_end.json()) == (400, _calendar_error(400, "Missing end time.", "required"))
    assert (tentative.json()["status"], born_cancelled.json()["status"]) == ("tentative", "confirmed")
    # The insert's recurrence, a guest's optional flag and unknown response, and a status a new event
    # cannot have are not kept, and each is counted.
    assert _control(external_services, "health")["unsupported_calls"] == {
        "google:calendar:body:recurrence": 1,
        "google:calendar:body:attendees.optional": 1,
        "google:calendar:body:attendees.responseStatus": 1,
        "google:calendar:body:status": 1,
    }


def test_another_accounts_calendar_is_not_found(external_services: RunningExternalServices) -> None:
    """Calendar answers a calendar the caller has no access to as one that does not exist, with a 404
    rather than a 403, so the fake does the same for another seeded account's calendar and for an
    event on it."""
    thomas_event = _event_ids(external_services)["thomas-planning"]
    answers = [
        _calendar_get(external_services, "calendars/{}/events".format(_THOMAS)),
        _calendar_get(external_services, "calendars/{}".format(_THOMAS)),
        _calendar_get(external_services, "users/me/calendarList/{}".format(_THOMAS)),
        _calendar_get(external_services, "calendars/primary/events/{}".format(thomas_event)),
        _calendar_get(external_services, "calendars/{}/events/{}".format(_THOMAS, thomas_event)),
    ]
    anonymous = _calendar_get(external_services, "calendars/primary", token=None)

    for answer in answers:
        assert (answer.status_code, answer.json()) == (404, _calendar_error(404, "Not Found", "notFound"))
    assert (anonymous.status_code, anonymous.json()["error"]["errors"][0]["reason"]) == (401, "required")
    assert _control(external_services, "health")["unsupported_calls"] == {}


def test_a_calendar_method_or_parameter_the_fake_lacks_is_googles_not_found_or_served_and_counted(
    external_services: RunningExternalServices,
) -> None:
    headers = {"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)}
    base = "{}/calendar/v3".format(external_services.google_url)
    sync_id = _event_ids(external_services)["vendor-sync"]
    lacking = [
        httpx2.patch("{}/calendars/primary/events/{}".format(base, sync_id), headers=headers, json={"summary": "x"}),
        httpx2.post("{}/calendars/primary/events/quickAdd".format(base), headers=headers, params={"text": "x"}),
        httpx2.get("{}/calendars/primary/events/{}/instances".format(base, sync_id), headers=headers),
        httpx2.post("{}/freeBusy".format(base), headers=headers, json={}),
        httpx2.get("{}/colors".format(base), headers=headers),
        httpx2.get("{}/users/me/settings/timezone".format(base), headers=headers),
    ]
    with_parameters = [
        _calendar_get(external_services, "calendars/primary/events", syncToken="CPDAlvWDx70CEPDAlvWDx70CGAU="),
        _calendar_get(external_services, "calendars/primary/events", iCalUID="x@google.com"),
        _calendar_get(external_services, "calendars/primary/events", q="invoice", prettyPrint="false"),
        _calendar_get(external_services, "calendars/primary/events", fields="items(summary)"),
    ]

    for answer in lacking:
        assert answer.status_code == 404
        assert answer.headers["content-type"] == "text/html; charset=UTF-8"
        assert "<code>{}</code> was not found on this server.".format(answer.request.url.path) in answer.text
    assert [answer.status_code for answer in with_parameters] == [200] * 4
    # A search is answered by the words in an event's text.
    assert [event["summary"] for event in with_parameters[2].json()["items"]] == ["Northwind invoice sync"]
    assert _control(external_services, "health")["unsupported_calls"] == {
        "google:calendar:PATCH calendars/{calendarId}/events/{eventId}": 1,
        "google:calendar:POST calendars/{calendarId}/events/quickAdd": 1,
        "google:calendar:GET calendars/{calendarId}/events/{eventId}/instances": 1,
        "google:calendar:POST freeBusy": 1,
        "google:calendar:GET colors": 1,
        "google:calendar:GET users/me/settings/{setting}": 1,
        "google:calendar:syncToken": 1,
        "google:calendar:iCalUID": 1,
        "google:calendar:q": 1,
        "google:calendar:fields": 1,
    }


def test_a_calendar_update_adds_its_events_with_the_seeds_shift(external_services: RunningExternalServices) -> None:
    added = _add(
        external_services, "google", external_services_fixture_text(EXTERNAL_SERVICES_UPDATE_FIXTURE, "google")
    )
    calendars = _control(external_services, "state")["google"]["collections"]["google.calendars"]["items"]
    call = next(
        event
        for event in _calendar(external_services, "calendars/primary/events")["items"]
        if event["summary"] == "Corrected invoice call"
    )

    assert added.status_code == 200, added.text
    assert added.json()["added"]["calendar_events"] == 1
    assert call["start"] == {"dateTime": _shifted_date_time("2026-03-23T09:00:00-07:00"), "timeZone": "Etc/GMT+7"}
    assert call["id"] == _event_ids(external_services)["corrected-invoice-call"]
    assert [a["email"] for a in call["attendees"]] == [_MORGAN, "olivia.chen@redwood.example"]
    # The update's new account has its primary calendar too, in UTC with no event of its own.
    assert {calendar["google_id"]: calendar["time_zone"] for calendar in calendars}[
        "olivia.chen@redwood.example"
    ] == "UTC"


def test_a_burst_past_calendars_per_user_quota_is_refused_as_calendar_refuses_it(
    external_services: RunningExternalServices,
) -> None:
    # Every request costs one of a user's 600 a minute: 600 fit, and the 601st does not.
    with httpx2.Client(headers={"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)}) as client:
        burst = [
            client.get("{}/calendar/v3/calendars/primary".format(external_services.google_url)) for _ in range(601)
        ]
    # The quota is each user's own and Calendar's own: Thomas, and Morgan's Gmail, are still served.
    thomas = _calendar_get(external_services, "calendars/primary", token=_THOMAS_GOOGLE_TOKEN)
    gmail = _gmail_get(external_services, "me/profile")

    assert [answer.status_code for answer in burst[:600]] == [200] * 600
    refused = burst[600]
    assert (refused.status_code, refused.json()) == (
        403,
        {
            "error": {
                "errors": [
                    {"domain": "usageLimits", "reason": "userRateLimitExceeded", "message": "User Rate Limit Exceeded"}
                ],
                "code": 403,
                "message": "User Rate Limit Exceeded",
            }
        },
    )
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert not {name for name in burst[0].headers if name.startswith(("x-ratelimit", "retry-after"))}
    assert (thomas.status_code, gmail.status_code) == (200, 200)
    assert _control(external_services, "health")["rate_limited"] == {"google:calendar_per_user": 1}
