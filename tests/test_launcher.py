"""The real launcher on small seeds. Skipped on a machine without node or the launcher's npm
packages (`just minds-evals-external-services-install`)."""

import json
from typing import Any

import httpx2
import pytest

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

_SLACK_TOKEN = "xoxb-fake-redwood-slack-token"
_LINEAR_TOKEN = "lin_api_fakeredwoodlineartoken"
_SLACK_HEADERS = {"Authorization": "Bearer {}".format(_SLACK_TOKEN)}


def _slack(services: RunningExternalServices, method: str, **params: str) -> dict[str, Any]:
    return httpx2.get("{}/api/{}".format(services.slack_url, method), params=params, headers=_SLACK_HEADERS).json()


def _linear(services: RunningExternalServices, query: str, token: str = _LINEAR_TOKEN) -> httpx2.Response:
    return httpx2.post(
        "{}/graphql".format(services.linear_url), json={"query": query}, headers={"Authorization": token}
    )


def _control(services: RunningExternalServices, route: str) -> Any:
    return httpx2.get("{}/_control/{}".format(services.control_url, route)).json()


def _add(services: RunningExternalServices, service_name: str, update_text: str) -> httpx2.Response:
    return httpx2.post("{}/_control/records/{}".format(services.control_url, service_name), content=update_text)


def _issue_titles(services: RunningExternalServices) -> dict[str, str]:
    answer = _linear(services, "{ issues { nodes { identifier title } } }")
    return {issue["identifier"]: issue["title"] for issue in answer.json()["data"]["issues"]["nodes"]}


def _channel_id(services: RunningExternalServices, name: str) -> str:
    channels = _slack(services, "conversations.list")["channels"]
    return next(channel["id"] for channel in channels if channel["name"] == name)


def _shifted(ts: str) -> str:
    seconds, fraction = ts.split(".")
    return "{}.{}".format(int(seconds) + EXTERNAL_SERVICES_TEST_SHIFT_SECONDS, fraction)


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
    assert {channel["name"]: channel["is_member"] for channel in channels} == {
        "announcements": True,
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


def test_every_seeded_user_is_a_member_of_every_public_channel(external_services: RunningExternalServices) -> None:
    channel_id = _channel_id(external_services, "eng-oncall")

    members = _slack(external_services, "conversations.members", channel=channel_id)["members"]

    user_ids = {member["id"] for member in _slack(external_services, "users.list")["members"]}
    assert len(user_ids) == 3
    assert set(members) == user_ids


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


def test_a_seed_that_asks_for_no_time_shift_is_served_as_written(
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
        "ts seq service method url body content_type token_user token_sha256_8 user_agent caller "
        "status ok error unsupported result_count response_bytes duration_ms"
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
    assert (linear_line["url"], linear_line["result_count"]) == ("https://api.linear.app/graphql", 1)
    assert json.loads(linear_line["body"]) == {"query": "{ teams { nodes { key } } }"}
    assert (refused_line["status"], refused_line["ok"], refused_line["token_user"]) == (401, False, None)
    assert _control(external_services, "log") == [slack_line, linear_line, refused_line]


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
