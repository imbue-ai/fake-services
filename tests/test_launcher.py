"""The real launcher on the fixture seeds, driven over HTTP."""

import base64
import email
import json
import time
from collections.abc import Callable
from collections.abc import Iterator
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Any

import httpx
import pytest

from conftest import TEST_FAULTS
from conftest import TEST_LINEAR_REQUEST_LIMIT
from conftest import TEST_RATE_LIMIT_WINDOW_SECONDS
from conftest import TEST_SHIFT_SECONDS
from conftest import RunningFakes
from conftest import fixture_document
from conftest import fixture_text

_SLACK_TOKEN = "xoxb-fake-redwood-slack-token"
_LINEAR_TOKEN = "lin_api_fakeredwoodlineartoken"
_SLACK_HEADERS = {"Authorization": "Bearer {}".format(_SLACK_TOKEN)}


def _slack(services: RunningFakes, method: str, **params: str) -> dict[str, Any]:
    return httpx.get("{}/api/{}".format(services.slack_url, method), params=params, headers=_SLACK_HEADERS).json()


def _linear(
    services: RunningFakes,
    query: str,
    token: str = _LINEAR_TOKEN,
    variables: dict[str, Any] | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"query": query} if variables is None else {"query": query, "variables": variables}
    return httpx.post("{}/graphql".format(services.linear_url), json=body, headers={"Authorization": token})


def _control(services: RunningFakes, route: str) -> Any:
    return httpx.get("{}/_control/{}".format(services.control_url, route)).json()


def _add(services: RunningFakes, service_name: str, update_text: str) -> httpx.Response:
    return httpx.post("{}/_control/records/{}".format(services.control_url, service_name), content=update_text)


def _add_after(
    services: RunningFakes, service_name: str, after: str, update: dict[str, Any]
) -> httpx.Response:
    """Post a step's update for a service, naming the step it follows as the driver does."""
    return httpx.post(
        "{}/_control/records/{}".format(services.control_url, service_name),
        params={"after": after},
        content=json.dumps(update),
    )


