# fake-services

Fake Slack, Linear and Google (Gmail and Calendar) APIs, seeded from JSON files, for testing a
program that reads the real ones. The program under test calls `https://slack.com/api/...`,
`POST https://api.linear.app/graphql`, `https://gmail.googleapis.com/gmail/v1/...` and
`https://www.googleapis.com/calendar/v3/...` exactly as it would against the real services; only
where the request lands differs.

The fakes are one Node process (the *launcher*) built on the `@emulators/core`, `@emulators/slack`,
`@emulators/linear` and `@emulators/google` packages
([vercel-labs/emulate](https://github.com/vercel-labs/emulate), Apache-2.0). On top of the
emulators the launcher:

- seeds each service from a JSON file whose ids, identifiers and timestamps it keeps, so the data
  reads the same in every run, and loads none of the emulators' own default workspace;
- validates every Linear query against Linear's public GraphQL schema, so a query passes or fails as
  it would against the real API and introspection shows Linear's own types;
- keeps the real services' rate limits, so a caller that outruns them meets the refusals it would
  meet there, and can misbehave on a fixed schedule of faults the way a real service does on a bad
  day;
- counts every call the real service has and the fake lacks, instead of hiding the gap;
- logs every request, and serves control endpoints for adding records, reading the state, reading
  what a phase changed and marking phases, on a port of their own.

This repository is a fork of the fakes the Imbue Studio evals use (`apps/minds_evals` in
`mngr-internal`), with their history; it is meant for the tests of
[datalib](https://github.com/imbue-ai/datalib) and anything else that reads these services. The
complete reference for the launcher's interface is the header comment of
[`launcher/launcher.mjs`](launcher/launcher.mjs), and for each service the header of its module;
the tests under [`tests/`](tests/) show every behaviour in use.

## Running the launcher

The package is consumed straight from this repository, pinned to a commit:

```sh
npm install github:imbue-ai/fake-services#<commit sha>
```

The launcher is `launcher/launcher.mjs`, also installed as the `fake-services` binary. It reads its
configuration from the environment and serves until it is killed:

```sh
SEED_DIR=tests/fixtures/seed CONTROL_PORT=4400 SLACK_PORT=4401 LINEAR_PORT=4402 GOOGLE_PORT=4403 \
    ACCESS_LOG=/tmp/access_log.jsonl npx fake-services
```

| variable | meaning |
|---|---|
| `SEED_DIR` | A directory holding one `<service>.json` per service to serve (`slack.json`, `linear.json`, `google.json`). A service whose file is absent is not served; a directory with no service file is refused. |
| `CONTROL_PORT`, `SLACK_PORT`, `LINEAR_PORT`, `GOOGLE_PORT` | The loopback ports. `0` takes a free port. |
| `ACCESS_LOG` | Optional. A JSONL file the launcher appends one line per request to. |
| `START_MS` | Optional. The launcher's present, as epoch milliseconds, in place of the process's own clock. For a test that wants every served date known. |
| `RATE_LIMIT_WINDOW_SECONDS` | Optional. Replaces every rate limit bucket's window, for a test that waits a window out. |
| `RATE_LIMIT_BUCKET_LIMITS` | Optional. JSON `{"<service>:<bucket>": limit}` replacing named buckets' limits, for a test that fills one in a few requests. |

Once every port listens the launcher prints one JSON line to stdout naming the ports and what was
seeded, for example
`{"ready": true, "ports": {"control": 4400, "slack": 4401, "linear": 4402, "google": 4403}, ...}`.
Everything listens on `127.0.0.1` only. A seed that cannot be placed (a message in a channel nothing
names, an issue in a state its team does not have) exits the process with status 2 and a message on
stderr.

## Getting requests to the fakes

The program under test is pointed at the fakes by whatever it offers for that. A program whose HTTP
client is a curl it is told of, such as [latchkey](https://github.com/imbue-ai/latchkey)'s gateway
through `LATCHKEY_CURL`, can be given `curl_shim.sh`. The shim reads two files beside itself on every
request:

- `curl_router`: one line, the path of the curl the program would have run itself. With no such file,
  or one naming a program that cannot be run, the system `curl` runs.
- `curl_routes`: one faked host a line, as `<shell pattern> <port>`, for example `slack.com 4401`,
  `*.slack.com 4401`, `api.linear.app 4402`, `uploads.linear.app 4402`,
  `gmail.googleapis.com 4403`, `www.googleapis.com 4403`, `*.googleapis.com 4403`. With no such
  file every request is passed through untouched.

A request for a listed host is pointed at `http://127.0.0.1:<port>` with the same path and query,
and gains an `X-Forwarded-Host` header naming the host it was for. Every other argument and every
other URL is passed on untouched. A host is matched whatever its case, over http or https, with or
without its default port; the value of an option that takes one (a body, a header) is never read as a
URL.

The launcher reads `X-Forwarded-Host` to tell a service's API hosts from its other hosts: a request
for a Slack file download (`files.slack.com`) or a Linear upload (`uploads.linear.app`) reaches the
fake and is answered as not found, and counted as an unsupported call under `<service>:host:<host>`.
A request with no such header is taken to be for the service's first API host.

## Seed files

A seed is one JSON object. Beside the service's own content it states:

- `anchor` (required): a timestamp with a UTC offset, such as `"2026-03-21T00:00:00Z"`, for when the
  seed's data ends.
- `tokens` (required): a non-empty list of `{"token": ..., "user": ...}`. Only these tokens
  authenticate; each names the seeded user it belongs to. Every token across the served services
  must be distinct.
- `time_shift` (optional): `"none"` (the default) or `"to_trial_start"`. See [Time](#time).
- `rate_limits` (optional): `"real"` (the default) or `"none"`. See [Rate limits and
  faults](#rate-limits-and-faults).
- `faults` (optional): a schedule of misbehaviour; see [Rate limits and faults](#rate-limits-and-faults).

Every served service must state the same `anchor`, `time_shift`, `rate_limits` and `faults`: records
of one service refer to dates in another, so they keep one clock, and a caller is written for the
real limits or for none.

The service's own content is, for Slack, `team`, `users`, `channels`, `dms` and `messages`; for
Linear `organization`, `teams`, `users`, `labels`, `projects`, `cycles`, `issues` and `comments`; for
Google `users` (accounts, shared by every product), `labels`, `messages` and `label_changes` (Gmail)
and `events` (Calendar). The fixture seeds under [`tests/fixtures/seed/`](tests/fixtures/seed/) are
complete small examples of every service, and [`tests/fixtures/update/`](tests/fixtures/update/) an
update to each; the record shapes are what each service module's `add` reads
(`launcher/slack.mjs`, `launcher/linear.mjs`, `launcher/google/gmail.mjs`,
`launcher/google/calendar.mjs`). Timestamps everywhere are ISO 8601 with a UTC offset
(`2026-03-20T21:00:00.000Z`), and dates are `YYYY-MM-DD`; the launcher reads them with JavaScript's
`Date.parse`, which refuses forms without separators. A reference the fake cannot resolve (an issue
whose team, state, assignee, project, cycle or label nothing names, a message from a user nothing
names, a reply whose thread parent is not an earlier message) refuses the whole file.

### Updates

An update is a later file for a service that is already served, posted to
`POST /_control/records/<service>` on the control port as JSON. It carries the service's record
collections and nothing else, and is added to what the fake holds, with the same time shift as the
seed. An update lands whole or not at all: one with a record the fake cannot place is refused with a
400 naming the record, and the store is left as it was.

An update may follow a named *step*, `POST /_control/records/<service>?after=<step>`, and then may
write an instant as a **relative timestamp**, `<step>+HH:MM:SS` with an optional fraction: that long
after the last request any fake served while that step ran. So what an update adds reads as activity
between an earlier stretch of reading and a later one, whatever the shift; an update whose newest
instant is still in the future is refused with a 409 and how long to wait. A Linear issue entry whose
identifier the fake holds and whose `updated_at` is relative edits that issue rather than repeating
it.

## Time

Timestamps in the files are absolute. With `"time_shift": "none"`, the default, every one is served
as written: the data ends where it was authored to end, however long before the run that is, the
fake's present is the process's own clock (or `START_MS`), and a date written inside a message or a
description agrees with the timestamps around it.

With `"time_shift": "to_trial_start"` the launcher shifts every timestamp by its start minus the
seed's `anchor`, rounded down to whole days, so data authored to end at the anchor ends within the
day before the launcher's start whenever it runs, on the weekdays and at the times of day it was
written for, and an update's records, shifted by the same amount, land in the past and newer than
the seed's. Dates inside text are not moved, so they then disagree with the timestamps around them.
The setting's name is the one the Imbue Studio evals gave it, where the launcher's start is a
trial's start.

No record carries the process's clock. A date a record does not state is derived, once a seed's or
an update's records are all in, from the records around it (each service module's `settle` step),
and shifted with them; an update never moves a record's creation back. "Now" in a query (Linear's
`isActive` cycle, a date filter given as a duration such as `-P2W`, Gmail's `newer_than:`) is the
launcher's start, advanced by the wall clock since.

## Rate limits and faults

With `"rate_limits": "real"`, the default, each fake refuses a caller who outruns the limits the real
service documents, the way the real service refuses one: Slack per method and workspace by the
method's tier, Linear per user by requests and complexity points an hour (scored as the real API
charges them, with Linear's `X-Complexity` and `X-RateLimit-*` headers on every answer), Gmail per
user by quota units a minute, Calendar per user by requests a minute. A refused request draws on no
limit, is logged with the limit that refused it, and is counted under `rate_limited` by
`<service>:<bucket>`. `"rate_limits": "none"` serves every request unlimited and without the
headers. The limits, with the documentation and measurements they come from, are each module's
`limits` export.

A seed may carry a `faults` object, and then every fake misbehaves on a fixed schedule: after the
first `after_requests` requests, every `rate_limit_episode_every`th request opens an episode of
`rate_limit_episode_seconds` in which every request is refused as rate limited with `Retry-After`,
every `transient_error_every`th is answered with the service's own transient server error, and every
`unreadable_every`th with an HTML error page. A faulted request reaches no service and is marked
`fault` in the access log and counted under `faulted`. See the header of
[`launcher/faults.mjs`](launcher/faults.mjs).

## What the fakes serve

**Calls the fakes do not support are counted, not hidden.** A fake that answers "no such method" for
a Slack method, answers a public Linear field, argument or filter key empty, or answers a Google path
no API takes, has a gap where the real service has data. The launcher marks each such request in its
access log (`unsupported`, the first of the request's gaps) and keeps a count of every gap by what
was asked, once per request, under `unsupported_calls`: a Slack method (`slack:files.info`), a Linear
field, argument, filter key or write (`linear:field:User.timezone`,
`linear:argument:Query.issues.sort`, `linear:filter:IssueFilter.slaStatus`,
`linear:mutation:issueCreate`), a Google path or parameter, or one of a service's other hosts
(`slack:host:files.slack.com`). A Linear query the public schema refuses is the caller's mistake and
is not counted.

**No answer says what is serving it.** Error answers point at the real services' documentation, and
nothing a caller is sent names the emulators or this package.

## Control endpoints

On the control port only; a request for `/_control/...` on a service port is a 404 like any other
unknown path there.

| endpoint | answer |
|---|---|
| `GET /_control/health` | `{ok, shift_ms, start_at, last_request_at, step_ends, rate_limits, faults, faulted, services, counts, unsupported_calls, rate_limited, phase, phases}`: the record counts and requests per service, the totals by key, and per phase the counts of the requests made in it alone. |
| `GET /_control/state` | The stores of the served services, every token redacted, with the health's counts. |
| `GET /_control/changes` | What the running phase did to the stores: per service and collection the records added, changed and removed since the phase was marked, and the phase's requests other than GET by `<METHOD> <host><path>`. |
| `GET /_control/log` | The access log so far, as a JSON array. |
| `POST /_control/records/<service>[?after=<step>]` | Adds an [update](#updates). Answers `{ok: true, added, counts}`, a 400 `{ok: false, error, counts}`, or a 409 with `retry_after_seconds`. |
| `POST /_control/phase` | `{"name": <phase>}`: every request from now on is logged and counted in that phase (`agent` until one is marked), and the changes are counted from here. The name is a lowercase identifier. Answers `{ok, phase, previous}`. |

## The access log

One JSON line per request that reached a fake, in `ACCESS_LOG` and at `GET /_control/log`: when and
in which phase, the service, method and URL (with the host the request was for), the body's first
8 KiB, the seeded user the token belongs to and a digest telling two tokens apart (the token itself
never reaches the log, and every token the seeds name is masked in the URL and body), the caller's
`User-Agent` and its `X-Eval-Caller` header (which nothing adds on the way, so a requester that wants
its calls told apart sets it itself), the status, the fake's `ok` and `error`, the first unsupported
key, the rate limit or fault that answered it, the result count and the response size. Never a
response body.

## Updating Linear's schema

`linear_schema.graphql.gz` is `packages/sdk/src/schema.graphql` of
https://github.com/linear/linear at commit `87d638c0b151575505d36822839324d2287f2e06`, gzipped unchanged; `LINEAR_SCHEMA_LICENSE`
is that repository's MIT licence. To move to a newer schema:

```sh
gh api 'repos/linear/linear/contents/packages/sdk/src/schema.graphql?ref=<commit>' \
    -H 'Accept: application/vnd.github.raw' | gzip -9 > linear_schema.graphql.gz
```

then record the commit here and run the tests, which check the fields the fake serves are still the
schema's.

## Adding a service

A service is a module under `launcher/` exporting its name, its API hosts, how it is built from a
seed and the rate limits and faults of the real service (the interface is documented at the top of
`launcher/launcher.mjs`, `launcher/rate_limits.mjs` and `launcher/faults.mjs`), and a line naming it
in `launcher.mjs`'s `MODULES`. Its seed is `<name>.json` and its port `<NAME>_PORT`. A Google
product is a module under `launcher/google/` (see `launcher/google.mjs`).

## Developing

The launcher is Node (22 or newer). Its tests drive the real launcher over HTTP from pytest:

```sh
npm ci
uv run pytest
```

The packages are pinned by `package-lock.json`; `@emulators/*` is held at the newest release older
than two weeks when the lockfile was last written.
