// External data services for a minds-evals trial: fake Slack, Linear and Google, each served on a
// loopback port of its own by one Node process in the eval box. The workspace reaches them through
// the latchkey gateway, whose curl the driver points at these ports for the real hosts.
//
// A service is served when the seed directory holds its file (slack.json, linear.json, google.json).
// Each service is a module beside this file (slack.mjs, linear.mjs, google.mjs) that exports:
//   name              the service's name; its seed is <name>.json
//   hosts             the Set of API hosts the fake answers for: a request for any other host routed
//                     to the service is answered as not found
//   create(seed, clock) -> {
//     name, hosts     the module's own
//     store          the @emulators/core Store its records live in; an update is applied inside a
//                     snapshot of it, so one that fails leaves the store as it was
//     tokenUsers      Map of each token the seed names to the user it belongs to
//     add(records)    places a seed's or an update's records, throws on one it cannot place, dates
//                     every record from the seed's own dates (never the box's clock), and returns
//                     what it added
//     route(req, url) answers a request for one of its API hosts
//     notFound(url)   what a request for another host is answered with
//     counts()        the record counts for health
//     outcome(json)   what the access log says of an answer, given its JSON (null for one that is
//                     not JSON): {ok, error, result_count}
//     unsupportedKeys(url, json, req)
//                     what a request asked for that the real service has and the fake lacks
//   }
//   limits            the rate limits the real service enforces: its buckets, which of them a request
//                     draws on, and the service's own refusal and headers (see rate_limits.mjs)
// This file settles the one clock every service keeps, seeds and serves the services, rate-limits
// them (rate_limits.mjs), logs every request (access_log.mjs) and answers the control endpoints.
//
// What a trial gets on top of the @emulators packages:
//   - a seed's records carry absolute timestamps. With `"time_shift": "none"` (the default) they are
//     served as written; with `"time_shift": "to_trial_start"` every one is shifted by
//     (start - anchor) rounded down to whole days on load, so the data ends within the day before
//     the trial's start whenever the trial runs, on the weekdays and at the times of day it was
//     written for
//   - an update may write an instant as a relative timestamp, `+HH:MM:SS` with an optional fraction
//     of up to six digits: that long after the update's base, the moment the latest request any fake
//     had served before the update began (the start, when none had). So what an update adds reads as
//     activity between the earlier step's last look and the later step's first, whatever the shift.
//     `<step>+HH:MM:SS` names the step whose base it counts from, the update's own or an earlier one;
//     the bare form is shorthand for the update's own. An update whose newest instant is still in
//     the future is refused with how long to wait
//   - no record carries the box's clock: a date the seed does not state is derived from the seed's
//     own records (each module's settle step)
//   - only the seed's tokens authenticate, and the stores start empty: the emulators' own default
//     workspace is never loaded
//   - with `"rate_limits": "real"` (the default) each service refuses a caller who outruns the limits
//     the real service documents, as the real service refuses it, and adds the real service's rate
//     limit headers to its answers; with `"rate_limits": "none"` nothing is limited
//   - an access log (JSONL, one line per request that reaches a fake), which marks every call the
//     fake does not support, and control endpoints on a port of their own
//
// env: SEED_DIR=<directory of <service>.json> CONTROL_PORT=<port> SLACK_PORT=<port> LINEAR_PORT=<port>
//      GOOGLE_PORT=<port> (0 takes a free port) [ACCESS_LOG=<jsonl path>] [START_MS=<epoch ms; the
//      process's own clock when unset>] [RATE_LIMIT_WINDOW_SECONDS=<seconds every rate limit bucket's window lasts; each
//      bucket's own when unset; for a test that waits a window out>]
// stdout: one JSON line once every port listens, naming the ports and what was seeded.
// control (the control port only, never logged):
//   GET  /_control/health             {ok, shift_ms, start_at, last_request_at, bases, rate_limits,
//                                     services, counts, unsupported_calls, rate_limited, phase, phases};
//                                     bases holds {base, applied_at} by step; counts.requests,
//                                     unsupported_calls and rate_limited are the totals, rate_limited
//                                     counting refused requests by "<service>:<bucket>";
//                                     phases[<phase>] holds {requests, unsupported_calls,
//                                     rate_limited} of the requests made in that phase alone
//   GET  /_control/state              the stores of the served services, tokens redacted, with the
//                                     health's counts, rate_limited and phases
//   POST /_control/phase              {"name": <phase>}: every request from now on is logged and
//                                     counted in that phase ("agent" until one is marked); answers
//                                     {ok, phase, previous}
//   POST /_control/records/<service>?step=<name>
//                                     add an update to a served service (its seed's record collections,
//                                     same time shift; relative timestamps against the step's base, which
//                                     the step's first update fixes); all of it lands or none does; 409
//                                     with retry_after_seconds while its newest instant is in the future.
//                                     The step is "update" when unnamed
//   GET  /_control/log                the access log so far, as a JSON array
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { serve } from "@emulators/core";
import { createAccessLog } from "./access_log.mjs";
import * as google from "./google.mjs";
import * as linear from "./linear.mjs";
import { createRateLimits } from "./rate_limits.mjs";
import * as slack from "./slack.mjs";