def _instant(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _issue_titles(services: RunningFakes) -> dict[str, str]:
    answer = _linear(services, "{ issues { nodes { identifier title } } }")
    return {issue["identifier"]: issue["title"] for issue in answer.json()["data"]["issues"]["nodes"]}


def _channel_id(services: RunningFakes, name: str) -> str:
    channels = _slack(services, "conversations.list", types="public_channel,private_channel")["channels"]
    return next(channel["id"] for channel in channels if channel["name"] == name)


def _shifted(ts: str) -> str:
    seconds, fraction = ts.split(".")
    return "{}.{}".format(int(seconds) + TEST_SHIFT_SECONDS, fraction)


def _shifted_iso(stamp: str) -> str:
    """A seed's timestamp as the launcher serves it under the test's shift."""
    moved = _instant(stamp) + timedelta(seconds=TEST_SHIFT_SECONDS)
    return moved.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _shifted_seconds(ts: str) -> int:
    return int(ts.split(".")[0]) + TEST_SHIFT_SECONDS


def _shifted_date(day: str) -> str:
    """A seed's calendar date as the launcher serves it under the test's shift."""
    return (date.fromisoformat(day) + timedelta(seconds=TEST_SHIFT_SECONDS)).isoformat()


# Every key under which a fake serves a record's date, and the instant each names in epoch seconds.
_DATE_KEYS = ("createdAt", "updatedAt", "completedAt", "startDate", "targetDate", "created", "updated", "last_set")


def _served_dates(answer: Any) -> Iterator[float]:
    if isinstance(answer, dict):
        for key, value in answer.items():
            if key in _DATE_KEYS and isinstance(value, str):
                yield _instant(value).timestamp()
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
    services: RunningFakes, path: str, token: str | None = _GOOGLE_TOKEN, **params: str
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": "Bearer {}".format(token)}
    return httpx.get("{}/gmail/v1/users/{}".format(services.google_url, path), params=params, headers=headers)


def _gmail(services: RunningFakes, path: str, **params: str) -> Any:
    answer = _gmail_get(services, path, **params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _message_keys(services: RunningFakes, mailbox: str = _MORGAN) -> dict[str, str]:
    """Each seeded message's Gmail id by its key, as the state dump holds them."""
    items = _control(services, "state")["google"]["collections"]["google.messages"]["items"]
    return {item["seed_key"]: item["gmail_id"] for item in items if item["user_email"] == mailbox}


def _listed_ids(services: RunningFakes, kind: str = "messages", **params: str) -> list[str]:
    return [entry["id"] for entry in _gmail(services, "me/{}".format(kind), **params).get(kind, [])]


def _calendar_get(
    services: RunningFakes, path: str, token: str | None = _GOOGLE_TOKEN, **params: str
) -> httpx.Response:
    # Calendar lives on www.googleapis.com alone, which the shim names for every request it routes.
    headers = {"X-Forwarded-Host": "www.googleapis.com"}
    if token is not None:
        headers["Authorization"] = "Bearer {}".format(token)
    return httpx.get("{}/calendar/v3/{}".format(services.google_url, path), params=params, headers=headers)


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {header["name"]: header["value"] for header in message["payload"]["headers"]}


def _userinfo(services: RunningFakes, token: str | None = _GOOGLE_TOKEN) -> httpx.Response:
    headers = {"X-Forwarded-Host": "openidconnect.googleapis.com"}
    if token is not None:
        headers["Authorization"] = "Bearer {}".format(token)
    return httpx.get("{}/v1/userinfo".format(services.google_url), headers=headers)


def _shifted_ms(stamp: str) -> int:
    return round((datetime.fromisoformat(stamp).timestamp() + TEST_SHIFT_SECONDS) * 1000)


def _shifted_date_header(stamp: str) -> str:
    """A seed's date as a Date header serves it under the test's shift: in the offset the seed states,
    with the day unpadded as Gmail writes it."""
    moment = datetime.fromisoformat(stamp) + timedelta(seconds=TEST_SHIFT_SECONDS)
    return "{:%a}, {} {:%b %Y %H:%M:%S %z}".format(moment, moment.day, moment)


def test_the_stores_hold_the_seed_and_nothing_of_the_emulators_own(fakes: RunningFakes) -> None:
    identity = _slack(fakes, "auth.test")
    channels = _slack(fakes, "conversations.list")["channels"]
    members = _slack(fakes, "users.list")["members"]
    viewer = _linear(
        fakes, "{ viewer { id name email } organization { name urlKey } users { nodes { email } } }"
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


def _user_ids(services: RunningFakes) -> dict[str, str]:
    return {member["name"]: member["id"] for member in _slack(services, "users.list")["members"]}


def _members(services: RunningFakes, channel_name: str) -> set[str]:
    by_id = {user_id: name for name, user_id in _user_ids(services).items()}
    answer = _slack(services, "conversations.members", channel=_channel_id(services, channel_name))
    return {by_id[user_id] for user_id in answer["members"]}


def test_a_channels_members_are_the_users_who_posted_in_it_and_those_it_names(
    fakes: RunningFakes,
) -> None:
    seeded = {name: _members(fakes, name) for name in ("announcements", "eng-oncall")}
    added = _add(
        fakes,
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
    assert _members(fakes, "eng-oncall") == {"pagerbot", "claire", "morgan.reyes", "olivia_chen"}
    assert _members(fakes, "leads") == {"claire", "morgan.reyes"}
    assert _members(fakes, "quiet") == {"pagerbot"}
    user_ids = _user_ids(fakes)
    channels = {channel["name"]: channel for channel in _slack(fakes, "conversations.list")["channels"]}
    assert {name: (channel["creator"], channel["num_members"]) for name, channel in channels.items()} == {
        "announcements": (user_ids["claire"], 1),
        "eng-oncall": (user_ids["pagerbot"], 4),
        "quiet": (user_ids["pagerbot"], 1),
    }
    # A public channel reads the same to a token whose user never posted in it, as with a Slack user
    # token; the private channel is listed only to its members.
    history = _slack(fakes, "conversations.history", channel=channels["announcements"]["id"])
    assert [message["user"] for message in history["messages"]] == [user_ids["claire"]]
    private = _slack(fakes, "conversations.list", types="private_channel")["channels"]
    assert [channel["name"] for channel in private] == ["leads"]


def test_timestamps_are_shifted_by_the_start_minus_the_anchor(fakes: RunningFakes) -> None:
    channel_id = _channel_id(fakes, "eng-oncall")

    history = _slack(fakes, "conversations.history", channel=channel_id)
    issue = _linear(
        fakes, '{ issue(id: "ENG-2") { createdAt updatedAt comments { nodes { createdAt } } } }'
    )

    assert _control(fakes, "health")["shift_ms"] == TEST_SHIFT_SECONDS * 1000
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
    fakes: RunningFakes,
) -> None:
    """Only issues and comments state their dates in the fixture seed, besides Morgan's creation: a
    project, cycle, user, label, team and state is dated from the issues and comments around it, and
    the organization from the earliest record of all. Morgan was last updated by the first issue
    they created."""
    data = _linear(fakes, _LINEAR_DATES_QUERY).json()["data"]

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
    fakes: RunningFakes,
) -> None:
    before = _linear(fakes, _LINEAR_DATES_QUERY).json()["data"]
    added = _add(
        fakes,
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
    after = _linear(fakes, _LINEAR_DATES_QUERY).json()["data"]

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
    fakes: RunningFakes,
) -> None:
    """A team keeps its stated dates though placing an issue on it advances its issue sequence, and a
    stated last update earlier than the creation is served as the creation."""
    added = _add(
        fakes,
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
    data = _linear(fakes, _LINEAR_DATES_QUERY).json()["data"]

    assert added.status_code == 200, added.text
    assert _by(data["teams"]["nodes"], "key")["OPS"] == (
        _shifted_iso("2025-11-03T09:00:00.000Z"),
        _shifted_iso("2026-02-14T09:00:00.000Z"),
    )
    assert _by(data["issueLabels"]["nodes"], "name")["billing"] == (_shifted_iso("2025-12-01T10:00:00.000Z"),) * 2


def test_a_project_serves_its_lead_and_dates_from_the_seed_or_its_issues(
    fakes: RunningFakes,
) -> None:
    added = _add(
        fakes,
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
        fakes,
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
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_every_slack_date_is_the_seeds_or_derived_from_its_messages(
    fakes: RunningFakes,
) -> None:
    added = _add(
        fakes,
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
        for channel in _slack(fakes, "conversations.list", types="public_channel,private_channel")[
            "channels"
        ]
    }
    users = {member["name"]: member for member in _slack(fakes, "users.list")["members"]}

    assert added.status_code == 200, added.text
    # A channel is created at its first message unless its file says otherwise, and one with no
    # message at the earliest message of all; its topic and purpose were set when it was created.
    planning_created = int(datetime.fromisoformat("2026-02-02T09:30:00+00:00").timestamp())
    expected_created = {
        "announcements": _shifted_seconds("1769123456.000000"),
        "eng-oncall": _shifted_seconds("1773900000.000000"),
        "planning": planning_created + TEST_SHIFT_SECONDS,
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
        "sam": sam_created + TEST_SHIFT_SECONDS,
    }
    info = _slack(fakes, "users.info", user=users["claire"]["id"])
    assert info["user"]["updated"] == _shifted_seconds("1769123456.000000")
    _assert_no_date_is_near_the_wall_clock(channels, users)


def test_a_shift_to_the_trials_start_is_a_whole_number_of_days(
    fakes_started_mid_day: RunningFakes,
) -> None:
    """The launcher is told it started part of a day past a whole number of days after the anchor,
    and drops that part, so every timestamp keeps its weekday and its time of day."""
    services = fakes_started_mid_day
    issue = _linear(services, '{ issue(id: "ENG-2") { createdAt } }')
    history = _slack(services, "conversations.history", channel=_channel_id(services, "eng-oncall"))

    assert _control(services, "health")["shift_ms"] == TEST_SHIFT_SECONDS * 1000
    assert issue.json()["data"]["issue"]["createdAt"] == _shifted_iso("2026-03-19T09:00:00.000Z")
    assert history["messages"][0]["ts"] == _shifted("1774000000.000000")


def test_history_takes_oldest_and_latest(fakes: RunningFakes) -> None:
    channel_id = _channel_id(fakes, "eng-oncall")
    boundary = _shifted("1773900000.000000")

    newer = _slack(fakes, "conversations.history", channel=channel_id, oldest=boundary)
    inclusive = _slack(
        fakes, "conversations.history", channel=channel_id, oldest=boundary, inclusive="true"
    )
    older = _slack(
        fakes, "conversations.history", channel=channel_id, latest=_shifted("1774000000.000000")
    )

    assert [message["ts"] for message in newer["messages"]] == [_shifted("1774000000.000000")]
    assert len(inclusive["messages"]) == 2
    assert [message["ts"] for message in older["messages"]] == [boundary]
    first_page = _slack(
        fakes, "conversations.history", channel=channel_id, oldest=boundary, inclusive="true", limit="1"
    )
    second_page = _slack(
        fakes,
        "conversations.history",
        channel=channel_id,
        oldest=boundary,
        inclusive="true",
        limit="1",
        cursor=first_page["response_metadata"]["next_cursor"],
    )
    stale = _slack(fakes, "conversations.history", channel=channel_id, oldest=boundary, cursor="1.000000")
    assert [message["ts"] for message in first_page["messages"] + second_page["messages"]] == [
        _shifted("1774000000.000000"),
        boundary,
    ]
    assert (second_page["has_more"], stale) == (False, {"ok": False, "error": "invalid_cursor"})


def test_search_finds_messages_by_text_and_channel(fakes: RunningFakes) -> None:
    found = _slack(fakes, "search.messages", query="latency in:#eng-oncall")

    assert found["messages"]["total"] == 2
    assert {match["channel"]["name"] for match in found["messages"]["matches"]} == {"eng-oncall"}
    assert _slack(fakes, "search.messages", query="latency in:#announcements")["messages"]["total"] == 0


def test_search_answers_datalibs_reply_search_query(fakes: RunningFakes) -> None:
    """The query datalib's reply search sends: `is:thread`, the channel as `in:<#id>`, whole-day bounds
    two days past each end of the stretch it searches, and a page sorted by timestamp. It reads a
    reply's thread off the permalink's `thread_ts`, and tells a reply from a parent by that value
    differing from the match's own `ts`."""
    channel_id = _channel_id(fakes, "eng-oncall")
    root_ts = _shifted("1773900000.000000")
    reply_ts = _shifted("1773900600.000000")
    root_day = datetime.fromtimestamp(int(root_ts.split(".")[0]), tz=UTC).date()
    query = "is:thread in:<#{}> after:{} before:{}".format(
        channel_id, root_day - timedelta(days=2), root_day + timedelta(days=2)
    )
    found = _slack(
        fakes, "search.messages", query=query, sort="timestamp", sort_dir="desc", count="100", page="1"
    )

    reply, root = found["messages"]["matches"]
    assert (reply["ts"], root["ts"]) == (reply_ts, root_ts)
    assert reply["permalink"].endswith("/p{}?thread_ts={}".format(reply_ts.replace(".", ""), root_ts))
    assert root["permalink"].endswith("/p{}?thread_ts={}".format(root_ts.replace(".", ""), root_ts))
    # Several in: filters are alternatives, as Slack reads them: datalib names every channel of a
    # workspace in one query, and a thread in any of them is found.
    announcements_id = _channel_id(fakes, "announcements")
    both = _slack(
        fakes,
        "search.messages",
        query="is:thread in:<#{}> in:<#{}> after:{} before:{}".format(
            channel_id, announcements_id, root_day - timedelta(days=2), root_day + timedelta(days=2)
        ),
        sort="timestamp",
        sort_dir="desc",
        count="100",
        page="1",
    )
    assert {match["ts"] for match in both["messages"]["matches"]} >= {reply_ts, root_ts}
    assert {match["channel"]["id"] for match in both["messages"]["matches"]} >= {channel_id}
    in_either = _search(fakes, "in:#eng-oncall in:#announcements")
    in_each = _search(fakes, "in:#eng-oncall") + _search(fakes, "in:#announcements")
    assert sorted(in_either) == sorted(in_each)
    assert in_either == sorted(in_either, key=float, reverse=True)
    # The `<#id|name>` form names the channel as the `#name` form does, and nothing in the query is a
    # gap the fake counts.
    assert _search(fakes, "in:<#{}|eng-oncall>".format(channel_id)) == _search(
        fakes, "in:#eng-oncall"
    )
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_an_update_posted_without_a_step_takes_absolute_timestamps_only(
    fakes: RunningFakes,
) -> None:
    """Nothing is taken to follow a step it does not name: a relative timestamp in an update posted
    without `?after=` is refused, nothing is recorded under step_ends, and the same file posted naming
    a step is taken."""
    update = {
        "users": [],
        "channels": [],
        "messages": [{"channel": "eng-oncall", "user": "morgan.reyes", "ts": "mirror+00:00:00", "text": "Paging."}],
    }
    before = _control(fakes, "health")["counts"]

    refused = _add(fakes, "slack", json.dumps(update))
    taken = _add_after(fakes, "slack", "mirror", update)

    assert refused.status_code == 400
    assert refused.json()["error"] == (
        "mirror+00:00:00 is a relative timestamp, which counts from the end of the step an update follows; "
        "this update follows none (POST ...?after=<step>)"
    )
    assert _control(fakes, "health")["counts"]["slack"]["messages"] == before["slack"]["messages"] + 1
    assert taken.json()["after"] == "mirror"
    assert list(_control(fakes, "health")["step_ends"]) == ["mirror"]
    # An absolute update posted without a step is taken as before, follows no step and records none.
    absolute = _add(
        fakes, "slack", fixture_text("update", "slack")
    )
    assert (absolute.json()["ok"], absolute.json()["after"], absolute.json()["after_last_request_at"]) == (
        True,
        None,
        None,
    )
    assert list(_control(fakes, "health")["step_ends"]) == ["mirror"]


def test_a_relative_timestamp_counts_from_the_last_request_the_fake_served(
    fakes: RunningFakes,
) -> None:
    """An update's own instants written as `<step>+HH:MM:SS` land that long after the last request any fake
    served while that step ran, whatever the shift; a reply's `thread_ts` stays the seed's absolute ts
    and resolves under the shift to the root the seed placed."""
    channel_id = _channel_id(fakes, "eng-oncall")
    before = _control(fakes, "health")
    # Offsets of zero, told apart by the fraction: the instants land on the step's end itself, which is
    # already past, so the test spends no wall-clock time waiting for them to come round.
    update = {
        "users": [],
        "channels": [],
        "messages": [
            {"channel": "eng-oncall", "user": "morgan.reyes", "ts": "mirror+00:00:00", "text": "Paging the on-call."},
            {
                "channel": "eng-oncall",
                "user": "morgan.reyes",
                "ts": "mirror+00:00:00.000001",
                "thread_ts": "1773900000.000000",
                "text": "Rolled back.",
            },
        ],
    }

    added = _add_after(fakes, "slack", "mirror", update)

    assert added.status_code == 200, added.text
    health = _control(fakes, "health")
    assert (before["step_ends"], before["start_at"]) == ({}, _shifted_iso("2026-03-21T00:00:00.000Z"))
    # The step ended with the conversations.list request above, the latest the fake had served; the
    # health requests in between are control calls and count for nothing.
    assert health["step_ends"]["mirror"]["last_request_at"] == before["last_request_at"]
    assert added.json()["after_last_request_at"] == before["last_request_at"]
    end_seconds = int(_instant(health["step_ends"]["mirror"]["last_request_at"]).timestamp())
    root_ts, reply_ts = "{}.000000".format(end_seconds), "{}.000001".format(end_seconds)
    history = _slack(fakes, "conversations.history", channel=channel_id)["messages"]
    assert history[0]["ts"] == root_ts
    replies = _slack(fakes, "conversations.replies", channel=channel_id, ts=_shifted("1773900000.000000"))
    assert [message["ts"] for message in replies["messages"]][-1] == reply_ts
    # The seed's root now names the new reply as its latest_reply, in history and as the thread's
    # parent copy: what a downloader compares to know the thread it holds has grown.
    seed_root = next(message for message in history if message["ts"] == _shifted("1773900000.000000"))
    assert (seed_root["reply_count"], seed_root["latest_reply"]) == (2, reply_ts)
    assert replies["messages"][0]["latest_reply"] == reply_ts
    assert "latest_reply" not in history[0]
    # A second file following the same step counts from the same end, after more requests have been served.
    second = _add_after(
        fakes,
        "slack",
        "mirror",
        {
            "messages": [
                {
                    "channel": "announcements",
                    "user": "claire",
                    "ts": "mirror+00:00:00.000002",
                    "text": "Status page updated.",
                }
            ]
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["after_last_request_at"] == before["last_request_at"]
    announcements = _slack(
        fakes, "conversations.history", channel=_channel_id(fakes, "announcements")
    )
    assert announcements["messages"][0]["ts"] == "{}.000002".format(end_seconds)


def test_an_update_whose_instants_are_still_in_the_future_is_refused_with_a_wait(
    fakes: RunningFakes,
) -> None:
    before = _control(fakes, "health")["counts"]
    update = {
        "messages": [{"channel": "eng-oncall", "user": "morgan.reyes", "ts": "mirror+01:00:00", "text": "Later."}]
    }

    answer = _add_after(fakes, "slack", "mirror", update)

    assert answer.status_code == 409, answer.text
    body = answer.json()
    assert body["ok"] is False and 3590 <= body["retry_after_seconds"] <= 3600
    health = _control(fakes, "health")
    assert (health["step_ends"], health["counts"]) == ({}, before)


def test_a_relative_timestamp_may_count_from_the_end_of_an_earlier_step(
    fakes: RunningFakes,
) -> None:
    """A later update names an earlier step to refer to a record the update after that step added;
    naming a step no update has followed is refused."""
    first = _add_after(
        fakes,
        "slack",
        "mirror",
        {
            "messages": [
                {"channel": "eng-oncall", "user": "morgan.reyes", "ts": "mirror+00:00:00", "text": "Opening a thread."}
            ]
        },
    )
    # One request served between the two updates, so the triage step ends no earlier than mirror did.
    _channel_id(fakes, "eng-oncall")
    second = _add_after(
        fakes,
        "slack",
        "triage",
        {
            "messages": [
                {
                    "channel": "eng-oncall",
                    "user": "claire",
                    "ts": "triage+00:00:00.000001",
                    "thread_ts": "mirror+00:00:00",
                    "text": "Replying in it.",
                }
            ]
        },
    )
    unknown = _add_after(
        fakes,
        "slack",
        "followup",
        {
            "messages": [
                {
                    "channel": "eng-oncall",
                    "user": "claire",
                    "ts": "followup+00:00:00",
                    "thread_ts": "nosuch+00:00:00",
                    "text": "?",
                }
            ]
        },
    )

    assert (first.status_code, second.status_code) == (200, 200), second.text
    ends = _control(fakes, "health")["step_ends"]
    assert _instant(ends["triage"]["last_request_at"]) >= _instant(ends["mirror"]["last_request_at"])
    root_ts = "{}.000000".format(int(_instant(ends["mirror"]["last_request_at"]).timestamp()))
    replies = _slack(
        fakes, "conversations.replies", channel=_channel_id(fakes, "eng-oncall"), ts=root_ts
    )
    assert [message["text"] for message in replies["messages"]] == ["Opening a thread.", "Replying in it."]
    assert unknown.status_code == 400 and "nosuch" in unknown.json()["error"]


def test_client_counts_names_each_channels_newest_message_and_its_last_change(
    fakes: RunningFakes,
) -> None:
    """The unpublished sidebar method a downloader skips quiet channels by: `latest` is the channel's
    newest top-level message, `updated` moves with a reply or an edit as well, and the fake serves it
    rather than counting it as a gap."""
    channel_id = _channel_id(fakes, "eng-oncall")
    before = _slack_post(fakes, "client.counts")
    row = next(entry for entry in before["channels"] if entry["id"] == channel_id)
    history = _slack(fakes, "conversations.history", channel=channel_id)["messages"]
    newest_root = max((m["ts"] for m in history), key=float)
    assert (row["latest"], row["last_read"]) == (newest_root, newest_root)
    assert float(row["updated"]) >= float(newest_root)
    # One row per conversation the token's user is in, as the sidebar shows them.
    mine = _slack(fakes, "users.conversations", types="public_channel,private_channel")["channels"]
    assert {entry["id"] for entry in before["channels"]} == {channel["id"] for channel in mine} >= {channel_id}
    assert before["threads"] == {"has_unreads": False, "mention_count": 0}

    # An edit of a seed root moves updated and leaves latest; a new root moves both.
    edited = _add_after(
        fakes,
        "slack",
        "mirror",
        {
            "messages": [
                {
                    "channel": "eng-oncall",
                    "user": "claire",
                    "ts": "1773900000.000000",
                    "edited_ts": "mirror+00:00:00",
                    "text": "ALERT resolved",
                }
            ]
        },
    )
    assert edited.status_code == 200, edited.text
    after_edit = next(e for e in _slack_post(fakes, "client.counts")["channels"] if e["id"] == channel_id)
    assert after_edit["latest"] == newest_root and float(after_edit["updated"]) > float(row["updated"])
    added = _add_after(
        fakes,
        "slack",
        "mirror",
        {"messages": [{"channel": "eng-oncall", "user": "claire", "ts": "mirror+00:00:00.000001", "text": "New"}]},
    )
    assert added.status_code == 200, added.text
    after_root = next(e for e in _slack_post(fakes, "client.counts")["channels"] if e["id"] == channel_id)
    assert float(after_root["latest"]) > float(newest_root) and after_root["updated"] == after_root["latest"]
    assert "slack:client.counts" not in _control(fakes, "health")["unsupported_calls"]


def test_an_update_edits_a_message_it_names_with_edited_ts_and_refuses_a_bare_repeat(
    fakes: RunningFakes,
) -> None:
    channel_id = _channel_id(fakes, "eng-oncall")
    root = "1773900000.000000"
    edit = {
        "messages": [
            {
                "channel": "eng-oncall",
                "user": "claire",
                "ts": root,
                "edited_ts": "mirror+00:00:00",
                "text": "ALERT resolved: gateway p99 back under threshold",
            }
        ]
    }

    added = _add_after(fakes, "slack", "mirror", edit)
    repeat = _add_after(
        fakes,
        "slack",
        "mirror",
        {"messages": [{"channel": "eng-oncall", "user": "claire", "ts": root, "text": "again"}]},
    )
    unknown = _add_after(
        fakes,
        "slack",
        "mirror",
        {
            "messages": [
                {
                    "channel": "eng-oncall",
                    "user": "claire",
                    "ts": "1700000000.000000",
                    "edited_ts": "mirror+00:00:00",
                    "text": "?",
                }
            ]
        },
    )

    assert added.status_code == 200, added.text
    assert added.json()["added"] == {"slack_messages": 0, "slack_edits": 1}
    ended = _control(fakes, "health")["step_ends"]["mirror"]["last_request_at"]
    history = _slack(fakes, "conversations.history", channel=channel_id)["messages"]
    message = next(m for m in history if m["ts"] == _shifted(root))
    assert message["text"] == "ALERT resolved: gateway p99 back under threshold"
    assert message["edited"] == {
        "user": _user_ids(fakes)["claire"],
        "ts": "{}.000000".format(int(_instant(ended).timestamp())),
    }
    # The edit changed the text and nothing of the thread.
    assert (message["reply_count"], message["thread_ts"], message["latest_reply"]) == (
        1,
        _shifted(root),
        _shifted("1773900600.000000"),
    )
    assert repeat.status_code == 400 and "edited_ts" in repeat.json()["error"]
    assert unknown.status_code == 400 and "no such message" in unknown.json()["error"]
    unchanged = _slack(fakes, "conversations.history", channel=channel_id)["messages"]
    assert next(m for m in unchanged if m["ts"] == _shifted(root))["text"] == message["text"]


def test_an_update_edits_an_issue_it_names_with_a_later_updated_at(fakes: RunningFakes) -> None:
    edit = {
        "issues": [
            {
                "identifier": "ENG-2",
                "team": "ENG",
                "state": "Done",
                "title": "Fix route normalization",
                "updated_at": "mirror+00:00:00",
            }
        ]
    }

    added = _add_after(fakes, "linear", "mirror", edit)
    repeat = _add_after(
        fakes, "linear", "mirror", {"issues": [{"identifier": "ENG-2", "team": "ENG", "title": "again"}]}
    )
    # An absolute updated_at is how a seed writes an issue's own instant, so on a held identifier it
    # is a copied entry and not an edit, as the harness's checks read it.
    absolute = _add_after(
        fakes,
        "linear",
        "mirror",
        {
            "issues": [
                {"identifier": "ENG-2", "team": "ENG", "title": "again", "updated_at": "2026-03-21T00:00:00.000Z"}
            ]
        },
    )

    assert added.status_code == 200, added.text
    assert added.json()["added"] == {"linear_issues": 0, "linear_comments": 0, "linear_edits": 1}
    ended = _control(fakes, "health")["step_ends"]["mirror"]["last_request_at"]
    issue = _linear(fakes, '{ issue(id: "ENG-2") { title updatedAt completedAt state { name } } }')
    assert issue.json()["data"]["issue"] == {
        "title": "Fix route normalization",
        "updatedAt": ended,
        "completedAt": ended,
        "state": {"name": "Done"},
    }
    assert repeat.status_code == 400 and "updated_at" in repeat.json()["error"]
    assert absolute.status_code == 400 and absolute.json()["error"] == repeat.json()["error"]
    assert _issue_titles(fakes)["ENG-2"] == "Fix route normalization"


def _dms(services: RunningFakes) -> dict[str, dict[str, Any]]:
    """The token's dms by kind: its im and its mpim."""
    listed = _slack(services, "conversations.list", types="im,mpim")["channels"]
    return {"im" if channel["is_im"] else "mpim": channel for channel in listed}


def _slack_post(services: RunningFakes, method: str, **fields: str) -> dict[str, Any]:
    return httpx.post("{}/api/{}".format(services.slack_url, method), data=fields, headers=_SLACK_HEADERS).json()


def _search(services: RunningFakes, query: str, **params: str) -> list[str]:
    return [match["ts"] for match in _slack(services, "search.messages", query=query, **params)["messages"]["matches"]]


def _day_after(ts: str) -> str:
    return (datetime.fromtimestamp(float(ts), tz=UTC).date() + timedelta(days=1)).isoformat()


def test_a_dm_is_listed_by_its_type_and_read_like_a_channel_by_its_members(
    fakes: RunningFakes,
) -> None:
    user_ids = _user_ids(fakes)
    dms = _dms(fakes)
    im, mpim = dms["im"], dms["mpim"]

    # A dm is listed under its own type alone, under Slack's id prefixes: D for an im, and the G
    # mpims have long carried.
    listed = _slack(fakes, "conversations.list", types="public_channel,private_channel")["channels"]
    assert {channel["name"] for channel in listed} == {"announcements", "eng-oncall"}
    assert (im["id"][0], mpim["id"][0], len(im["id"]), len(mpim["id"])) == ("D", "G", 11, 11)
    assert (im["is_im"], im["is_mpim"], im["is_private"], im["user"]) == (True, False, True, user_ids["claire"])
    assert (mpim["is_im"], mpim["is_mpim"], mpim["name"]) == (False, True, "mpdm-claire--morgan.reyes--pagerbot-1")
    # An im was created at its first message, an mpim at the date its file states.
    mpim_created = int(datetime.fromisoformat("2026-03-01T09:00:00+00:00").timestamp())
    assert (im["created"], mpim["created"]) == (
        _shifted_seconds("1773950000.000000"),
        mpim_created + TEST_SHIFT_SECONDS,
    )
    info = _slack(fakes, "conversations.info", channel=im["id"])["channel"]
    assert (info["id"], info["is_im"], info["user"]) == (im["id"], True, user_ids["claire"])
    members = _slack(fakes, "conversations.members", channel=mpim["id"])["members"]
    assert sorted(members) == sorted(user_ids[name] for name in ("claire", "morgan.reyes", "pagerbot"))
    # The im's reply is folded into its thread, and history takes oldest/latest/inclusive as for a channel.
    parent = _shifted("1773950000.000000")
    history = _slack(fakes, "conversations.history", channel=im["id"])["messages"]
    assert [(message["ts"], message["reply_count"]) for message in history] == [(parent, 1)]
    after_parent = _slack(fakes, "conversations.history", channel=im["id"], oldest=parent)
    from_parent = _slack(fakes, "conversations.history", channel=im["id"], oldest=parent, inclusive="1")
    assert (after_parent["messages"], [message["ts"] for message in from_parent["messages"]]) == ([], [parent])
    replies = _slack(fakes, "conversations.replies", channel=im["id"], ts=parent)["messages"]
    assert [message["ts"] for message in replies] == [parent, _shifted("1774000300.000000")]
    # Search reaches a dm through in:@user and names it as Slack does.
    found = _slack(fakes, "search.messages", query="latency in:@claire")["messages"]["matches"]
    assert [
        (match["ts"], match["channel"]["id"], match["channel"]["is_im"], match["channel"]["name"]) for match in found
    ] == [(parent, im["id"], True, user_ids["claire"])]
    assert _search(fakes, "thursday") == [_shifted("1773980000.000000")]


def test_users_conversations_lists_the_conversations_the_tokens_user_is_in(
    fakes: RunningFakes,
) -> None:
    user_ids = _user_ids(fakes)
    dms = _dms(fakes)

    default = _slack(fakes, "users.conversations")
    with_im = _slack(fakes, "users.conversations", types="public_channel,im")
    dms_only = _slack(fakes, "users.conversations", types="im,mpim")
    first = _slack(fakes, "users.conversations", types="public_channel,im,mpim", limit="2")
    rest = _slack(
        fakes,
        "users.conversations",
        types="public_channel,im,mpim",
        limit="2",
        cursor=first["response_metadata"]["next_cursor"],
    )
    pagerbots = _slack(
        fakes, "users.conversations", user=user_ids["pagerbot"], types="public_channel,mpim"
    )

    # Morgan posted in eng-oncall and is no member of announcements, which conversations.list lists too.
    assert {channel["name"] for channel in _slack(fakes, "conversations.list")["channels"]} == {
        "announcements",
        "eng-oncall",
    }
    assert [channel["name"] for channel in default["channels"]] == ["eng-oncall"]
    assert default["response_metadata"] == {"next_cursor": ""}
    assert "is_member" not in default["channels"][0] and "num_members" not in default["channels"][0]
    assert {channel["id"] for channel in with_im["channels"]} == {
        _channel_id(fakes, "eng-oncall"),
        dms["im"]["id"],
    }
    assert {channel["id"] for channel in dms_only["channels"]} == {dms["im"]["id"], dms["mpim"]["id"]}
    assert len(first["channels"]) == 2 and first["response_metadata"]["next_cursor"]
    assert {channel["id"] for channel in first["channels"] + rest["channels"]} == {
        _channel_id(fakes, "eng-oncall"),
        dms["im"]["id"],
        dms["mpim"]["id"],
    }
    assert rest["response_metadata"] == {"next_cursor": ""}
    assert {channel["id"] for channel in pagerbots["channels"]} == {
        _channel_id(fakes, "eng-oncall"),
        dms["mpim"]["id"],
    }
    assert _slack(fakes, "users.conversations", cursor="CNOSUCH") == {
        "ok": False,
        "error": "invalid_cursor",
    }
    assert _slack(fakes, "users.conversations", user="UNOSUCH") == {
        "ok": False,
        "error": "user_not_found",
    }
    assert [
        channel["name"] for channel in _slack(fakes, "users.conversations", limit="-1")["channels"]
    ] == ["eng-oncall"]
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_search_all_answers_search_messages_messages_and_no_files(fakes: RunningFakes) -> None:
    for params in ({"query": "latency"}, {"query": "latency", "sort": "timestamp", "count": "2", "page": "2"}):
        messages = _slack(fakes, "search.messages", **params)
        everything = _slack(fakes, "search.all", **params)

        count, page = int(params.get("count", "20")), int(params.get("page", "1"))
        assert everything == {
            **messages,
            "files": {
                "total": 0,
                "paging": {"count": count, "total": 0, "page": page, "pages": 0},
                "pagination": {
                    "total_count": 0,
                    "page": page,
                    "per_page": count,
                    "page_count": 0,
                    "first": 0,
                    "last": 0,
                },
                "matches": [],
            },
            "posts": {"total": 0, "matches": []},
        }
    assert _control(fakes, "health")["unsupported_calls"] == {}
    # A gap of its query is counted under search.all.
    _slack(fakes, "search.all", query="latency has:link")
    assert _control(fakes, "health")["unsupported_calls"] == {"slack:search.all:has": 1}


def test_search_finds_a_posted_message_and_reads_a_date_as_written(fakes: RunningFakes) -> None:
    """A message posted in the trial is stamped with the box's clock, and a seed's under a time shift
    with dates ending the day before the trial: both are found by the dates an agent reads off them,
    with no shift applied to the query."""
    channel_id = _channel_id(fakes, "eng-oncall")
    posted = _slack_post(fakes, "chat.postMessage", channel=channel_id, text="latency latency postmortem")

    assert posted["ok"], posted
    assert _search(fakes, "postmortem") == [posted["ts"]]
    assert _search(fakes, "postmortem after:{}".format(_day_after(posted["ts"]))) == []
    posted_day = datetime.fromtimestamp(float(posted["ts"]), tz=UTC).date()
    assert _search(fakes, "postmortem after:{}".format(posted_day - timedelta(days=1))) == [posted["ts"]]
    assert _search(fakes, "postmortem on:{}".format(posted_day)) == [posted["ts"]]
    on_the_19th = [_shifted(ts) for ts in ("1773950000.000000", "1773900600.000000", "1773900000.000000")]
    assert _search(fakes, "on:{}".format(_shifted_date("2026-03-19")), sort="timestamp") == on_the_19th
    assert _search(fakes, "during:{}".format(_shifted_date("2026-03-19"))) == _search(
        fakes, "on:{}".format(_shifted_date("2026-03-19"))
    )
    after_the_19th = [_shifted(ts) for ts in ("1774000300.000000", "1774000000.000000", "1773980000.000000")]
    assert (
        _search(fakes, "after:{}".format(_shifted_date("2026-03-19")), sort="timestamp") == after_the_19th
    )
    # yesterday is the day before the trial's present, which the shifted seed's data ends on.
    assert _search(fakes, "on:yesterday", sort="timestamp") == after_the_19th
    before_the_19th = _search(fakes, "before:{}".format(_shifted_date("2026-03-19")), sort="timestamp")
    assert before_the_19th == [_shifted("1769123456.000000"), posted["ts"]]
    month = _shifted_date("2026-03-19")[:7]
    assert len(_search(fakes, "during:{}".format(month))) == 6
    # A date the fake cannot read matches nothing, and is counted as a gap.
    assert _search(fakes, "latency during:march") == []
    assert _control(fakes, "health")["unsupported_calls"] == {"slack:search.messages:during": 1}


def test_search_sorts_by_score_or_timestamp_and_pages_as_slack_does(
    fakes: RunningFakes,
) -> None:
    posted = _slack_post(
        fakes,
        "chat.postMessage",
        channel=_channel_id(fakes, "eng-oncall"),
        text="latency latency postmortem",
    )
    seeded = [_shifted(ts) for ts in ("1774000000.000000", "1773950000.000000", "1773900000.000000")]

    # The posted message says latency twice and is the oldest of them, as the box's clock is ten
    # years behind the shifted seed's.
    assert _search(fakes, "latency") == [posted["ts"], *seeded]
    assert _search(fakes, "latency", sort="timestamp") == [*seeded, posted["ts"]]
    assert _search(fakes, "latency", sort="timestamp", sort_dir="asc") == [posted["ts"], *reversed(seeded)]
    second = _slack(fakes, "search.messages", query="latency", sort="timestamp", count="3", page="2")
    assert [match["ts"] for match in second["messages"]["matches"]] == [posted["ts"]]
    assert second["messages"]["paging"] == {"count": 3, "total": 4, "page": 2, "pages": 2}
    assert second["messages"]["pagination"] == {
        "total_count": 4,
        "page": 2,
        "per_page": 3,
        "page_count": 2,
        "first": 4,
        "last": 4,
    }
    # is:thread is a thread's parent and its replies, as Slack reads it; any other is: is a gap.
    parent, reply = _shifted("1773900000.000000"), _shifted("1773900600.000000")
    in_threads = _search(fakes, "is:thread in:#eng-oncall")
    assert {parent, reply} <= set(in_threads) and _shifted("1774000000.000000") not in in_threads
    assert _search(fakes, "latency is:starred") == []
    # An operator the fake does not read matches nothing and is counted.
    assert _search(fakes, "latency has:link") == []
    assert _search(fakes, "latency -postmortem") == []
    assert _search(fakes, "laten*") == []
    assert _control(fakes, "health")["unsupported_calls"] == {
        "slack:search.messages:is": 1,
        "slack:search.messages:has": 1,
        "slack:search.messages:exclude": 1,
        "slack:search.messages:wildcard": 1,
    }


def test_a_permalink_names_the_workspace_and_a_replys_thread(fakes: RunningFakes) -> None:
    channel_id = _channel_id(fakes, "eng-oncall")
    parent, reply = _shifted("1773900000.000000"), _shifted("1773900600.000000")
    im_id = _dms(fakes)["im"]["id"]

    top = _slack(fakes, "chat.getPermalink", channel=channel_id, message_ts=parent)
    threaded = _slack(fakes, "chat.getPermalink", channel=channel_id, message_ts=reply)
    in_im = _slack(fakes, "chat.getPermalink", channel=im_id, message_ts=_shifted("1773950000.000000"))

    base = "https://redwood.slack.com/archives/{}/p".format(channel_id)
    # A thread's parent names the thread too, as Slack's permalinks do; a message in no thread does not.
    assert top == {
        "ok": True,
        "channel": channel_id,
        "permalink": "{}{}?thread_ts={}&cid={}".format(base, parent.replace(".", ""), parent, channel_id),
    }
    assert threaded["permalink"] == "{}{}?thread_ts={}&cid={}".format(base, reply.replace(".", ""), parent, channel_id)
    im_parent = _shifted("1773950000.000000")
    assert in_im["permalink"] == "https://redwood.slack.com/archives/{}/p{}?thread_ts={}&cid={}".format(
        im_id, im_parent.replace(".", ""), im_parent, im_id
    )
    # A search match's permalink names the thread but not the channel, as Slack's do.
    found = _slack(fakes, "search.messages", query="normalization")["messages"]["matches"]
    assert [match["permalink"] for match in found] == [
        "{}{}?thread_ts={}".format(base, reply.replace(".", ""), parent)
    ]
    missing = _slack(fakes, "chat.getPermalink", channel=channel_id, message_ts="1773900000.000000")
    assert missing == {"ok": False, "error": "message_not_found"}


def test_an_update_replies_into_a_seed_thread_named_by_the_seeds_ts(
    fakes: RunningFakes,
) -> None:
    """A later step names a seed message's ts as the seed writes it, and the launcher shifts both alike."""
    channel_id = _channel_id(fakes, "eng-oncall")
    parent = _shifted("1773900000.000000")

    added = _add(fakes, "slack", fixture_text("update", "slack"))
    posted = _slack_post(fakes, "chat.postMessage", channel=channel_id, thread_ts=parent, text="Ack.")

    assert added.status_code == 200, added.text
    olivia = _user_ids(fakes)["olivia_chen"]
    replies = _slack(fakes, "conversations.replies", channel=channel_id, ts=parent)["messages"]
    # The parent first, then its replies oldest first: the one posted, which the box's clock stamps
    # ten years before the shifted seed, the seed's and the update's.
    assert [message["ts"] for message in replies] == [
        parent,
        posted["ts"],
        _shifted("1773900600.000000"),
        _shifted("1774050600.000000"),
    ]
    assert (replies[0]["reply_count"], replies[0]["thread_ts"]) == (3, parent)
    # The posted reply is the thread's oldest, so the update's stays its latest.
    stored = _control(fakes, "state")["slack"]["collections"]["slack.messages"]["items"]
    assert [m["latest_reply"] for m in stored if m["channel_id"] == channel_id and m["ts"] == parent] == [
        _shifted("1774050600.000000")
    ]
    assert olivia in replies[0]["reply_users"]
    history = _slack(fakes, "conversations.history", channel=channel_id)["messages"]
    assert _shifted("1774050600.000000") not in [message["ts"] for message in history]
    # A reply posted in the trial makes a message with none a thread's parent.
    lone = _shifted("1774000000.000000")
    _slack_post(fakes, "chat.postMessage", channel=channel_id, thread_ts=lone, text="Tracked in ENG-2.")
    lone_parent = _slack(fakes, "conversations.replies", channel=channel_id, ts=lone)["messages"][0]
    assert (lone_parent["ts"], lone_parent["thread_ts"], lone_parent["reply_count"]) == (lone, lone, 1)


def test_linear_answers_a_nested_filter_ordered_by_update(fakes: RunningFakes) -> None:
    answer = _linear(
        fakes,
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


def test_a_date_filter_takes_a_duration_from_the_trials_present(fakes: RunningFakes) -> None:
    """ENG-1 was created 18 days and 15 hours before the anchor and ENG-2 a day and 15 hours before,
    and the anchor is shifted to the trial's start."""

    def created_within(duration: str) -> list[str]:
        answer = _linear(
            fakes,
            '{ issues(filter: {createdAt: {gt: "%s"}}) { nodes { identifier } } }' % duration,
        )
        return sorted(issue["identifier"] for issue in answer.json()["data"]["issues"]["nodes"])

    assert created_within("-P1W") == ["ENG-2"]
    assert created_within("-P2W4DT12H") == ["ENG-2"]
    assert created_within("-P2W4DT16H") == ["ENG-1", "ENG-2"]
    assert created_within("-P3W") == ["ENG-1", "ENG-2"]
    assert created_within("-P1M") == ["ENG-1", "ENG-2"]
    assert created_within("P1D") == []
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_the_active_cycle_is_the_one_spanning_the_trials_present(fakes: RunningFakes) -> None:
    """The launcher is told the trial started 200 days after the anchor, so the cycle spanning the
    anchor is active, whatever day the wall clock reads."""
    added = _add(
        fakes,
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
        fakes,
        '{ team(id: "ENG") { activeCycle { name } } cycles(filter: {isActive: {eq: true}}) { nodes { name } } }',
    )

    assert added.status_code == 200, added.text
    assert answer.json()["data"] == {
        "team": {"activeCycle": {"name": "2026-W12"}},
        "cycles": {"nodes": [{"name": "2026-W12"}]},
    }


def test_only_a_seeded_token_authenticates(fakes: RunningFakes) -> None:
    slack_answer = httpx.get(
        "{}/api/conversations.list".format(fakes.slack_url), headers={"Authorization": "Bearer xoxb-other"}
    )
    linear_answer = _linear(fakes, "{ viewer { name } }", token="lin_api_other")
    anonymous = httpx.get("{}/api/conversations.list".format(fakes.slack_url))

    assert slack_answer.json()["ok"] is False
    assert anonymous.json() == {"ok": False, "error": "not_authed"}
    assert linear_answer.status_code == 401


def test_an_update_lands_on_its_own_service_with_the_seeds_shift(fakes: RunningFakes) -> None:
    slack_added = _add(
        fakes, "slack", fixture_text("update", "slack")
    )
    after_slack = _control(fakes, "health")["counts"]
    linear_added = _add(
        fakes, "linear", fixture_text("update", "linear")
    )

    assert (slack_added.status_code, slack_added.json()["added"]) == (200, {"slack_messages": 2, "slack_edits": 0})
    # Each service's update is its own request: Slack's left Linear holding what it was seeded with.
    assert (after_slack["slack"]["messages"], after_slack["linear"]["issues"]) == (9, 2)
    assert (linear_added.status_code, linear_added.json()["added"]) == (
        200,
        {"linear_issues": 1, "linear_comments": 0, "linear_edits": 0},
    )
    history = _slack(fakes, "conversations.history", channel=_channel_id(fakes, "eng-oncall"))
    assert history["messages"][0]["ts"] == _shifted("1774050000.000000")
    issue = _linear(fakes, '{ issue(id: "ENG-3") { title createdAt labels { nodes { name } } } }')
    assert issue.json()["data"]["issue"] == {
        "title": "Invoice line alignment",
        "createdAt": _shifted_iso("2026-03-20T18:00:00.000Z"),
        "labels": {"nodes": [{"name": "billing"}]},
    }


def test_a_refused_update_leaves_its_store_as_it_was(fakes: RunningFakes) -> None:
    before = _control(fakes, "health")["counts"]

    refused = _add(
        fakes,
        "linear",
        fixture_text("refused_update", "linear"),
    )

    assert refused.status_code == 400
    assert "linear state not found: No Such State" in refused.json()["error"]
    # The update's first issue was placeable and went in before its second was refused, and is gone again.
    assert _control(fakes, "health")["counts"] == before
    assert sorted(_issue_titles(fakes)) == ["ENG-1", "ENG-2"]
    # The store still takes an update after one was rolled back.
    retried = _add(
        fakes, "linear", fixture_text("update", "linear")
    )
    assert retried.status_code == 200
    assert _control(fakes, "health")["counts"]["linear"]["issues"] == before["linear"]["issues"] + 1


def test_an_update_goes_to_a_served_service_by_name_and_nowhere_else(
    fakes: RunningFakes,
) -> None:
    update_text = fixture_text("update", "slack")
    before = _control(fakes, "health")["counts"]

    unnamed = httpx.post("{}/_control/records".format(fakes.control_url), content=update_text)
    unknown = _add(fakes, "github", update_text)

    assert (unnamed.status_code, unnamed.json()) == (404, {"error": "unknown control route"})
    assert (unknown.status_code, unknown.json()["error"]) == (
        400,
        "the github service was not seeded, so it takes no update",
    )
    assert _control(fakes, "health")["counts"] == before


def test_the_control_endpoints_are_on_the_control_port_only(fakes: RunningFakes) -> None:
    """A workspace's requests are pointed at the service ports, so nothing there may dump the stores,
    read the log, or add records."""
    before = _control(fakes, "health")["counts"]
    update_text = fixture_text("update", "slack")

    answers = [
        httpx.get("{}/_control/{}".format(service_url, route), headers=headers)
        for service_url, headers in (
            (fakes.slack_url, _SLACK_HEADERS),
            (fakes.linear_url, {"Authorization": _LINEAR_TOKEN}),
        )
        for route in ("health", "state", "log")
    ]
    answers.append(
        httpx.post(
            "{}/_control/records/slack".format(fakes.slack_url),
            content=update_text,
            headers=_SLACK_HEADERS,
        )
    )

    for answer in answers:
        assert answer.status_code == 404, (answer.request.url, answer.text[:200])
        assert not {"counts", "shift_ms", "added", "dumped_at"} & set(answer.json())
    after = _control(fakes, "health")["counts"]
    assert (after["slack"], after["linear"]) == (before["slack"], before["linear"])
    # Unlike a request to the control port, each of these reached a fake and is in its log.
    assert after["requests"] == {"slack": 4, "linear": 3, "google": 0}


def test_a_directory_holding_one_seed_serves_that_service_alone(
    slack_only_fakes: RunningFakes,
) -> None:
    health = _control(slack_only_fakes, "health")
    dump = _control(slack_only_fakes, "state")
    refused = _add(
        slack_only_fakes,
        "linear",
        fixture_text("update", "linear"),
    )

    assert sorted(slack_only_fakes.service_urls) == ["slack"]
    assert health["services"] == ["slack"]
    assert sorted(health["counts"]) == ["requests", "slack"]
    assert health["counts"]["requests"] == {"slack": 0}
    assert "linear" not in dump
    assert (refused.status_code, refused.json()["error"]) == (
        400,
        "the linear service was not seeded, so it takes no update",
    )
    assert _slack(slack_only_fakes, "auth.test")["user"] == "morgan.reyes"


def test_a_seed_that_leaves_the_time_shift_out_is_served_as_written(
    slack_only_fakes: RunningFakes,
) -> None:
    added = _add(
        slack_only_fakes,
        "slack",
        fixture_text("update", "slack"),
    )
    history = _slack(
        slack_only_fakes,
        "conversations.history",
        channel=_channel_id(slack_only_fakes, "eng-oncall"),
    )

    assert added.status_code == 200
    assert _control(slack_only_fakes, "health")["shift_ms"] == 0
    assert _control(slack_only_fakes, "state")["shift_ms"] == 0
    # The update's message is the newest, and it too keeps the timestamp its file states.
    assert [message["ts"] for message in history["messages"]] == [
        "1774050000.000000",
        "1774000000.000000",
        "1773900000.000000",
    ]


def test_a_request_for_a_services_other_host_is_not_found_and_counted(
    fakes: RunningFakes,
) -> None:
    """The curl shim points every host of a service at its fake and names the host the request was
    for; only the API host is served."""
    slack_file = httpx.get(
        "{}/files-pri/T1-F1/report.pdf".format(fakes.slack_url),
        headers={**_SLACK_HEADERS, "X-Forwarded-Host": "files.slack.com"},
    )
    # The path is one the API host serves, so only the named host can be what refuses it.
    sibling_api = httpx.get(
        "{}/api/auth.test".format(fakes.slack_url),
        headers={**_SLACK_HEADERS, "X-Forwarded-Host": "redwood.slack.com"},
    )
    linear_upload = httpx.post(
        "{}/graphql".format(fakes.linear_url),
        json={"query": "{ viewer { name } }"},
        headers={"Authorization": _LINEAR_TOKEN, "X-Forwarded-Host": "uploads.linear.app"},
    )
    primary = httpx.get(
        "{}/api/auth.test".format(fakes.slack_url),
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
    assert _control(fakes, "health")["unsupported_calls"] == expected_counts
    assert _control(fakes, "state")["unsupported_calls"] == expected_counts
    assert [(line["url"], line["unsupported"]) for line in _control(fakes, "log")] == [
        ("https://files.slack.com/files-pri/T1-F1/report.pdf", "slack:host:files.slack.com"),
        ("https://redwood.slack.com/api/auth.test", "slack:host:redwood.slack.com"),
        ("https://uploads.linear.app/graphql", "linear:host:uploads.linear.app"),
        ("https://slack.com/api/auth.test", None),
    ]


def test_a_public_linear_field_or_argument_the_fake_lacks_is_answered_empty_and_counted(
    fakes: RunningFakes,
) -> None:
    """Linear's public schema validates every query, so a field or argument Linear has and the fake
    does not is answered as its type's empty value and counted, while one Linear itself does not have
    is refused as Linear refuses it, uncounted: that is the caller's mistake, not the fake's gap."""
    nullable = _linear(fakes, "{ viewer { name timezone } }")
    _linear(fakes, "{ viewer { timezone } }")
    non_null = _linear(fakes, "{ viewer { guest organization { name } } projects { nodes { slugId } } }")
    root = _linear(fakes, "{ initiatives { nodes { name } pageInfo { hasNextPage } } }")
    argument = _linear(fakes, "{ issues(sort: [{priority: {order: Descending}}]) { nodes { id } } }")
    filtered = _linear(fakes, "{ issues(filter: {slaStatus: {eq: Breached}}) { nodes { id } } }")
    _linear(fakes, "{ viewer { name } }")
    no_such_field = _linear(fakes, "{ viewer { noSuchField } }")
    no_such_argument = _linear(fakes, "{ issues(noSuchArgument: 1) { nodes { id } } }")
    _linear(fakes, "{ viewer { name }")

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
    assert _control(fakes, "health")["unsupported_calls"] == {
        "linear:field:User.timezone": 2,
        "linear:field:User.guest": 1,
        "linear:field:User.organization": 1,
        "linear:field:Project.slugId": 1,
        "linear:field:Query.initiatives": 1,
        "linear:argument:Query.issues.sort": 1,
        "linear:filter:IssueFilter.slaStatus": 1,
    }
    assert [line["unsupported"] for line in _control(fakes, "log")] == [
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
    fakes: RunningFakes,
) -> None:
    """`includeDisabled` is read by no resolver; `includeArchived` is read where issues are listed
    and nowhere else; `includeSubTeams: false` is what the schema gives a caller who says nothing."""
    answer = _linear(
        fakes,
        '{ team(id: "ENG") { members(includeDisabled: true) { nodes { name } } '
        "issues(includeArchived: true, includeSubTeams: false) { nodes { identifier } } "
        "projects(includeArchived: true) { nodes { name } } } }",
    )

    assert answer.status_code == 200 and "errors" not in answer.json()
    assert len(answer.json()["data"]["team"]["issues"]["nodes"]) == 2
    assert _control(fakes, "health")["unsupported_calls"] == {
        "linear:argument:Team.members.includeDisabled": 1,
        "linear:argument:Team.projects.includeArchived": 1,
    }


def test_an_argument_the_fake_serves_is_not_counted_when_no_record_is_listed(
    fakes: RunningFakes,
) -> None:
    """The viewer is assigned no issue, so nothing is filtered: the arguments are served all the same."""
    answer = _linear(
        fakes,
        '{ viewer { assignedIssues(filter: {title: {contains: "x"}}, includeArchived: true, orderBy: updatedAt) '
        "{ nodes { id } } } }",
    )

    assert answer.status_code == 200 and "errors" not in answer.json()
    assert answer.json()["data"]["viewer"]["assignedIssues"]["nodes"] == []
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_a_nested_filter_names_the_type_linear_names(fakes: RunningFakes) -> None:
    refused = _linear(
        fakes,
        '{ issues(filter: {assignee: {createdAt: {lt: "P1D"}}, labels: {some: {isGroup: {eq: true}}}}) { nodes { id } } }',
    )

    assert refused.status_code == 200 and refused.json()["data"] is None
    assert refused.json()["errors"][0]["message"] == 'Field "createdAt" is not defined by type "NullableUserFilter".'
    assert _control(fakes, "health")["unsupported_calls"] == {
        "linear:filter:NullableUserFilter.createdAt": 1,
    }


def test_every_graphql_request_goes_through_the_public_schema(fakes: RunningFakes) -> None:
    """A mutation is refused as Linear's SDK reads a 403 and counted; a GET carries its document in
    the URL and is validated like a POST. Neither reaches the emulator's own schema."""
    before = _control(fakes, "health")["counts"]["linear"]

    refused = _linear(
        fakes,
        'mutation { issueCreate(input: {teamId: "ENG", title: "x"}) { success } commentCreate(input: {body: "y"}) { success } }',
    )
    got = httpx.get(
        "{}/graphql".format(fakes.linear_url),
        params={"query": "{ viewer { name } }"},
        headers={"Authorization": _LINEAR_TOKEN},
    )
    invalid_get = httpx.get(
        "{}/graphql".format(fakes.linear_url),
        params={"query": "{ viewer { noSuchField } }"},
        headers={"Authorization": _LINEAR_TOKEN},
    )
    unvalidated_write = _linear(fakes, "mutation { noSuchMutation { success } }")

    assert refused.status_code == 403
    assert refused.json()["errors"][0]["extensions"]["type"] == "forbidden"
    assert "data" not in refused.json()
    assert (got.status_code, got.json()["data"]["viewer"]["name"]) == (200, "Morgan Reyes")
    assert invalid_get.status_code == 400
    assert unvalidated_write.status_code == 400
    assert _control(fakes, "health")["counts"]["linear"] == before
    assert _control(fakes, "health")["unsupported_calls"] == {
        "linear:mutation:issueCreate": 1,
        "linear:mutation:commentCreate": 1,
    }
    assert [line["unsupported"] for line in _control(fakes, "log")] == [
        "linear:mutation:issueCreate",
        None,
        None,
        None,
    ]


def test_a_write_is_refused_only_when_it_is_the_operation_that_would_run(
    fakes: RunningFakes,
) -> None:
    document = (
        'mutation Write { issueCreate(input: {teamId: "ENG", title: "x"}) { success } } query Read { viewer { name } }'
    )

    def post(operation_name: str) -> httpx.Response:
        return httpx.post(
            "{}/graphql".format(fakes.linear_url),
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
        fakes,
        'mutation { ...Archive } fragment Archive on Mutation { issueArchive(id: "ENG-1") { success } }',
    )

    assert write.status_code == 403
    assert through_fragment.status_code == 403
    assert _control(fakes, "health")["unsupported_calls"] == {
        "linear:mutation:issueCreate": 1,
        "linear:mutation:issueArchive": 1,
    }


def test_a_query_missing_its_required_variable_is_refused_with_400(fakes: RunningFakes) -> None:
    answer = _linear(fakes, "query One($id: String!) { issue(id: $id) { identifier } }")

    assert answer.status_code == 400
    assert "data" not in answer.json()
    assert answer.json()["errors"][0]["message"].startswith('Variable "$id" has invalid value')


def test_a_get_carries_its_variables_as_json_in_the_url(fakes: RunningFakes) -> None:
    def get(variables: str) -> httpx.Response:
        return httpx.get(
            "{}/graphql".format(fakes.linear_url),
            params={"query": "query One($id: String!) { issue(id: $id) { identifier } }", "variables": variables},
            headers={"Authorization": _LINEAR_TOKEN},
        )

    found = get('{"id": "ENG-1"}')
    malformed = get("id=ENG-1")

    assert (found.status_code, found.json()["data"]["issue"]["identifier"]) == (200, "ENG-1")
    assert (malformed.status_code, malformed.json()["errors"][0]["message"]) == (400, "Variables are invalid JSON.")


def test_a_slack_method_the_fake_does_not_have_is_answered_as_slack_does_and_counted(
    fakes: RunningFakes,
) -> None:
    asked = httpx.get(
        "{}/api/admin.users.list".format(fakes.slack_url), params={"limit": "5"}, headers=_SLACK_HEADERS
    )
    posted = httpx.post("{}/api/admin.users.list".format(fakes.slack_url), headers=_SLACK_HEADERS)
    other = httpx.get("{}/api/no.such.method".format(fakes.slack_url), headers=_SLACK_HEADERS)
    # A method the fake has, a call that fails for the caller's own reason, and a path that is no method.
    _slack(fakes, "auth.test")
    missing_argument = _slack(fakes, "conversations.history")
    not_a_method = httpx.get("{}/no/such/path".format(fakes.slack_url), headers=_SLACK_HEADERS)

    for answer in (asked, posted, other):
        assert (answer.status_code, answer.json()) == (200, {"ok": False, "error": "unknown_method"})
    assert missing_argument["ok"] is False and missing_argument["error"] != "unknown_method"
    assert not_a_method.status_code == 404
    assert _control(fakes, "health")["unsupported_calls"] == {
        "slack:admin.users.list": 2,
        "slack:no.such.method": 1,
    }
    assert [(line["status"], line["error"], line["unsupported"]) for line in _control(fakes, "log")] == [
        (200, "unknown_method", "slack:admin.users.list"),
        (200, "unknown_method", "slack:admin.users.list"),
        (200, "unknown_method", "slack:no.such.method"),
        (200, None, None),
        (200, missing_argument["error"], None),
        (404, None, None),
    ]


def test_every_gap_of_one_query_is_counted_once_and_the_log_names_the_first(
    fakes: RunningFakes,
) -> None:
    answer = _linear(fakes, "{ viewer { timezone guest } users { nodes { timezone } } }")

    assert answer.json()["data"]["viewer"] == {"timezone": None, "guest": False}
    assert _control(fakes, "health")["unsupported_calls"] == {
        "linear:field:User.timezone": 1,
        "linear:field:User.guest": 1,
    }
    assert [line["unsupported"] for line in _control(fakes, "log")] == ["linear:field:User.timezone"]


def test_introspection_shows_linears_own_schema(fakes: RunningFakes) -> None:
    """A workspace agent that introspects sees the fields the real API has, the fake's gaps among them,
    and never a type the fake made up."""
    project = _linear(fakes, '{ __type(name: "Project") { fields { name } } }')
    filters = _linear(fakes, '{ __type(name: "IssueFilter") { kind inputFields { name } } }')

    assert {"lead", "slugId", "status", "issues", "name"} <= {
        f["name"] for f in project.json()["data"]["__type"]["fields"]
    }
    assert filters.json()["data"]["__type"]["kind"] == "INPUT_OBJECT"
    assert {"slaStatus", "title", "state"} <= {f["name"] for f in filters.json()["data"]["__type"]["inputFields"]}


def test_an_update_that_is_not_json_is_refused_as_one(fakes: RunningFakes) -> None:
    before = _control(fakes, "health")["counts"]

    for body in ("not json", "", '{"issues": ['):
        refused = _add(fakes, "linear", body)
        assert (refused.status_code, refused.json()) == (400, {"ok": False, "error": "the update is not JSON"})

    assert _control(fakes, "health")["counts"] == before


def test_the_access_log_holds_one_line_per_request_and_no_token(fakes: RunningFakes) -> None:
    _control(fakes, "health")
    httpx.get(
        "{}/api/conversations.list".format(fakes.slack_url),
        params={"limit": "1", "token": _SLACK_TOKEN},
        headers={**_SLACK_HEADERS, "User-Agent": "roadmap-app/1.0", "X-Eval-Caller": "app"},
    )
    _linear(fakes, "{ teams { nodes { key } } }")
    _linear(
        fakes,
        'mutation { issueCreate(input: {teamId: "ENG", title: "x"}) { success } }',
        token="lin_api_other",
    )

    log_text = fakes.access_log_path.read_text()
    slack_line, linear_line, refused_line = (json.loads(line) for line in log_text.splitlines())

    assert _SLACK_TOKEN not in log_text and _LINEAR_TOKEN not in log_text
    assert " ".join(slack_line) == (
        "ts seq phase service method url body content_type token_user token_sha256_8 user_agent caller "
        "status ok error unsupported rate_limited fault result_count response_bytes duration_ms"
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
    assert _control(fakes, "log") == [slack_line, linear_line, refused_line]


def _mark_phase(services: RunningFakes, body: Any) -> httpx.Response:
    return httpx.post("{}/_control/phase".format(services.control_url), json=body)


def test_changes_are_what_the_running_phase_did_to_the_stores(fakes: RunningFakes) -> None:
    """What a verification flow's press wrote is read from the fakes themselves: the records the
    stores gained or lost and the ones that differ since the phase was marked, apart from whatever
    the agent's own phase wrote before it."""
    channel_id = _channel_id(fakes, "eng-oncall")
    agent_post = _slack_post(
        fakes, "chat.postMessage", channel=channel_id, text="Posted while the agent worked."
    )
    assert _control(fakes, "changes")["previous_phase"] is None

    _mark_phase(fakes, {"name": "verification"})

    quiet = _control(fakes, "changes")
    assert (quiet["phase"], quiet["changes"], quiet["calls"]) == ("verification", {}, {})
    _slack_post(fakes, "chat.postMessage", channel=channel_id, text="Posted by a flow.")
    deleted = _slack_post(fakes, "chat.delete", channel=channel_id, ts=agent_post["ts"])
    assert deleted["ok"], deleted
    message_id = _message_keys(fakes)["invoice-question"]
    modified = httpx.post(
        "{}/gmail/v1/users/me/messages/{}/modify".format(fakes.google_url, message_id),
        headers={"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)},
        json={"removeLabelIds": ["INBOX"]},
    )
    assert modified.status_code == 200, modified.text
    # A read changes nothing and is not among the calls.
    _slack(fakes, "auth.test")
    answer = _control(fakes, "changes")

    assert answer["shift_ms"] == _control(fakes, "health")["shift_ms"]
    posted = answer["changes"]["slack"]["slack.messages"]
    assert [message["text"] for message in posted["added"]] == ["Posted by a flow."]
    assert [message["text"] for message in posted["removed"]] == ["Posted while the agent worked."]
    assert posted["changed"] == []
    relabelled = answer["changes"]["google"]["google.messages"]
    assert (relabelled["added"], relabelled["removed"]) == ([], [])
    (change,) = relabelled["changed"]
    assert (change["before"]["seed_key"], change["after"]["seed_key"]) == ("invoice-question", "invoice-question")
    assert "INBOX" in change["before"]["label_ids"] and "INBOX" not in change["after"]["label_ids"]
    # A collection no write reached is left out.
    assert "google.calendar_events" not in answer["changes"]["google"]
    assert answer["calls"] == {
        "POST slack.com/api/chat.postMessage": 1,
        "POST slack.com/api/chat.delete": 1,
        "POST gmail.googleapis.com/gmail/v1/users/me/messages/{}/modify".format(message_id): 1,
    }
    previous = answer["previous_phase"]
    assert (previous["phase"], previous["calls"]) == ("agent", {"POST slack.com/api/chat.postMessage": 1})
    # A keyed record is how a check finds what a new one refers to: a seeded thread's id, a dm's channel.
    keyed = answer["keyed"]
    assert "invoice-question" in {message["seed_key"] for message in keyed["google"]["google.messages"]}
    assert {channel["seed_key"] for channel in keyed["slack"]["slack.channels"]} == {"dm-claire", "leads-group"}
    assert _SLACK_TOKEN not in json.dumps(answer) and _GOOGLE_TOKEN not in json.dumps(answer)

    # A later step's phases start from their own marks.
    _mark_phase(fakes, {"name": "agent"})
    later = _control(fakes, "changes")
    assert (later["phase"], later["changes"], later["calls"]) == ("agent", {}, {})
    assert later["previous_phase"]["phase"] == "verification"


def test_a_phase_mark_stamps_every_later_request_and_counts_it_apart(
    fakes: RunningFakes,
) -> None:
    """The verification flows drive the agent's own app, whose requests look like any other: the
    phase the driver marks is what tells them from the ones the app made while the agent worked."""
    assert _control(fakes, "health")["phases"] == {
        "agent": {
            "requests": {"slack": 0, "linear": 0, "google": 0},
            "unsupported_calls": {},
            "rate_limited": {},
            "faulted": {},
        }
    }
    _slack(fakes, "auth.test")
    httpx.get("{}/api/admin.users.list".format(fakes.slack_url), headers=_SLACK_HEADERS)

    marked = _mark_phase(fakes, {"name": "verification"})

    _slack(fakes, "auth.test")
    httpx.get("{}/api/admin.users.list".format(fakes.slack_url), headers=_SLACK_HEADERS)
    _linear(fakes, "{ teams { nodes { key } } }")
    assert (marked.status_code, marked.json()) == (200, {"ok": True, "phase": "verification", "previous": "agent"})
    log_lines = _control(fakes, "log")
    assert [(line["phase"], line["service"]) for line in log_lines] == [
        ("agent", "slack"),
        ("agent", "slack"),
        ("verification", "slack"),
        ("verification", "slack"),
        ("verification", "linear"),
    ]
    file_lines = [json.loads(line) for line in fakes.access_log_path.read_text().splitlines()]
    assert [line["phase"] for line in file_lines] == [line["phase"] for line in log_lines]
    health = _control(fakes, "health")
    # The totals keep their shape, and each phase's counts sit beside them.
    assert health["counts"]["requests"] == {"slack": 4, "linear": 1, "google": 0}
    assert health["unsupported_calls"] == {"slack:admin.users.list": 2}
    assert health["phase"] == "verification"
    assert health["phases"] == {
        "agent": {
            "requests": {"slack": 2, "linear": 0, "google": 0},
            "unsupported_calls": {"slack:admin.users.list": 1},
            "rate_limited": {},
            "faulted": {},
        },
        "verification": {
            "requests": {"slack": 2, "linear": 1, "google": 0},
            "unsupported_calls": {"slack:admin.users.list": 1},
            "rate_limited": {},
            "faulted": {},
        },
    }
    state = _control(fakes, "state")
    assert (state["phase"], state["phases"]) == (health["phase"], health["phases"])

    # A later step's conversation is the agent's again, and adds to the agent's counts.
    _mark_phase(fakes, {"name": "agent"})
    _slack(fakes, "auth.test")
    assert _control(fakes, "health")["phases"]["agent"]["requests"] == {
        "slack": 3,
        "linear": 0,
        "google": 0,
    }
    assert _control(fakes, "log")[-1]["phase"] == "agent"


@pytest.mark.parametrize("body", [{"name": "Verification phase"}, {"name": ""}, {"name": 3}, {}])
def test_a_phase_mark_that_names_no_identifier_is_refused_and_changes_nothing(
    fakes: RunningFakes, body: Any
) -> None:
    refused = _mark_phase(fakes, body)

    assert refused.status_code == 400 and refused.json()["ok"] is False
    assert _control(fakes, "health")["phase"] == "agent"
    assert list(_control(fakes, "health")["phases"]) == ["agent"]


def test_a_phase_named_like_an_object_key_is_counted_like_any_other(
    fakes: RunningFakes,
) -> None:
    marked = _mark_phase(fakes, {"name": "constructor"})
    answered = _slack(fakes, "auth.test")

    assert marked.status_code == 200
    assert answered["ok"] is True
    phases = _control(fakes, "health")["phases"]
    assert phases["constructor"] == {
        "requests": {"slack": 1, "linear": 0, "google": 0},
        "unsupported_calls": {},
        "rate_limited": {},
        "faulted": {},
    }


def test_the_state_dump_redacts_every_token(fakes: RunningFakes) -> None:
    dump_text = httpx.get("{}/_control/state".format(fakes.control_url)).text
    dump = json.loads(dump_text)

    assert _SLACK_TOKEN not in dump_text and _LINEAR_TOKEN not in dump_text
    assert "[redacted]" in dump_text
    assert dump["counts"]["slack"] == {"users": 3, "channels": 2, "dms": 2, "messages": 7}
    assert dump["counts"]["linear"]["issues"] == len(
        fixture_document("seed", "linear")["issues"]
    )
    # Relations are a collection of the Linear store's own, dumped and restored with the rest.
    relations = dump["linear"]["collections"]["linear.issue_relations"]["items"]
    assert [relation["type"] for relation in relations] == ["blocks"]


def test_no_answer_says_what_is_serving_it(fakes: RunningFakes) -> None:
    """A workspace agent must not be able to tell a fake from the service, least of all from an
    error: these are the answers a client gets for asking something that is not served."""
    answers = [
        _linear(fakes, "{ issues(filter: {slaStatus: {eq: Breached}}) { nodes { id } } }"),
        _linear(fakes, '{ issues(filter: {title: {regex: "x"}}) { nodes { id } } }'),
        _linear(fakes, '{ issues(filter: {createdAt: {gt: "last week"}}) { nodes { id } } }'),
        _linear(fakes, "{ noSuchField }"),
        _linear(fakes, "{ viewer { name }", token="lin_api_other"),
        _linear(fakes, 'mutation { issueCreate(input: {teamId: "NOPE", title: "x"}) { success } }'),
        httpx.get("{}/".format(fakes.linear_url), headers={"Authorization": _LINEAR_TOKEN}),
        httpx.get("{}/".format(fakes.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/no/such/path".format(fakes.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/api/no.such.method".format(fakes.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/api/conversations.history".format(fakes.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/api/auth.test".format(fakes.slack_url)),
        httpx.get(
            "{}/files-pri/T1-F1/report.pdf".format(fakes.slack_url),
            headers={**_SLACK_HEADERS, "X-Forwarded-Host": "files.slack.com"},
        ),
        httpx.get(
            "{}/abc".format(fakes.linear_url),
            headers={"Authorization": _LINEAR_TOKEN, "X-Forwarded-Host": "uploads.linear.app"},
        ),
        httpx.get("{}/_control/health".format(fakes.slack_url), headers=_SLACK_HEADERS),
        httpx.get("{}/_control/state".format(fakes.linear_url), headers={"Authorization": _LINEAR_TOKEN}),
        _gmail_get(fakes, "me/settings/vacation"),
        _gmail_get(fakes, "me/messages/0000000000000000"),
        _gmail_get(fakes, "me/profile", token=None),
        _gmail_get(fakes, "me/profile", token="ya29.other"),
        _gmail_get(fakes, "thomas.baird@redwood.example/profile"),
        _gmail_get(fakes, "me/messages", pageToken="x"),
        _gmail_get(fakes, "me/messages", q="nothing-says-this"),
        _gmail_get(fakes, "me/threads", q="invoice"),
        _gmail_get(fakes, "me/messages/8b5677a1c796501d", format="raw"),
        _gmail_get(fakes, "me/threads/8b5677a1c796501d"),
        _gmail_get(fakes, "me/history", startHistoryId="1"),
        _gmail_get(fakes, "me/labels"),
        _gmail_get(fakes, "me/settings/sendAs"),
        _gmail_get(fakes, "me/drafts"),
        _calendar_get(fakes, "users/me/calendarList"),
        _calendar_get(fakes, "calendars/primary"),
        _calendar_get(fakes, "calendars/primary/events", maxResults="1"),
        _calendar_get(fakes, "calendars/primary/events", orderBy="startTime"),
        _calendar_get(fakes, "calendars/primary/events", timeMin="yesterday"),
        _calendar_get(fakes, "calendars/primary/events", pageToken="x"),
        _calendar_get(fakes, "calendars/primary/events/0000000000"),
        _calendar_get(fakes, "calendars/thomas.baird@redwood.example/events"),
        _calendar_get(fakes, "colors"),
        httpx.get("{}/".format(fakes.google_url)),
        httpx.get("{}/o/oauth2/v2/auth".format(fakes.google_url)),
        httpx.get("{}/oauth2/v2/userinfo".format(fakes.google_url)),
        _userinfo(fakes),
        _userinfo(fakes, token=None),
        _userinfo(fakes, token="ya29.other"),
        httpx.get(
            "{}/gmail/v1/users/me/profile".format(fakes.google_url),
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
def _slack_burst(services: RunningFakes, method: str, count: int, **params: str) -> list[httpx.Response]:
    with httpx.Client(headers=_SLACK_HEADERS) as client:
        return [client.get("{}/api/{}".format(services.slack_url, method), params=params) for _ in range(count)]


def _replies_params(services: RunningFakes) -> dict[str, str]:
    return {"channel": _channel_id(services, "eng-oncall"), "ts": _shifted("1773900000.000000")}


# Tier 3, 50 a minute, and the burst allowance of a fifth more.
_TIER_3_LIMIT = 60


def test_a_burst_past_a_slack_methods_tier_is_refused_as_slack_refuses_it(
    fakes: RunningFakes,
) -> None:
    burst = _slack_burst(
        fakes, "conversations.replies", _TIER_3_LIMIT + 2, **_replies_params(fakes)
    )
    history = _slack(fakes, "conversations.history", channel=_channel_id(fakes, "eng-oncall"))

    assert [answer.json()["ok"] for answer in burst[:_TIER_3_LIMIT]] == [True] * _TIER_3_LIMIT
    for refused in burst[_TIER_3_LIMIT:]:
        assert refused.status_code == 429
        assert refused.json() == {"ok": False, "error": "ratelimited"}
        assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert "retry-after" not in burst[0].headers
    # Slack limits each method on its own: the methods around the burst still answer.
    assert history["ok"] is True
    expected = {"slack:conversations.replies": 2}
    assert _control(fakes, "health")["rate_limited"] == expected
    assert _control(fakes, "state")["rate_limited"] == expected
    assert _control(fakes, "health")["rate_limits"] == "real"
    log = _control(fakes, "log")
    replies_lines = [line for line in log if "conversations.replies" in line["url"]]
    assert [(line["status"], line["error"], line["rate_limited"]) for line in replies_lines[-3:]] == [
        (200, None, None),
        (429, "ratelimited", "slack:conversations.replies"),
        (429, "ratelimited", "slack:conversations.replies"),
    ]


def test_a_rate_limited_request_is_counted_under_the_phase_it_was_made_in(
    fakes: RunningFakes,
) -> None:
    params = _replies_params(fakes)
    agent_burst = _slack_burst(fakes, "conversations.replies", _TIER_3_LIMIT + 1, **params)
    _mark_phase(fakes, {"name": "verification"})
    # A bucket knows no phase, so the window the agent filled refuses these too.
    verification_burst = _slack_burst(fakes, "conversations.replies", 2, **params)

    assert [answer.status_code for answer in agent_burst[_TIER_3_LIMIT:] + verification_burst] == [429] * 3
    health = _control(fakes, "health")
    assert health["rate_limited"] == {"slack:conversations.replies": 3}
    assert health["phases"]["agent"]["rate_limited"] == {"slack:conversations.replies": 1}
    assert health["phases"]["verification"]["rate_limited"] == {"slack:conversations.replies": 2}
    assert _control(fakes, "state")["phases"] == health["phases"]
    assert [line["phase"] for line in _control(fakes, "log") if line["rate_limited"]] == [
        "agent",
        "verification",
        "verification",
    ]


def test_a_slack_method_answers_again_once_its_retry_after_has_passed(
    fakes_with_short_rate_limit_windows: RunningFakes,
) -> None:
    services = fakes_with_short_rate_limit_windows
    # users.list is Tier 2: 20 a minute and 4 more.
    burst = _slack_burst(services, "users.list", 25)
    retry_after = int(burst[-1].headers["retry-after"])
    # A refused request draws on nothing, so asking until one is answered waits the window out.
    deadline = time.monotonic() + TEST_RATE_LIMIT_WINDOW_SECONDS + 3
    while httpx.get("{}/api/users.list".format(services.slack_url), headers=_SLACK_HEADERS).status_code != 200:
        assert time.monotonic() < deadline, "users.list was still refused after its window"
        time.sleep(0.1)

    assert [answer.status_code for answer in burst] == [200] * 24 + [429]
    assert 1 <= retry_after <= TEST_RATE_LIMIT_WINDOW_SECONDS


def test_a_seed_that_asks_for_no_rate_limits_serves_an_unlimited_burst(
    unlimited_fakes: RunningFakes,
) -> None:
    burst = _slack_burst(
        unlimited_fakes,
        "conversations.replies",
        3 * _TIER_3_LIMIT,
        **_replies_params(unlimited_fakes),
    )
    linear_answer = _linear(unlimited_fakes, "{ viewer { id } }")

    assert {answer.status_code for answer in burst} == {200}
    assert "x-complexity" not in linear_answer.headers
    health = _control(unlimited_fakes, "health")
    assert (health["rate_limits"], health["rate_limited"]) == ("none", {})


def _until_served(get: Callable[[], httpx.Response], refused_status: int, seconds: int) -> list[httpx.Response]:
    """Asks until an answer is served again, as a client waiting out an episode does, and returns the
    asks the episode refused on the way: at most one per quarter second, so fewer than ten over a
    two-second episode."""
    refused: list[httpx.Response] = []

    def served() -> bool:
        answer = get()
        if answer.status_code == refused_status:
            refused.append(answer)
            return False
        return True

    deadline = time.monotonic() + seconds + 3
    while not served():
        assert time.monotonic() < deadline, "the episode never ended"
        time.sleep(0.25)
    assert all(1 <= int(answer.headers["retry-after"]) <= seconds for answer in refused)
    return refused


def _requests(get: Callable[[], httpx.Response], first: int, last: int) -> list[httpx.Response]:
    """Requests `first` to `last` of a service's count, inclusive."""
    return [get() for _ in range(first, last + 1)]


def _up_to_the_episode(
    get: Callable[[], httpx.Response], already_made: int = 0
) -> tuple[list[httpx.Response], httpx.Response, httpx.Response, httpx.Response]:
    """Walk TEST_FAULTS from request `already_made + 1` of a service's count to the
    one that opens the rate limit episode: the answers served on the way, then the transient error,
    the unreadable page and the episode's opener."""
    faults = TEST_FAULTS
    served = _requests(get, already_made + 1, faults["transient_error_every"] - 1)
    transient = get()
    served += _requests(get, faults["transient_error_every"] + 1, faults["unreadable_every"] - 1)
    unreadable = get()
    served += _requests(get, faults["unreadable_every"] + 1, faults["rate_limit_episode_every"] - 1)
    return served, transient, unreadable, get()


def test_a_seed_with_faults_is_served_misbehaving_on_its_schedule(
    faulted_fakes: RunningFakes,
) -> None:
    """Requests 1 and 2 are spared; request 50 fails transiently; request 60 is unreadable; request 70
    opens a 2 s rate limit episode that request 71 and the asks that wait it out land in; everything
    else is served, and a faulted post changes nothing."""
    services = faulted_fakes
    faults = TEST_FAULTS
    channel = _channel_id(services, "eng-oncall")
    before = _slack(services, "conversations.history", channel=channel)["messages"]
    with httpx.Client(headers=_SLACK_HEADERS) as client:

        def get() -> httpx.Response:
            return client.get("{}/api/auth.test".format(services.slack_url))

        # The Slack count stands at 2: conversations.list in _channel_id and the history read.
        served, transient, unreadable, episode_opener = _up_to_the_episode(get, already_made=2)
        inside_episode = client.post(
            "{}/api/chat.postMessage".format(services.slack_url),
            json={"channel": channel, "text": "lost to the episode"},
        )
        refused_asks = _until_served(get, 429, faults["rate_limit_episode_seconds"])

    assert [
        (answer.status_code, answer.text[:80])
        for answer in served
        if answer.status_code != 200 or answer.json().get("ok") is not True
    ] == []
    # Slack answers internal_error with HTTP 200, as it does every API error but a rate limit.
    assert (transient.status_code, transient.json()) == (200, {"ok": False, "error": "internal_error"})
    assert unreadable.status_code == 503
    assert unreadable.headers["content-type"].startswith("text/html")
    assert "<html>" in unreadable.text
    assert (episode_opener.status_code, episode_opener.json()) == (429, {"ok": False, "error": "ratelimited"})
    assert int(episode_opener.headers["retry-after"]) == faults["rate_limit_episode_seconds"]
    assert (inside_episode.status_code, inside_episode.json()["error"]) == (429, "ratelimited")
    # The post the episode refused never reached the store.
    assert _slack(services, "conversations.history", channel=channel)["messages"] == before
    health = _control(services, "health")
    assert health["faults"] == faults
    episode_refusals = 2 + len(refused_asks)
    assert health["faulted"] == {
        "slack:transient_error": 1,
        "slack:unreadable": 1,
        "slack:rate_limit_episode": episode_refusals,
    }
    assert health["phases"]["agent"]["faulted"] == health["faulted"]
    assert _control(services, "state")["faulted"] == health["faulted"]
    # A faulted request is logged as such and drew on no rate limit.
    faulted_lines = [line for line in _control(services, "log") if line["fault"]]
    assert [(line["status"], line["error"], line["fault"], line["rate_limited"]) for line in faulted_lines] == [
        (200, "internal_error", "slack:transient_error", None),
        (503, None, "slack:unreadable", None),
    ] + [(429, "ratelimited", "slack:rate_limit_episode", None)] * episode_refusals
    assert health["rate_limited"] == {}


def test_linear_and_google_misbehave_in_their_own_shapes(faulted_fakes: RunningFakes) -> None:
    """The one schedule applies to every service, each refusing and failing as its vendor does."""
    services = faulted_fakes
    faults = TEST_FAULTS
    seconds = faults["rate_limit_episode_seconds"]
    with httpx.Client(headers={"Authorization": _LINEAR_TOKEN}) as client:

        def post() -> httpx.Response:
            return client.post(services.linear_url + "/graphql", json={"query": "{ viewer { id } }"})

        served, failed, unreadable, refused = _up_to_the_episode(post)
        linear_refusals = _until_served(post, 429, seconds)
    with httpx.Client(headers={"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)}) as client:

        def profile() -> httpx.Response:
            return client.get(services.google_url + "/gmail/v1/users/me/profile")

        google_served, google_failed, google_unreadable, google_refused = _up_to_the_episode(profile)
        # A product with no limit of its own is refused in an episode all the same.
        userinfo_refused = _userinfo(services)
        google_refusals = _until_served(profile, 403, seconds)

    assert [
        (answer.status_code, answer.text[:80])
        for answer in served
        if answer.status_code != 200 or "errors" in answer.json()
    ] == []
    assert (failed.status_code, failed.json()["errors"][0]["message"]) == (500, "Internal server error")
    assert (unreadable.status_code, unreadable.headers["content-type"].split(";")[0]) == (503, "text/html")
    assert refused.status_code == 429
    assert refused.json()["errors"][0]["extensions"]["code"] == "RATELIMITED"
    assert int(refused.headers["retry-after"]) == seconds
    assert all(answer.status_code == 200 for answer in google_served)
    assert (google_failed.status_code, google_failed.json()["error"]["status"]) == (500, "INTERNAL")
    assert google_unreadable.status_code == 503
    assert google_refused.status_code == 403
    assert google_refused.json()["error"]["errors"][0]["reason"] == "userRateLimitExceeded"
    assert int(google_refused.headers["retry-after"]) == seconds
    assert (userinfo_refused.status_code, userinfo_refused.json()["error"]["status"]) == (429, "RESOURCE_EXHAUSTED")
    assert 1 <= int(userinfo_refused.headers["retry-after"]) <= seconds
    faulted = _control(services, "health")["faulted"]
    assert {key: count for key, count in faulted.items() if not key.startswith("slack:")} == {
        "linear:transient_error": 1,
        "linear:unreadable": 1,
        "linear:rate_limit_episode": 1 + len(linear_refusals),
        "google:transient_error": 1,
        "google:unreadable": 1,
        "google:rate_limit_episode": 2 + len(google_refusals),
    }


def test_a_seed_without_faults_is_served_without_any(fakes: RunningFakes) -> None:
    health = _control(fakes, "health")

    assert (health["faults"], health["faulted"]) == (None, {})
    assert all(line["fault"] is None for line in _control(fakes, "log"))


def _launcher_started_ms() -> float:
    anchor = fixture_document("seed", "linear")["anchor"]
    return (
        datetime.fromisoformat(anchor.replace("Z", "+00:00")).timestamp() + TEST_SHIFT_SECONDS
    ) * 1000


def test_every_linear_answer_carries_linears_rate_limit_headers(fakes: RunningFakes) -> None:
    answer = _linear(fakes, "{ viewer { name } }")
    # Linear's own examples: a user and three fields of its created issues, 50 by default, is 66
    # points, and 14 with `first: 10`.
    default_page = _linear(fakes, "{ viewer { createdIssues { nodes { id title createdAt } } } }")
    ten = _linear(fakes, "{ viewer { createdIssues(first: 10) { nodes { id title createdAt } } } }")
    by_variable = _linear(
        fakes,
        "query Q($n: Int) { viewer { createdIssues(first: $n) { nodes { id title createdAt } } } }",
        variables={"n": 10},
    )
    by_default = _linear(
        fakes,
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
    fakes: RunningFakes,
) -> None:
    at_maximum = _linear(fakes, _MAX_COMPLEXITY_QUERY)
    past_maximum = _linear(
        fakes,
        "{ teams(first: 100) { nodes { issues(first: 67) { nodes { id title description createdAt updatedAt } } } } }",
    )
    after = _linear(fakes, "{ viewer { id } }")

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
    assert _control(fakes, "health")["rate_limited"] == {"linear:complexity": 1}


def test_linear_scores_a_query_as_the_real_api_charges_it(fakes: RunningFakes) -> None:
    scored = [
        (query, int(_linear(fakes, query).headers["x-complexity"])) for query, _ in _MEASURED_COMPLEXITIES
    ]

    assert scored == list(_MEASURED_COMPLEXITIES)


# The answers up to the first refusal, and how many seconds sending them took.
def _linear_until_refused(
    services: RunningFakes, query: str, at_most: int
) -> tuple[list[httpx.Response], float]:
    answers: list[httpx.Response] = []
    began = time.monotonic()
    with httpx.Client(headers={"Authorization": _LINEAR_TOKEN}) as client:
        while len(answers) < at_most and (not answers or answers[-1].status_code == 200):
            answers.append(client.post("{}/graphql".format(services.linear_url), json={"query": query}))
    return answers, time.monotonic() - began


def test_linear_refuses_a_user_past_its_hourly_complexity(fakes: RunningFakes) -> None:
    answers, elapsed_seconds = _linear_until_refused(fakes, _MAX_COMPLEXITY_QUERY, 400)
    refused = answers[-1]

    # 3,000,000 points an hour hold 300 queries of 10,000, and one more drains away every 12 seconds.
    assert 300 <= len(answers) - 1 <= 300 + elapsed_seconds / 12 + 1
    assert refused.status_code == 429
    assert refused.json()["errors"][0]["extensions"]["code"] == "RATELIMITED"
    assert "complexity" in refused.json()["errors"][0]["message"]
    # The bucket drains at 3,000,000 points an hour, so room for one more query takes seconds.
    assert 1 <= int(refused.headers["retry-after"]) <= 13
    assert int(refused.headers["x-ratelimit-complexity-remaining"]) < 10000
    assert _control(fakes, "health")["rate_limited"] == {"linear:complexity": 1}
    assert _control(fakes, "log")[-1]["rate_limited"] == "linear:complexity"


def test_linear_refuses_a_user_past_its_hourly_requests(
    fakes_with_a_small_linear_request_bucket: RunningFakes,
) -> None:
    services = fakes_with_a_small_linear_request_bucket
    limit = TEST_LINEAR_REQUEST_LIMIT
    answers, elapsed_seconds = _linear_until_refused(services, "{ viewer { id } }", 2 * limit)
    refused = answers[-1]
    # As Linear's does, the bucket drains its whole limit in an hour, one request at a time.
    drain_seconds = 60 * 60 / limit

    # The bucket's limit, and one more for each drain period the burst took to send.
    assert limit <= len(answers) - 1 <= limit + elapsed_seconds / drain_seconds + 1
    assert refused.status_code == 429
    assert refused.json()["errors"][0]["extensions"]["code"] == "RATELIMITED"
    assert (refused.headers["x-ratelimit-requests-limit"], refused.headers["x-ratelimit-requests-remaining"]) == (
        str(limit),
        "0",
    )
    # Room for one more comes one drain period after the burst began.
    assert drain_seconds - elapsed_seconds <= int(refused.headers["retry-after"]) <= drain_seconds
    assert _control(services, "health")["rate_limited"] == {"linear:requests": 1}


def test_the_google_store_holds_the_seed_and_nothing_of_the_emulators_own(
    fakes: RunningFakes,
) -> None:
    profile = _gmail(fakes, "me/profile")
    labels = _gmail(fakes, "me/labels")["labels"]
    dump = _control(fakes, "state")["google"]["collections"]

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
    fakes: RunningFakes,
) -> None:
    keys = _message_keys(fakes)

    question = _gmail(fakes, "me/messages/{}".format(keys["invoice-question"]))
    reply = _gmail(fakes, "me/messages/{}".format(keys["invoice-reply"]), format="metadata")

    # 200 days after 2026-03-16, in the offset each sender's clock kept.
    assert _headers(question)["Date"] == _shifted_date_header("2026-03-16T09:15:00-07:00")
    assert _headers(reply)["Date"] == _shifted_date_header("2026-03-16T17:40:00+01:00")
    assert question["internalDate"] == str(_shifted_ms("2026-03-16T09:15:00-07:00"))
    assert reply["internalDate"] == str(_shifted_ms("2026-03-16T17:40:00+01:00"))
    for message in (question, reply):
        assert abs(int(message["internalDate"]) / 1000 - time.time()) > 24 * 60 * 60


def test_a_gmail_thread_holds_its_replies_with_their_headers_and_derived_labels(
    fakes: RunningFakes,
) -> None:
    keys = _message_keys(fakes)
    vendors = next(
        label["id"] for label in _gmail(fakes, "me/labels")["labels"] if label["name"] == "Vendors"
    )

    thread = _gmail(fakes, "me/threads/{}".format(keys["invoice-question"]))
    raw = _gmail(fakes, "me/messages/{}".format(keys["invoice-reply"]), format="raw")

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


def test_a_gmail_message_shows_the_mime_headers_of_its_raw_form(fakes: RunningFakes) -> None:
    keys = _message_keys(fakes)

    question = _gmail(fakes, "me/messages/{}".format(keys["invoice-question"]))
    reply = _gmail(fakes, "me/messages/{}".format(keys["invoice-reply"]))
    reply_metadata = _gmail(fakes, "me/messages/{}".format(keys["invoice-reply"]), format="metadata")
    named = _gmail(
        fakes,
        "me/messages/{}".format(keys["invoice-reply"]),
        format="metadata",
        metadataHeaders="content-type",
    )
    thread = _gmail(fakes, "me/threads/{}".format(keys["invoice-question"]), format="metadata")
    reply_raw = email.message_from_bytes(
        base64.urlsafe_b64decode(
            _gmail(fakes, "me/messages/{}".format(keys["invoice-reply"]), format="raw")["raw"] + "=="
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
    fakes: RunningFakes,
) -> None:
    labels = _gmail(fakes, "me/labels")["labels"]
    inbox = _gmail(fakes, "me/labels/INBOX")

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


def test_gmail_lists_and_searches_with_the_operators_it_supports(fakes: RunningFakes) -> None:
    keys = _message_keys(fakes)
    question, reply, followup, newsletter = (
        keys[key] for key in ("invoice-question", "invoice-reply", "invoice-followup", "newsletter")
    )

    # Newest first; an archived message is listed, and only the mailbox's own.
    assert _listed_ids(fakes) == [followup, reply, question, newsletter]
    assert _listed_ids(fakes, labelIds="INBOX") == [followup, question]
    assert _listed_ids(fakes, q="from:me") == [reply]
    assert _listed_ids(fakes, q="label:Vendors is:unread") == [followup]
    assert _listed_ids(fakes, q='"corrected invoice"') == [followup]
    # An operator's quoted value is a phrase, matched whatever its case.
    for query in ('subject:"March invoice"', 'subject:"MARCH INVOICE"', '(subject:"march invoice")'):
        assert _listed_ids(fakes, q=query) == [followup, reply, question], query
    assert _listed_ids(fakes, q='subject:"invoice March"') == []
    assert _listed_ids(fakes, q='from:"Priya Shah" label:"Vendors"') == [followup, question]
    assert _listed_ids(fakes, q='subject:"March invoice" -label:"Vendors"') == [reply]
    # A date is midnight Pacific time, as Gmail reads it, and newer_than counts from the trial's present.
    day = _shifted_date("2026-03-16")
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    assert _listed_ids(
        fakes, q="after:{} before:{}".format(day.replace("-", "/"), next_day.replace("-", "/"))
    ) == [reply, question]
    assert _listed_ids(fakes, q="newer_than:3d") == [followup]
    assert _listed_ids(fakes, "threads") == [question, newsletter]
    # Gmail leaves an empty list out, and refuses a malformed page token.
    assert _gmail(fakes, "me/messages", q="nothing-says-this") == {"resultSizeEstimate": 0}
    assert _gmail(fakes, "me/threads", q="nothing-says-this") == {"resultSizeEstimate": 0}
    paged = _gmail(fakes, "me/messages", maxResults="2")
    assert (len(paged["messages"]), _listed_ids(fakes, pageToken=paged["nextPageToken"])) == (
        2,
        [question, newsletter],
    )
    bad_token = _gmail_get(fakes, "me/messages", pageToken="not-a-token")
    assert (bad_token.status_code, bad_token.json()["error"]["status"]) == (400, "INVALID_ARGUMENT")
    # A metadata header is matched whatever its case, as Gmail matches it.
    metadata = _gmail(
        fakes, "me/messages/{}".format(question), format="metadata", metadataHeaders="subject"
    )
    assert metadata["payload"]["headers"] == [{"name": "Subject", "value": "March invoice question"}]
    # A word with a colon that names no Gmail operator is a word to Gmail, and no gap of the fake's.
    assert _gmail(fakes, "me/messages", q="see:(notes) constructor:x https://northwind.example") == {
        "resultSizeEstimate": 0
    }
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_gmail_in_anywhere_searches_spam_and_trash_too(fakes: RunningFakes) -> None:
    update = {
        "messages": [
            {
                "key": key,
                "mailbox": _MORGAN,
                "from": "Promo <deals@promo.example>",
                "to": _MORGAN,
                "subject": "Invoice {} offer".format(label.lower()),
                "date": date,
                "body": "An offer.",
                "labels": [label],
            }
            for key, label, date in (
                ("spam-offer", "SPAM", "2026-03-17T10:00:00-07:00"),
                ("trashed-offer", "TRASH", "2026-03-18T10:00:00-07:00"),
            )
        ]
    }
    added = _add(fakes, "google", json.dumps(update))
    assert added.status_code == 200, added.text
    keys = _message_keys(fakes)
    question, reply, followup, newsletter, spam, trashed = (
        keys[key]
        for key in (
            "invoice-question",
            "invoice-reply",
            "invoice-followup",
            "newsletter",
            "spam-offer",
            "trashed-offer",
        )
    )

    # Without it a search leaves SPAM and TRASH out; with it, it is a search of all mail, archived included.
    assert _listed_ids(fakes, q="offer") == []
    assert _listed_ids(fakes, q="in:anywhere") == [followup, trashed, spam, reply, question, newsletter]
    assert _listed_ids(fakes, q="in:anywhere") == _listed_ids(fakes, includeSpamTrash="true")
    assert _listed_ids(fakes, q="IN:Anywhere offer") == [trashed, spam]
    assert _listed_ids(fakes, q="in:anywhere offer", includeSpamTrash="false") == [trashed, spam]
    assert _listed_ids(fakes, "threads", q="in:anywhere offer") == [trashed, spam]
    assert _control(fakes, "health")["unsupported_calls"] == {}
    # in:archive is still answered as the package answers it, and counted.
    _gmail_get(fakes, "me/messages", q="in:archive")
    assert _control(fakes, "health")["unsupported_calls"] == {"google:gmail:q:in:archive": 1}


def test_gmail_reads_a_get_format_whatever_its_case(fakes: RunningFakes) -> None:
    keys = _message_keys(fakes)

    message, thread = "me/messages/{}".format(keys["invoice-reply"]), "me/threads/{}".format(keys["invoice-question"])
    gets = [(message, name) for name in ("raw", "minimal", "metadata", "full")]
    gets += [(thread, name) for name in ("minimal", "metadata", "full")]
    for path, format_name in gets:
        upper = _gmail(fakes, path, format=format_name.upper())
        assert upper == _gmail(fakes, path, format=format_name), (path, format_name)
    raw = _gmail(fakes, message, format="RAW")
    assert "raw" in raw and "payload" not in raw
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_a_gmail_get_costs_what_its_format_costs_whatever_its_case(
    fakes: RunningFakes,
) -> None:
    message_id = _message_keys(fakes)["invoice-question"]
    # A RAW get costs 60 of a user's 6,000 units a minute, as raw does: 100 fit, and the 101st does not.
    burst = [
        _gmail_get(fakes, "me/messages/{}".format(message_id), format="RAW").status_code
        for _ in range(101)
    ]

    assert burst == [200] * 100 + [403]


def test_a_gmail_search_operator_the_fake_answers_wrongly_is_counted(
    fakes: RunningFakes,
) -> None:
    for query in ("invoice OR newsletter", "-from:priya", "cc:thomas", "{invoice newsletter}", "older_than:1y"):
        assert _gmail_get(fakes, "me/messages", q=query).status_code == 200
    # Gmail reads `or` and `and` in anything but capitals as words; AND is its implicit AND.
    for query in ("invoice or newsletter", "invoice Or newsletter", "and", "invoice AND question"):
        assert _gmail_get(fakes, "me/messages", q=query).status_code == 200
    _gmail_get(fakes, "me/threads", q="subject:(invoice question) has:drive")

    assert _control(fakes, "health")["unsupported_calls"] == {
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
    assert [line["unsupported"] for line in _control(fakes, "log")][-1] == "google:gmail:q:subject:()"


def test_a_google_path_or_host_the_fake_does_not_serve_is_googles_html_not_found_and_counted(
    fakes: RunningFakes,
) -> None:
    headers = {"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)}
    vacation = _gmail_get(fakes, "me/settings/vacation")
    vacation_again = _gmail_get(fakes, _MORGAN + "/settings/vacation")
    no_such_setting = _gmail_get(fakes, "me/settings/doesnotexist")
    no_such_method = _gmail_get(fakes, "me/nothinghere")
    drive = httpx.get(
        "{}/drive/v3/files".format(fakes.google_url),
        headers={**headers, "X-Forwarded-Host": "www.googleapis.com"},
    )
    oauth = httpx.get(
        "{}/o/oauth2/v2/auth".format(fakes.google_url),
        headers={**headers, "X-Forwarded-Host": "www.googleapis.com"},
    )
    other_host = httpx.get(
        "{}/gmail/v1/users/me/profile".format(fakes.google_url),
        headers={**headers, "X-Forwarded-Host": "people.googleapis.com"},
    )
    # Gmail is served on both of its hosts.
    on_www = httpx.get(
        "{}/gmail/v1/users/me/profile".format(fakes.google_url),
        headers={**headers, "X-Forwarded-Host": "www.googleapis.com"},
    )
    missing_message = _gmail_get(fakes, "me/messages/0000000000000000")

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
    assert _control(fakes, "health")["unsupported_calls"] == {
        "google:gmail:GET users/{userId}/settings/vacation": 2,
        "google:gmail:GET users/{userId}/settings/doesnotexist": 1,
        "google:gmail:GET users/{userId}/nothinghere": 1,
        "google:path:/drive/v3": 1,
        "google:path:/o/oauth2": 1,
        "google:host:people.googleapis.com": 1,
    }
    # The access log reads an answer that is not JSON as neither ok nor an error, and keeps its gap.
    vacation_line = _control(fakes, "log")[0]
    assert (vacation_line["status"], vacation_line["ok"], vacation_line["error"], vacation_line["result_count"]) == (
        404,
        None,
        None,
        None,
    )
    assert vacation_line["unsupported"] == "google:gmail:GET users/{userId}/settings/vacation"


def test_userinfo_answers_each_tokens_own_account_as_openid_connect_does(
    fakes: RunningFakes,
) -> None:
    users = _control(fakes, "state")["google"]["collections"]["google.users"]["items"]
    account_ids = {user["email"]: user["uid"] for user in users}

    morgan = _userinfo(fakes)
    thomas = _userinfo(fakes, token=_THOMAS_GOOGLE_TOKEN)
    anonymous = _userinfo(fakes, token=None)
    unknown = _userinfo(fakes, token="ya29.other")
    # Only openidconnect's v1 path is served: Google's other userinfo paths are not.
    for path in ("oauth2/v2/userinfo", "oauth2/v3/userinfo"):
        other = httpx.get(
            "{}/{}".format(fakes.google_url, path),
            headers={"Authorization": "Bearer {}".format(_GOOGLE_TOKEN), "X-Forwarded-Host": "www.googleapis.com"},
        )
        assert other.status_code == 404, path

    assert (morgan.status_code, morgan.json()) == (
        200,
        {
            "sub": account_ids[_MORGAN],
            "name": "Morgan Reyes",
            "given_name": "Morgan",
            "family_name": "Reyes",
            "email": _MORGAN,
            "email_verified": True,
        },
    )
    assert len(account_ids[_MORGAN]) == 21 and account_ids[_MORGAN].isdigit()
    assert (thomas.status_code, thomas.json()["sub"], thomas.json()["email"]) == (
        200,
        account_ids["thomas.baird@redwood.example"],
        "thomas.baird@redwood.example",
    )
    assert (anonymous.status_code, anonymous.json()["error"]["errors"][0]["reason"]) == (401, "required")
    assert (unknown.status_code, unknown.json()["error"]["errors"][0]["reason"]) == (401, "authError")
    assert _control(fakes, "health")["unsupported_calls"] == {
        "google:path:/oauth2/v2": 1,
        "google:path:/oauth2/v3": 1,
    }


def test_userinfo_names_an_account_only_when_its_seed_does(
    unnamed_google_fakes: RunningFakes,
) -> None:
    answer = _userinfo(unnamed_google_fakes)

    assert answer.status_code == 200, answer.text
    assert set(answer.json()) == {"sub", "email", "email_verified"}


def test_a_google_token_reads_only_its_own_mailbox(fakes: RunningFakes) -> None:
    thomas_keys = _message_keys(fakes, "thomas.baird@redwood.example")
    morgan_keys = _message_keys(fakes)

    thomas = _gmail(fakes, "me/profile", token=_THOMAS_GOOGLE_TOKEN)
    thomas_listed = _gmail(fakes, "me/messages", token=_THOMAS_GOOGLE_TOKEN)
    across = _gmail_get(fakes, "me/messages/{}".format(thomas_keys["thomas-invoice-copy"]))
    delegated = _gmail_get(fakes, "thomas.baird@redwood.example/messages")
    anonymous = _gmail_get(fakes, "me/profile", token=None)
    unknown = _gmail_get(fakes, "me/profile", token="ya29.other")

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


def _vendors_id(services: RunningFakes) -> str:
    return next(label["id"] for label in _gmail(services, "me/labels")["labels"] if label["name"] == "Vendors")


def test_a_gmail_update_lands_in_its_thread_as_newer_history(fakes: RunningFakes) -> None:
    before = _gmail(fakes, "me/profile")

    added = _add(
        fakes, "google", fixture_text("update", "google")
    )
    after = _gmail(fakes, "me/profile")
    keys = _message_keys(fakes)
    history = _gmail(fakes, "me/history", startHistoryId=before["historyId"])

    assert (added.status_code, added.json()["added"]) == (
        200,
        {
            "google_users": 1,
            "gmail_labels": 1,
            "gmail_messages": 1,
            "gmail_label_changes": 1,
            "calendar_calendars": 1,
            "calendar_events": 1,
        },
    )
    assert (after["messagesTotal"], after["threadsTotal"]) == (5, 2)
    assert int(after["historyId"]) > int(before["historyId"])
    # The new message, then the label change of the seeded one, which the update lists after it.
    finance = next(
        label["id"] for label in _gmail(fakes, "me/labels")["labels"] if label["name"] == "Finance"
    )
    added_entry, changed_entry = history["history"]
    assert [entry["message"]["id"] for entry in added_entry["messagesAdded"]] == [keys["corrected-invoice"]]
    assert int(changed_entry["id"]) > int(added_entry["id"]) == int(after["historyId"]) - 1
    assert [(entry["message"]["id"], entry["labelIds"]) for entry in changed_entry["labelsAdded"]] == [
        (keys["invoice-followup"], [finance])
    ]
    assert [(entry["message"]["id"], sorted(entry["labelIds"])) for entry in changed_entry["labelsRemoved"]] == [
        (keys["invoice-followup"], ["INBOX", "UNREAD"])
    ]
    followup = _gmail(fakes, "me/messages/{}".format(keys["invoice-followup"]), format="minimal")
    assert sorted(followup["labelIds"]) == sorted(["STARRED", finance, _vendors_id(fakes)])
    assert followup["historyId"] == changed_entry["id"] == after["historyId"]
    thread = _gmail(fakes, "me/threads/{}".format(keys["invoice-question"]), format="minimal")
    assert thread["messages"][-1]["id"] == keys["corrected-invoice"]
    assert thread["messages"][-1]["internalDate"] == str(_shifted_ms("2026-03-20T15:30:00-07:00"))
    assert _listed_ids(fakes, q="label:Finance") == [keys["corrected-invoice"], keys["invoice-followup"]]
    assert keys["invoice-followup"] not in _listed_ids(fakes, q="is:unread")
    users = _control(fakes, "state")["google"]["collections"]["google.users"]["items"]
    assert "olivia.chen@redwood.example" in {user["email"] for user in users}


def test_a_burst_past_gmails_per_user_quota_is_refused_as_gmail_refuses_it(
    fakes: RunningFakes,
) -> None:
    message_id = _message_keys(fakes)["invoice-question"]
    # A full get costs 60 of a user's 6,000 units a minute: 100 fit, and the 101st does not.
    with httpx.Client(headers={"Authorization": "Bearer {}".format(_GOOGLE_TOKEN)}) as client:
        burst = [
            client.get("{}/gmail/v1/users/me/messages/{}".format(fakes.google_url, message_id))
            for _ in range(101)
        ]
    # The quota is each user's own, and a cheaper call does not fit either once it is spent.
    cheap = _gmail_get(fakes, "me/profile")
    thomas = _gmail_get(fakes, "me/profile", token=_THOMAS_GOOGLE_TOKEN)

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
    assert _control(fakes, "health")["rate_limited"] == {"google:gmail_per_user": 2}


_THOMAS = "thomas.baird@redwood.example"


def _calendar(services: RunningFakes, path: str, token: str = _GOOGLE_TOKEN, **params: str) -> Any:
    answer = _calendar_get(services, path, token=token, **params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _event_ids(services: RunningFakes) -> dict[str, str]:
    """Each seeded event's id by its key, as the state dump holds them."""
    items = _control(services, "state")["google"]["collections"]["google.calendar_events"]["items"]
    return {item["seed_key"]: item["google_id"] for item in items if item["seed_key"]}


def _listed_summaries(services: RunningFakes, token: str = _GOOGLE_TOKEN, **params: str) -> list[str]:
    return [
        event["summary"] for event in _calendar(services, "calendars/primary/events", token=token, **params)["items"]
    ]


def _shifted_date_time(stamp: str) -> str:
    """A seed's instant as Calendar serves an event's dateTime under the test's shift: in the offset
    it was written in, to the second."""
    moved = datetime.fromisoformat(stamp) + timedelta(seconds=TEST_SHIFT_SECONDS)
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
    fakes: RunningFakes,
) -> None:
    listed = _calendar(fakes, "users/me/calendarList")
    by_primary = _calendar(fakes, "users/me/calendarList/primary")
    by_address = _calendar(fakes, "users/me/calendarList/{}".format(_MORGAN))
    calendar = _calendar(fakes, "calendars/primary")
    thomas = _calendar(fakes, "calendars/primary", token=_THOMAS_GOOGLE_TOKEN)

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


def test_calendar_on_a_google_host_other_than_www_is_a_counted_path_no_api_takes(
    fakes: RunningFakes,
) -> None:
    on_gmail_host = httpx.get(
        "{}/calendar/v3/calendars/primary".format(fakes.google_url),
        headers={"Authorization": "Bearer {}".format(_GOOGLE_TOKEN), "X-Forwarded-Host": "gmail.googleapis.com"},
    )

    assert on_gmail_host.status_code == 404
    assert on_gmail_host.headers["content-type"] == "text/html; charset=UTF-8"
    assert "<code>/calendar/v3/calendars/primary</code> was not found on this server." in on_gmail_host.text
    assert _control(fakes, "health")["unsupported_calls"] == {"google:path:/calendar/v3": 1}


def test_calendar_settings_are_calendars_defaults_in_the_primary_calendars_zone(
    fakes: RunningFakes,
) -> None:
    listed = _calendar(fakes, "users/me/settings")
    timezone = _calendar(fakes, "users/me/settings/timezone")
    week_start = _calendar(fakes, "users/me/settings/weekStart")
    thomas = _calendar(fakes, "users/me/settings/timezone", token=_THOMAS_GOOGLE_TOKEN)
    unknown = _calendar_get(fakes, "users/me/settings/noSuchSetting")

    assert listed["kind"] == "calendar#settings" and "nextSyncToken" not in listed
    assert {item["id"]: item["value"] for item in listed["items"]} == {
        "dateFieldOrder": "MDY",
        "format24HourTime": "false",
        "hideWeekends": "false",
        "locale": "en",
        "showDeclinedEvents": "true",
        "timezone": "Etc/GMT+7",
        "weekStart": "0",
    }
    assert {item["kind"] for item in listed["items"]} == {"calendar#setting"}
    assert timezone == next(item for item in listed["items"] if item["id"] == "timezone")
    assert set(timezone) == {"kind", "etag", "id", "value"}
    assert (week_start["id"], week_start["value"]) == ("weekStart", "0")
    assert thomas["value"] == "UTC"
    assert (unknown.status_code, unknown.json()) == (404, _calendar_error(404, "Not Found", "notFound"))
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_a_seeded_calendar_event_is_served_as_calendar_serves_one(fakes: RunningFakes) -> None:
    ids = _event_ids(fakes)
    event = _calendar(fakes, "calendars/primary/events/{}".format(ids["vendor-sync"]))
    offsite = _calendar(fakes, "calendars/{}/events/{}".format(_MORGAN, ids["offsite"]))
    listed = {item["id"]: item for item in _calendar(fakes, "calendars/primary/events")["items"]}

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


def test_every_calendar_date_is_the_seeds_or_derived_from_it(fakes: RunningFakes) -> None:
    morgan = _calendar(fakes, "calendars/primary/events")
    thomas = _calendar(fakes, "calendars/primary/events", token=_THOMAS_GOOGLE_TOKEN)
    calendars = _calendar(fakes, "users/me/calendarList")

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


def test_calendar_lists_events_by_time_bounds_order_and_pages(fakes: RunningFakes) -> None:
    shifted = _shifted_iso
    everything = _calendar(fakes, "calendars/primary/events")
    # timeMin bounds an event's end and timeMax its start, both exclusively.
    after_sync = _listed_summaries(fakes, timeMin=shifted("2026-03-17T17:30:00.000Z"))
    before_review = _listed_summaries(fakes, timeMax=shifted("2026-03-24T21:00:00.000Z"))
    within = _listed_summaries(
        fakes, timeMin=shifted("2026-03-20T00:00:00.000Z"), timeMax=shifted("2026-03-27T00:00:00.000Z")
    )
    by_start = _listed_summaries(fakes, orderBy="startTime", singleEvents="true")
    by_update = _listed_summaries(fakes, orderBy="updated")
    unordered = _calendar_get(fakes, "calendars/primary/events", orderBy="startTime")
    empty_range = _calendar_get(
        fakes,
        "calendars/primary/events",
        timeMin=shifted("2026-03-20T00:00:00.000Z"),
        timeMax=shifted("2026-03-20T00:00:00.000Z"),
    )
    no_offset = _calendar_get(fakes, "calendars/primary/events", timeMin="2036-03-20T00:00:00")

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
        _calendar(fakes, "calendars/primary/events", timeMax=shifted("2026-01-01T00:00:00.000Z"))["items"]
        == []
    )


def test_calendar_pages_250_events_at_a_time_unless_asked_otherwise(
    fakes: RunningFakes,
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
    assert _add(fakes, "google", json.dumps({"events": many})).status_code == 200

    whole = _calendar(fakes, "calendars/primary/events")
    first = _calendar(fakes, "calendars/primary/events", maxResults="5")
    second = _calendar(fakes, "calendars/primary/events", maxResults="5", pageToken=first["nextPageToken"])
    last = _calendar(fakes, "calendars/primary/events", maxResults="5", pageToken=second["nextPageToken"])
    pages = [[event["summary"] for event in page["items"]] for page in (first, second, last)]
    malformed = _calendar_get(
        fakes, "calendars/primary/events", pageToken="CiAKGjBpNDd2Nmp2Zml2cXRwYjBpOXA"
    )
    none_asked = _calendar_get(fakes, "calendars/primary/events", maxResults="0")

    # The package pages ten at a time; Calendar 250.
    assert len(whole["items"]) == 15 and "nextPageToken" not in whole
    assert [len(page) for page in pages] == [5, 5, 5]
    assert "nextPageToken" not in last
    assert [summary for page in pages for summary in page] == [event["summary"] for event in whole["items"]]
    assert (malformed.status_code, malformed.json()["error"]["errors"][0]["location"]) == (400, "pageToken")
    assert (none_asked.status_code, none_asked.json()["error"]["errors"][0]["location"]) == (400, "maxResults")


def test_an_inserted_calendar_event_is_listed_and_a_deleted_one_is_cancelled(
    fakes: RunningFakes,
) -> None:
    headers = {"Authorization": "Bearer {}".format(_GOOGLE_TOKEN), "X-Forwarded-Host": "www.googleapis.com"}
    events_url = "{}/calendar/v3/calendars/primary/events".format(fakes.google_url)
    # A day between the finance review and the offsite, as the trial sees them; Los Angeles keeps
    # daylight time then, as the calendar's own -07:00 does.
    day = _shifted_date("2026-03-25")
    inserted = httpx.post(
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
    got = _calendar(fakes, "calendars/primary/events/{}".format(event["id"]))
    listed = _listed_summaries(fakes, orderBy="startTime", singleEvents="true")
    sync_id = _event_ids(fakes)["vendor-sync"]
    deleted = httpx.delete("{}/{}".format(events_url, sync_id), headers=headers)
    after_delete = _listed_summaries(fakes)
    with_deleted = _calendar(fakes, "calendars/primary/events", showDeleted="true")["items"]
    cancelled = _calendar(fakes, "calendars/primary/events/{}".format(sync_id))
    deleted_again = httpx.delete("{}/{}".format(events_url, sync_id), headers=headers)
    no_end = httpx.post(events_url, headers=headers, json={"summary": "x", "start": {"date": day}})
    one_day = {"start": {"date": day}, "end": {"date": _shifted_date("2026-03-26")}}
    tentative = httpx.post(events_url, headers=headers, json={"summary": "Maybe", "status": "tentative", **one_day})
    born_cancelled = httpx.post(
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
    assert _control(fakes, "health")["unsupported_calls"] == {
        "google:calendar:body:recurrence": 1,
        "google:calendar:body:attendees.optional": 1,
        "google:calendar:body:attendees.responseStatus": 1,
        "google:calendar:body:status": 1,
    }


def test_another_accounts_calendar_is_not_found(fakes: RunningFakes) -> None:
    """Calendar answers a calendar the caller has no access to as one that does not exist, with a 404
    rather than a 403, so the fake does the same for another seeded account's calendar and for an
    event on it."""
    thomas_event = _event_ids(fakes)["thomas-planning"]
    answers = [
        _calendar_get(fakes, "calendars/{}/events".format(_THOMAS)),
        _calendar_get(fakes, "calendars/{}".format(_THOMAS)),
        _calendar_get(fakes, "users/me/calendarList/{}".format(_THOMAS)),
        _calendar_get(fakes, "calendars/primary/events/{}".format(thomas_event)),
        _calendar_get(fakes, "calendars/{}/events/{}".format(_THOMAS, thomas_event)),
    ]
    anonymous = _calendar_get(fakes, "calendars/primary", token=None)

    for answer in answers:
        assert (answer.status_code, answer.json()) == (404, _calendar_error(404, "Not Found", "notFound"))
    assert (anonymous.status_code, anonymous.json()["error"]["errors"][0]["reason"]) == (401, "required")
    assert _control(fakes, "health")["unsupported_calls"] == {}


def test_a_calendar_method_or_parameter_the_fake_lacks_is_googles_not_found_or_served_and_counted(
    fakes: RunningFakes,
) -> None:
    headers = {"Authorization": "Bearer {}".format(_GOOGLE_TOKEN), "X-Forwarded-Host": "www.googleapis.com"}
    base = "{}/calendar/v3".format(fakes.google_url)
    sync_id = _event_ids(fakes)["vendor-sync"]
    lacking = [
        httpx.patch("{}/calendars/primary/events/{}".format(base, sync_id), headers=headers, json={"summary": "x"}),
        httpx.post("{}/calendars/primary/events/quickAdd".format(base), headers=headers, params={"text": "x"}),
        httpx.get("{}/calendars/primary/events/{}/instances".format(base, sync_id), headers=headers),
        httpx.post("{}/freeBusy".format(base), headers=headers, json={}),
        httpx.get("{}/colors".format(base), headers=headers),
        httpx.get("{}/calendars/primary/acl".format(base), headers=headers),
    ]
    with_parameters = [
        _calendar_get(fakes, "calendars/primary/events", syncToken="CPDAlvWDx70CEPDAlvWDx70CGAU="),
        _calendar_get(fakes, "calendars/primary/events", iCalUID="x@google.com"),
        _calendar_get(fakes, "calendars/primary/events", q="invoice", prettyPrint="false"),
        _calendar_get(fakes, "calendars/primary/events", fields="items(summary)"),
    ]

    for answer in lacking:
        assert answer.status_code == 404
        assert answer.headers["content-type"] == "text/html; charset=UTF-8"
        assert "<code>{}</code> was not found on this server.".format(answer.request.url.path) in answer.text
    assert [answer.status_code for answer in with_parameters] == [200] * 4
    # A search is answered by the words in an event's text, and not counted.
    assert [event["summary"] for event in with_parameters[2].json()["items"]] == ["Northwind invoice sync"]
    assert _control(fakes, "health")["unsupported_calls"] == {
        "google:calendar:PATCH calendars/{calendarId}/events/{eventId}": 1,
        "google:calendar:POST calendars/{calendarId}/events/quickAdd": 1,
        "google:calendar:GET calendars/{calendarId}/events/{eventId}/instances": 1,
        "google:calendar:POST freeBusy": 1,
        "google:calendar:GET colors": 1,
        "google:calendar:GET calendars/{calendarId}/acl": 1,
        "google:calendar:syncToken": 1,
        "google:calendar:iCalUID": 1,
        "google:calendar:fields": 1,
    }


def test_a_calendar_update_adds_its_events_with_the_seeds_shift(fakes: RunningFakes) -> None:
    added = _add(
        fakes, "google", fixture_text("update", "google")
    )
    calendars = _control(fakes, "state")["google"]["collections"]["google.calendars"]["items"]
    call = next(
        event
        for event in _calendar(fakes, "calendars/primary/events")["items"]
        if event["summary"] == "Corrected invoice call"
    )

    assert added.status_code == 200, added.text
    assert added.json()["added"]["calendar_events"] == 1
    assert call["start"] == {"dateTime": _shifted_date_time("2026-03-23T09:00:00-07:00"), "timeZone": "Etc/GMT+7"}
    assert call["id"] == _event_ids(fakes)["corrected-invoice-call"]
    assert [a["email"] for a in call["attendees"]] == [_MORGAN, "olivia.chen@redwood.example"]
    # The update's new account has its primary calendar too, in UTC with no event of its own.
    assert {calendar["google_id"]: calendar["time_zone"] for calendar in calendars}[
        "olivia.chen@redwood.example"
    ] == "UTC"


def test_a_burst_past_calendars_per_user_quota_is_refused_as_calendar_refuses_it(
    fakes: RunningFakes,
) -> None:
    # Every request costs one of a user's 600 a minute: 600 fit, and the 601st does not.
    with httpx.Client(
        headers={"Authorization": "Bearer {}".format(_GOOGLE_TOKEN), "X-Forwarded-Host": "www.googleapis.com"}
    ) as client:
        burst = [
            client.get("{}/calendar/v3/calendars/primary".format(fakes.google_url)) for _ in range(601)
        ]
    # The quota is each user's own and Calendar's own: Thomas, and Morgan's Gmail, are still served.
    thomas = _calendar_get(fakes, "calendars/primary", token=_THOMAS_GOOGLE_TOKEN)
    gmail = _gmail_get(fakes, "me/profile")

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
    assert _control(fakes, "health")["rate_limited"] == {"google:calendar_per_user": 1}
