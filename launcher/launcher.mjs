// External data services for a minds-evals trial: fake Slack and Linear, each served on a loopback port
// of its own by one Node process in the eval box. The workspace reaches them through the latchkey
// gateway, whose curl the driver points at these ports for the real hosts.
//
// A service is served when the seed directory holds its file (slack.json, linear.json). Each service
// is a module beside this file (slack.mjs, linear.mjs) that exports:
//   name              the service's name; its seed is <name>.json
//   host              the API host the fake answers for: a request for any other host of the
//                     service is answered as not found
//   create(seed, clock) -> {
//     name, host      the module's own
//     store          the @emulators/core Store its records live in; an update is applied inside a
//                     snapshot of it, so one that fails leaves the store as it was
//     tokenUsers      Map of each token the seed names to the user it belongs to
//     add(records)    places a seed's or an update's records, throws on one it cannot place, and
//                     returns what it added
//     route(req, url) answers a request for the API host
//     notFound()      what a request for another host is answered with
//     counts()        the record counts for health
//     outcome(json)   what the access log says of an answer: {ok, error, result_count}
//     unsupportedKeys(url, json, req)
//                     what a request asked for that the real service has and the fake lacks
//   }
// This file settles the one clock every service keeps, seeds and serves the services, logs every
// request (access_log.mjs) and answers the control endpoints.
//
// What a trial gets on top of the @emulators packages:
//   - a seed's records carry absolute timestamps. With `"time_shift": "none"` (the default) they are
//     served as written; with `"time_shift": "to_trial_start"` every one is shifted by
//     (start - anchor) on load, so the data ends at the trial's start whenever the trial runs
//   - only the seed's tokens authenticate, nothing is rate limited, and the stores start empty: the
//     emulators' own default workspace is never loaded
//   - an access log (JSONL, one line per request that reaches a fake), which marks every call the
//     fake does not support, and control endpoints on a port of their own
//
// env: SEED_DIR=<directory of <service>.json> CONTROL_PORT=<port> SLACK_PORT=<port> LINEAR_PORT=<port>
//      (0 takes a free port) [ACCESS_LOG=<jsonl path>] [START_MS=<epoch ms; the process's own clock
//      when unset>]
// stdout: one JSON line once every port listens, naming the ports and what was seeded.
// control (the control port only, never logged):
//   GET  /_control/health             {ok, shift_ms, services, counts, unsupported_calls}
//   GET  /_control/state              the stores of the served services, tokens redacted
//   POST /_control/records/<service>  add an update to a served service (its seed's record collections,
//                                     same time shift); all of it lands or none does
//   GET  /_control/log                the access log so far, as a JSON array
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { serve } from "@emulators/core";
import { createAccessLog } from "./access_log.mjs";
import * as linear from "./linear.mjs";
import * as slack from "./slack.mjs";

const MODULES = [slack, linear];

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

// One clock for every service: records of one refer to dates in another.
const timeShift = (seed) => seed.time_shift ?? "none";
const first = seeds[served[0]];
for (const name of served)
  if (seeds[name].anchor !== first.anchor || timeShift(seeds[name]) !== timeShift(first)) fail(`the ${name} seed's anchor and time_shift differ from the ${served[0]} seed's`);
if (!["to_trial_start", "none"].includes(timeShift(first))) fail(`time_shift ${JSON.stringify(first.time_shift)} is neither "to_trial_start" nor "none"`);
const anchorMs = Date.parse(first.anchor);
if (Number.isNaN(anchorMs)) fail(`the seed's anchor ${JSON.stringify(first.anchor)} is not a timestamp`);
const launchedAtMs = Date.now();
const startMs = Number(env.START_MS ?? launchedAtMs);
const shiftMs = timeShift(first) === "none" ? 0 : startMs - anchorMs;
const clock = {
  shiftMs,
  // The trial's present, which "now" means in a query: the start, advanced by the wall clock since.
  now: () => startMs + (Date.now() - launchedAtMs),
  iso: (v) => (v ? new Date(Date.parse(v) + shiftMs).toISOString() : v ?? null),
  date: (v) => (v ? new Date(Date.parse(`${v}T00:00:00Z`) + shiftMs).toISOString().slice(0, 10) : v ?? null),
  slackTs: (ts) => {
    if (!ts) return ts;
    const [sec, frac = "000000"] = String(ts).split(".");
    return `${Number(sec) + Math.round(shiftMs / 1000)}.${frac}`;
  },
};

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

const counts = () => ({
  ...Object.fromEntries(served.map((name) => [name, services[name].counts()])),
  requests: Object.fromEntries(served.map((name) => [name, log.requestCount(name)])),
});
async function control(req) {
  const url = new URL(req.url);
  if (url.pathname === "/_control/health") return Response.json({ ok: true, shift_ms: shiftMs, services: served, counts: counts(), unsupported_calls: log.unsupportedCalls });
  if (url.pathname === "/_control/log") return Response.json(log.lines);
  if (url.pathname === "/_control/state") {
    const redact = (snap) => JSON.parse(JSON.stringify(snap, (k, v) => (k === "token" || k === "client_secret" || k === "refresh_token" ? "[redacted]" : v)));
    return Response.json({
      dumped_at: new Date().toISOString(),
      shift_ms: shiftMs,
      counts: counts(),
      unsupported_calls: log.unsupportedCalls,
      ...Object.fromEntries(served.map((name) => [name, redact(services[name].store.snapshot())])),
    });
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
    // An update lands whole or not at all: a half-applied one would leave the trial on data nobody
    // authored. The snapshot holds the live records, which an update may write to in place, so it is
    // kept as text.
    const before = JSON.stringify(services[name].store.snapshot());
    try {
      return Response.json({ ok: true, added: services[name].add(update), counts: counts() });
    } catch (e) {
      services[name].store.restore(JSON.parse(before));
      return Response.json({ ok: false, error: e.message, counts: counts() }, { status: 400 });
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
for (const name of served) ports[name] = await listen(log.logged(services[name]), env[`${name.toUpperCase()}_PORT`]);
console.log(JSON.stringify({ ready: true, ports, shift_ms: shiftMs, seed_ms: Math.round(performance.now() - t0), seeded, counts: counts() }));