const MODULES = [slack, linear, google];

const env = process.env;
function fail(message) {
  console.error(`external services launcher: ${message}`);
  process.exit(2);
}
for (const name of ["SEED_DIR", "CONTROL_PORT", ...MODULES.map((m) => `${m.name.toUpperCase()}_PORT`)]) if (!env[name]) fail(`${name} is not set`);
const seeds = {};
for (const { name } of MODULES) {
  const path = join(env.SEED_DIR, `${name}.json`);
  if (existsSync(path)) seeds[name] = JSON.parse(readFileSync(path, "utf-8"));
}
const served = Object.keys(seeds);
if (!served.length) fail(`${env.SEED_DIR} holds no service seed`);

// One clock for every service, since records of one refer to dates in another, and one rate limit
// setting, since a task is written for limits or for none.
const timeShift = (seed) => seed.time_shift ?? "none";
const rateLimitsOf = (seed) => seed.rate_limits ?? "real";
const first = seeds[served[0]];
for (const name of served)
  if (seeds[name].anchor !== first.anchor || timeShift(seeds[name]) !== timeShift(first) || rateLimitsOf(seeds[name]) !== rateLimitsOf(first))
    fail(`the ${name} seed's anchor, time_shift and rate_limits differ from the ${served[0]} seed's`);
if (!["to_trial_start", "none"].includes(timeShift(first))) fail(`time_shift ${JSON.stringify(first.time_shift)} is neither "to_trial_start" nor "none"`);
if (!["real", "none"].includes(rateLimitsOf(first))) fail(`rate_limits ${JSON.stringify(first.rate_limits)} is neither "real" nor "none"`);
const rateLimitWindowSeconds = env.RATE_LIMIT_WINDOW_SECONDS ? Number(env.RATE_LIMIT_WINDOW_SECONDS) : undefined;
if (Number.isNaN(rateLimitWindowSeconds) || rateLimitWindowSeconds <= 0) fail(`RATE_LIMIT_WINDOW_SECONDS ${JSON.stringify(env.RATE_LIMIT_WINDOW_SECONDS)} is not a positive number`);
const anchorMs = Date.parse(first.anchor);
if (Number.isNaN(anchorMs)) fail(`the seed's anchor ${JSON.stringify(first.anchor)} is not a timestamp`);
const launchedAtMs = Date.now();
const startMs = Number(env.START_MS ?? launchedAtMs);
const DAY_MS = 24 * 60 * 60 * 1000;
const shiftMs = timeShift(first) === "none" ? 0 : Math.floor((startMs - anchorMs) / DAY_MS) * DAY_MS;
// The process's clock as the trial's present: the start, advanced by the wall clock since.
const toPresent = (processMs) => startMs + (processMs - launchedAtMs);
// A relative timestamp: `+HH:MM:SS`, with an optional fraction of up to six digits, counted from the
// base of the update being applied; `<step>+HH:MM:SS` counts from the base of the update that step
// applied. Null for anything else, an absolute timestamp included.
const RELATIVE_RE = /^(?:([a-z0-9][a-z0-9-]*))?\+(\d{1,3}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$/;
export function parseRelative(value) {
  const m = typeof value === "string" ? value.match(RELATIVE_RE) : null;
  if (!m) return null;
  const [, step, h, min, s, frac = ""] = m;
  const fraction = frac.padEnd(6, "0");
  return { step: step ?? null, seconds: (Number(h) * 60 + Number(min)) * 60 + Number(s), fraction, text: value };
}
// Each update step's base, by step name, with when it was applied (both on the trial's present), and
// the step whose update is being applied now: a bare `+HH:MM:SS` counts from its base, as does one
// that names it, since its own entry is only made once its first file has landed.
const bases = {};
let currentStep = null;
let currentBaseMs = null;
function baseMsOf(rel) {
  if (rel.step && rel.step !== currentStep) {
    if (!bases[rel.step]) throw new Error(`${rel.text} counts from step ${rel.step}, whose update has not been applied`);
    return bases[rel.step].baseMs;
  }
  if (currentBaseMs === null) throw new Error(`${rel.text} is a relative timestamp, which only an update may carry`);
  return currentBaseMs;
}
const relativeMs = (rel) => baseMsOf(rel) + rel.seconds * 1000 + Math.floor(Number(rel.fraction) / 1000);
const clock = {
  shiftMs,
  // The seed's anchor, shifted: the date of last resort for a record nothing else in a seed dates.
  anchor: new Date(anchorMs + shiftMs).toISOString(),
  now: () => toPresent(Date.now()),
  iso: (v) => {
    if (!v) return v ?? null;
    const rel = parseRelative(v);
    return new Date(rel ? relativeMs(rel) : Date.parse(v) + shiftMs).toISOString();
  },
  date: (v) => {
    if (!v) return v ?? null;
    const rel = parseRelative(v);
    return new Date(rel ? relativeMs(rel) : Date.parse(`${v}T00:00:00Z`) + shiftMs).toISOString().slice(0, 10);
  },
  slackTs: (ts) => {
    if (!ts) return ts;
    const rel = parseRelative(ts);
    // The fraction of a relative ts is kept as written: it is the per-channel sequence counter the
    // seed's own timestamps use, not a time.
    if (rel) return `${Math.floor(baseMsOf(rel) / 1000) + rel.seconds}.${rel.fraction}`;
    const [sec, frac = "000000"] = String(ts).split(".");
    return `${Number(sec) + Math.round(shiftMs / 1000)}.${frac}`;
  },
};
// Every relative timestamp in a document, wherever it sits.
function relativeStampsIn(value, out = []) {
  if (typeof value === "string") {
    const rel = parseRelative(value);
    if (rel) out.push(rel);
  } else if (Array.isArray(value)) for (const v of value) relativeStampsIn(v, out);
  else if (value && typeof value === "object") for (const v of Object.values(value)) relativeStampsIn(v, out);
  return out;
}

const t0 = performance.now();
const services = {};
const seeded = {};
for (const module of MODULES) {
  if (!seeds[module.name]) continue;
  const service = module.create(seeds[module.name], clock);
  services[module.name] = service;
  Object.assign(seeded, service.add(seeds[module.name]));
}
const log = createAccessLog(env.ACCESS_LOG, Object.values(services));
const limiter = createRateLimits({ enabled: rateLimitsOf(first) === "real", now: clock.now, windowSeconds: rateLimitWindowSeconds });
const limitedServices = Object.fromEntries(MODULES.filter((m) => services[m.name]).map((m) => [m.name, limiter.guard(services[m.name], m.limits)]));

const counts = () => ({
  ...Object.fromEntries(served.map((name) => [name, services[name].counts()])),
  requests: Object.fromEntries(served.map((name) => [name, log.requestCount(name)])),
});
const iso = (ms) => new Date(ms).toISOString();
const lastRequestMs = () => (log.lastBeganMs() === null ? null : toPresent(log.lastBeganMs()));
const timing = () => ({
  start_at: iso(startMs),
  last_request_at: lastRequestMs() === null ? null : iso(lastRequestMs()),
  bases: Object.fromEntries(Object.entries(bases).map(([step, b]) => [step, { base: iso(b.baseMs), applied_at: iso(b.appliedAtMs) }])),
});
async function control(req) {
  const url = new URL(req.url);
  if (url.pathname === "/_control/health")
    return Response.json({ ok: true, shift_ms: shiftMs, ...timing(), rate_limits: rateLimitsOf(first), services: served, counts: counts(), unsupported_calls: log.unsupportedCalls, rate_limited: limiter.rateLimited, phase: log.phase(), phases: log.phases });
  if (url.pathname === "/_control/log") return Response.json(log.lines);
  if (url.pathname === "/_control/state") {
    const redact = (snap) => JSON.parse(JSON.stringify(snap, (k, v) => (k === "token" || k === "client_secret" || k === "refresh_token" ? "[redacted]" : v)));
    return Response.json({
      dumped_at: new Date().toISOString(),
      shift_ms: shiftMs,
      ...timing(),
      rate_limits: rateLimitsOf(first),
      counts: counts(),
      unsupported_calls: log.unsupportedCalls,
      rate_limited: limiter.rateLimited,
      phase: log.phase(),
      phases: log.phases,
      ...Object.fromEntries(served.map((name) => [name, redact(services[name].store.snapshot())])),
    });
  }
  if (url.pathname === "/_control/phase" && req.method === "POST") {
    let name;
    try {
      ({ name } = await req.json());
    } catch {
      return Response.json({ ok: false, error: "the phase mark is not JSON" }, { status: 400 });
    }
    // A phase names a key of the health's phases, so it is kept to a plain identifier.
    if (typeof name !== "string" || !/^[a-z][a-z0-9_]*$/.test(name)) return Response.json({ ok: false, error: `the phase name ${JSON.stringify(name)} is not a lowercase identifier` }, { status: 400 });
    return Response.json({ ok: true, phase: name, previous: log.setPhase(name) });
  }
  const records = url.pathname.match(/^\/_control\/records\/([a-z]+)$/);
  if (records && req.method === "POST") {
    const name = records[1];
    if (!served.includes(name)) return Response.json({ ok: false, error: `the ${name} service was not seeded, so it takes no update` }, { status: 400 });
    let update;
    try {
      update = await req.json();
    } catch {
      return Response.json({ ok: false, error: "the update is not JSON" }, { status: 400 });
    }
    const step = url.searchParams.get("step") ?? "update";
    if (!/^[a-z0-9][a-z0-9-]*$/.test(step)) return Response.json({ ok: false, error: `the step name ${JSON.stringify(step)} is not a lowercase identifier` }, { status: 400 });
    // The step's base is fixed by its first update: the latest request any fake had served by then,
    // else the start. A later file of the same step counts from the same base, so a step's services
    // agree on when its activity happened.
    const baseMs = bases[step]?.baseMs ?? lastRequestMs() ?? startMs;
    const nowMs = clock.now();
    currentStep = step;
    currentBaseMs = baseMs;
    let newestMs;
    try {
      newestMs = Math.max(...relativeStampsIn(update).map(relativeMs));
    } catch (e) {
      currentStep = currentBaseMs = null;
      return Response.json({ ok: false, error: e.message, counts: counts() }, { status: 400 });
    }
    if (newestMs > nowMs) {
      currentStep = currentBaseMs = null;
      const wait = Math.ceil((newestMs - nowMs) / 1000);
      return Response.json({ ok: false, error: `the update's newest instant is ${wait} s in the future; apply it once the step's wait has passed`, retry_after_seconds: wait, base: iso(baseMs), now: iso(nowMs), newest: iso(newestMs), counts: counts() }, { status: 409 });
    }
    // An update lands whole or not at all: a half-applied one would leave the trial on data nobody
    // authored. The snapshot holds the live records, which an update may write to in place, so it is
    // kept as text.
    const before = JSON.stringify(services[name].store.snapshot());
    try {
      const added = services[name].add(update);
      bases[step] ??= { baseMs, appliedAtMs: nowMs };
      return Response.json({ ok: true, step, base: iso(baseMs), added, counts: counts() });
    } catch (e) {
      services[name].store.restore(JSON.parse(before));
      return Response.json({ ok: false, error: e.message, counts: counts() }, { status: 400 });
    } finally {
      currentStep = currentBaseMs = null;
    }
  }
  return Response.json({ error: "unknown control route" }, { status: 404 });
}

function listen(fetch, port) {
  const server = serve({ fetch, port: Number(port), hostname: "127.0.0.1" });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    if (server.listening) resolve(server.address().port);
    else server.once("listening", () => resolve(server.address().port));
  });
}
const ports = { control: await listen(control, env.CONTROL_PORT) };
for (const name of served) ports[name] = await listen(log.logged(limitedServices[name]), env[`${name.toUpperCase()}_PORT`]);
console.log(JSON.stringify({ ready: true, ports, shift_ms: shiftMs, seed_ms: Math.round(performance.now() - t0), seeded, counts: counts() }));
