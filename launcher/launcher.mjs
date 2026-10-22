// External data services for a minds-evals trial: fake Slack and Linear, each served on a loopback port
// of its own by one Node process in the eval box. The workspace reaches them through the latchkey
// gateway, whose curl is a shim that points requests for the real hosts here (see curl_shim.sh).
//
// A service is served when the seed directory holds its file (slack.json, linear.json).
//
// Built on @emulators/{core,slack,linear}, with what a trial needs on top:
//   - a seed's records carry absolute timestamps. With `"time_shift": "to_trial_start"` (the default)
//     every one is shifted by (start - anchor) on load, so the data ends at the trial's start whenever
//     the trial runs; with `"time_shift": "none"` they are served as written
//   - Slack messages, threads and ids are inserted straight into the store, so they read the same in
//     every trial; conversations.history takes oldest/latest/inclusive; Slack methods answer GET; a
//     basic search.messages
//   - Linear issues keep the seed's identifiers and timestamps, with estimate, parent/children and
//     relations; queries validate against Linear's public schema (vendored beside this file) and are
//     served by the resolvers here (nested filters, date/number comparators, searchIssues); a public
//     field or argument the fake does not hold is answered empty and counted; mutations pass through
//     to the emulator
//   - only the seed's tokens authenticate, nothing is rate limited, and the stores start empty: the
//     emulators' own default workspace is never loaded
//   - a request for a service's other hosts (the shim names the host it was for) is answered as not
//     found
//   - an access log (JSONL, one line per request that reaches a fake), which marks every call the
//     fake does not support, and control endpoints on a port of their own
//
// env: SEED_DIR=<directory of <service>.json> CONTROL_PORT=<port> SLACK_PORT=<port> LINEAR_PORT=<port>
//      (0 takes a free port) [ACCESS_LOG=<jsonl path>] [START_MS=<epoch ms; the process's own clock
//      when unset>]
// stdout: one JSON line once every port listens, naming the ports and what was seeded.
// control (the control port only, never logged):
//   GET  /_control/health             {ok, shift_ms, services, counts, unsupported_calls}
//   GET  /_control/state              the stores of the served services and the launcher's side tables,
//                                     tokens redacted
//   POST /_control/records/<service>  add an update to a served service (its seed's record collections,
//                                     same time shift); all of it lands or none does
//   GET  /_control/log                the access log so far, as a JSON array
import crypto from "node:crypto";
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";
import { buildSchema, defaultFieldResolver, getNamedType, graphql, isAbstractType, isEnumType, isListType, isNonNullType, isScalarType, parse, GraphQLError } from "graphql";

const core = await import("@emulators/core");
const slack = await import("@emulators/slack");
const linear = await import("@emulators/linear");
const { Hono, Store, WebhookDispatcher, cors, serve, authMiddleware, createApiErrorHandler, createErrorHandler } = core;

const env = process.env;
function fail(message) {
  console.error(`external services launcher: ${message}`);
  process.exit(2);
}
for (const name of ["SEED_DIR", "CONTROL_PORT", "SLACK_PORT", "LINEAR_PORT"]) if (!env[name]) fail(`${name} is not set`);
const seeds = {};
for (const name of ["slack", "linear"]) {
  const path = join(env.SEED_DIR, `${name}.json`);
  if (existsSync(path)) seeds[name] = JSON.parse(readFileSync(path, "utf-8"));
}
const served = Object.keys(seeds);
if (!served.length) fail(`${env.SEED_DIR} holds no service seed`);
// One clock for every service: records of one refer to dates in another.
const timeShift = (seed) => seed.time_shift ?? "to_trial_start";
const clock = seeds[served[0]];
for (const name of served)
  if (seeds[name].anchor !== clock.anchor || timeShift(seeds[name]) !== timeShift(clock)) fail(`the ${name} seed's anchor and time_shift differ from the ${served[0]} seed's`);
if (!["to_trial_start", "none"].includes(timeShift(clock))) fail(`time_shift ${JSON.stringify(clock.time_shift)} is neither "to_trial_start" nor "none"`);
const anchorMs = Date.parse(clock.anchor);
if (Number.isNaN(anchorMs)) fail(`the seed's anchor ${JSON.stringify(clock.anchor)} is not a timestamp`);
const launchedAtMs = Date.now();
const startMs = Number(env.START_MS ?? launchedAtMs);
const shiftMs = timeShift(clock) === "none" ? 0 : startMs - anchorMs;
// The trial's present, which "now" means in a query: the start, advanced by the wall clock since.
const trialNowMs = () => startMs + (Date.now() - launchedAtMs);
const REAL_HOST = { slack: "slack.com", linear: "api.linear.app" };
const REAL = { slack: "https://slack.com", linear: "https://api.linear.app" };
// What an error answer points its reader at. Nothing a caller is sent names what is serving it.
const DOCS = { slack: "https://api.slack.com/methods", linear: "https://linear.app/developers/graphql" };
const slackDomain = seeds.slack?.team?.domain ?? "workspace";
const linearUrl = `https://linear.app/${seeds.linear?.organization?.url_key ?? "workspace"}`;
const accessLogPath = env.ACCESS_LOG;
if (accessLogPath) writeFileSync(accessLogPath, "");

const shiftIso = (v) => (v ? new Date(Date.parse(v) + shiftMs).toISOString() : v ?? null);
const shiftDate = (v) => (v ? new Date(Date.parse(`${v}T00:00:00Z`) + shiftMs).toISOString().slice(0, 10) : v ?? null);
function shiftSlackTs(ts) {
  if (!ts) return ts;
  const [sec, frac = "000000"] = String(ts).split(".");
  return `${Number(sec) + Math.round(shiftMs / 1000)}.${frac}`;
}

function build(plugin, baseUrl) {
  const app = new Hono();
  const store = new Store();
  const docs = DOCS[plugin.name];
  app.onError(createApiErrorHandler(docs));
  app.use("*", cors());
  app.use("*", createErrorHandler(docs));
  // No fallback user: a token the seed does not name is anonymous.
  app.use("*", authMiddleware(new Map(), undefined, undefined));
  plugin.register(app, store, new WebhookDispatcher(), baseUrl, new Map());
  app.notFound((c) => c.json({ message: "Not Found", documentation_url: docs }, 404));
  return { app, store };
}
const s = build(slack.slackPlugin, REAL.slack);
const l = build(linear.linearPlugin, linearUrl);
const ss = slack.getSlackStore(s.store);
const ls = linear.getLinearStore(l.store);
const linearRelations = []; // {id, type: blocks|related|duplicate, issue_id, related_issue_id}
const tokenUsers = { slack: new Map(), linear: new Map() }; // token -> display name of its user

const slackId = (prefix, name) => prefix + crypto.createHash("sha1").update(name).digest("hex").slice(0, 10).toUpperCase();
function addSlack(cfg) {
  if (!cfg) return {};
  // The emulator only renames a team it already has, and answers for a missing one under its own name.
  if (!ss.teams.all()[0]) ss.teams.insert({ team_id: "T" + slackId("", slackDomain), name: cfg.team?.name ?? slackDomain, domain: slackDomain });
  slack.seedFromConfig(s.store, REAL.slack, {
    team: cfg.team,
    users: (cfg.users ?? []).map((u) => ({ name: u.name, real_name: u.real_name, email: u.email ?? `${u.name}@${slackDomain}.example`, profile: { title: u.title ?? "" } })),
    channels: (cfg.channels ?? []).map((c) => ({ name: c.name, topic: c.topic, is_private: c.is_private })),
    tokens: cfg.tokens,
  });
  // Stable ids: an id derived from the name survives a re-seed and reads the same in every trial.
  for (const u of cfg.users ?? []) {
    const rec = ss.users.all().find((x) => x.name === u.name);
    const id = u.id ?? slackId("U", u.name);
    if (rec.user_id === id) continue;
    const old = rec.user_id;
    ss.users.update(rec.id, { user_id: id, is_bot: Boolean(u.is_bot) });
    for (const ch of ss.channels.all())
      if (ch.members.includes(old) || ch.creator === old)
        ss.channels.update(ch.id, { members: ch.members.map((m) => (m === old ? id : m)), creator: ch.creator === old ? id : ch.creator });
    for (const t of ss.tokens.all()) if (t.user_id === old) ss.tokens.update(t.id, { user_id: id });
  }
  for (const c of cfg.channels ?? []) {
    const rec = ss.channels.findOneBy("name", c.name);
    const id = c.id ?? slackId("C", c.name);
    if (rec.channel_id !== id) ss.channels.update(rec.id, { channel_id: id });
  }
  const members = ss.users.all().map((u) => u.user_id);
  for (const ch of ss.channels.all()) if (!ch.is_private) ss.channels.update(ch.id, { members, num_members: members.length });
  const chan = (ref) => (ss.channels.findOneBy("name", ref) ?? ss.channels.findOneBy("channel_id", ref))?.channel_id;
  const user = (ref) => (ss.users.all().find((x) => x.name === ref) ?? ss.users.findOneBy("user_id", ref))?.user_id;
  let n = 0;
  for (const m of cfg.messages ?? []) {
    const channel_id = chan(m.channel);
    const uid = user(m.user);
    if (!channel_id || !uid) throw new Error(`slack message refers to unknown channel/user: ${m.channel}/${m.user}`);
    const ts = shiftSlackTs(m.ts);
    const thread_ts = shiftSlackTs(m.thread_ts) ?? undefined;
    ss.messages.insert({ ts, channel_id, user: uid, text: m.text, type: "message", thread_ts, reply_count: 0, reply_users: [], reactions: [] });
    if (thread_ts && thread_ts !== ts) {
      const p = ss.messages.all().find((x) => x.channel_id === channel_id && x.ts === thread_ts);
      if (!p) throw new Error(`thread parent missing for ${m.channel}/${m.ts}`);
      ss.messages.update(p.id, {
        thread_ts: p.ts,
        reply_count: p.reply_count + 1,
        reply_users: p.reply_users.includes(uid) ? p.reply_users : [...p.reply_users, uid],
        latest_reply: ts,
      });
    }
    n++;
  }
  return { slack_messages: n };
}

function addLinear(cfg) {
  if (!cfg) return {};
  linear.seedFromConfig(l.store, linearUrl, {
    organization: cfg.organization,
    users: cfg.users,
    teams: cfg.teams,
    labels: cfg.labels,
    projects: cfg.projects,
    cycles: (cfg.cycles ?? []).map((c) => ({ ...c, starts_at: shiftIso(c.starts_at) ?? undefined, ends_at: shiftIso(c.ends_at) ?? undefined })),
    tokens: cfg.tokens,
  });
  const need = (what, ref, rec) => {
    if (ref && !rec) throw new Error(`linear ${what} not found: ${ref}`);
    return rec?.linear_id ?? null;
  };
  let issues = 0;
  for (const i of cfg.issues ?? []) {
    const team = linear.resolveTeam(l.store, i.team);
    if (!team) throw new Error(`linear team not found: ${i.team}`);
    if (ls.issues.findOneBy("identifier", i.identifier)) throw new Error(`duplicate issue ${i.identifier}`);
    const state = linear.resolveState(l.store, i.state, team.linear_id);
    if (!state) throw new Error(`linear state not found: ${i.state}`);
    const number = Number(i.identifier.split("-")[1]);
    const created = shiftIso(i.created_at);
    const updated = shiftIso(i.updated_at ?? i.created_at);
    const rec = ls.issues.insert({
      linear_id: i.id ?? crypto.createHash("sha1").update(i.identifier).digest("hex").replace(/^(.{8})(.{4})(.{4})(.{4})(.{12}).*/, "$1-$2-$3-$4-$5"),
      identifier: i.identifier,
      number,
      team_id: team.linear_id,
      title: i.title,
      description: i.description ?? null,
      priority: i.priority ?? 0,
      state_id: state.linear_id,
      assignee_id: need("user", i.assignee, linear.resolveUser(l.store, i.assignee)),
      creator_id: need("user", i.creator, linear.resolveUser(l.store, i.creator)),
      delegate_id: null,
      project_id: need("project", i.project, linear.resolveProject(l.store, i.project)),
      cycle_id: need("cycle", i.cycle, linear.resolveCycle(l.store, i.cycle, team.linear_id)),
      label_ids: (i.labels ?? []).map((x) => need("label", x, linear.resolveLabel(l.store, x, team.linear_id))),
      url: `${linearUrl}/issue/${i.identifier}`,
      archived_at: null,
      canceled_at: null,
      completed_at: state.type === "completed" ? updated : null,
      started_at: state.type === "started" || state.type === "completed" ? created : null,
      due_date: shiftDate(i.due_date),
      create_as_user: null,
      display_icon_url: null,
      estimate: i.estimate ?? null,
      parent_ref: null,
    });
    // Collection.insert stamps both with the wall clock; neither field is indexed.
    rec.created_at = created;
    rec.updated_at = updated;
    if (number > team.issue_sequence) ls.teams.update(team.id, { issue_sequence: number });
    issues++;
  }
  for (const i of cfg.issues ?? []) {
    const self = ls.issues.findOneBy("identifier", i.identifier);
    for (const r of i.relations ?? []) {
      const other = ls.issues.findOneBy("identifier", r.issue);
      if (!other) throw new Error(`relation of ${i.identifier} names an unknown issue ${r.issue}`);
      if (r.type === "parent") self.parent_ref = other.linear_id;
      else if (r.type === "child") other.parent_ref = self.linear_id;
      else if (r.type === "blocked_by") linearRelations.push({ id: crypto.randomUUID(), type: "blocks", issue_id: other.linear_id, related_issue_id: self.linear_id });
      else linearRelations.push({ id: crypto.randomUUID(), type: r.type, issue_id: self.linear_id, related_issue_id: other.linear_id });
    }
  }
  let comments = 0;
  for (const c of cfg.comments ?? []) {
    const issue = ls.issues.findOneBy("identifier", c.issue);
    if (!issue) throw new Error(`comment names an unknown issue ${c.issue}`);
    const rec = ls.comments.insert({
      linear_id: crypto.randomUUID(),
      issue_id: issue.linear_id,
      user_id: c.user ? need("user", c.user, linear.resolveUser(l.store, c.user)) : null,
      body: c.body,
      create_as_user: null,
      display_icon_url: null,
    });
    rec.created_at = rec.updated_at = shiftIso(c.created_at);
    comments++;
  }
  return { linear_issues: issues, linear_comments: comments };
}

const ADD = { slack: addSlack, linear: addLinear };
const t0 = performance.now();
let seeded = {};
if (seeds.slack) {
  for (const entry of seeds.slack.tokens ?? []) tokenUsers.slack.set(entry.token, entry.user);
  seeded = { ...seeded, ...addSlack(seeds.slack) };
}
if (seeds.linear) {
  for (const entry of seeds.linear.tokens ?? []) tokenUsers.linear.set(entry.token, entry.user);
  seeded = { ...seeded, ...addLinear({ ...seeds.linear, tokens: (seeds.linear.tokens ?? []).map((entry) => ({ ...entry, scopes: ["read", "write"] })) }) };
}

async function params(req) {
  const url = new URL(req.url);
  const out = Object.fromEntries(url.searchParams);
  if (req.method !== "GET") {
    const text = await req.clone().text();
    if ((req.headers.get("content-type") ?? "").includes("json")) {
      try {
        Object.assign(out, JSON.parse(text));
      } catch {}
    } else Object.assign(out, Object.fromEntries(new URLSearchParams(text)));
  }
  return out;
}
const asPost = (req, method, body) =>
  s.app.fetch(
    new Request(new URL(`/api/${method}`, req.url), {
      method: "POST",
      headers: { authorization: req.headers.get("authorization") ?? "", "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(body).toString(),
    }),
  );
async function slackHistory(req, p) {
  const all = [];
  let cursor = "";
  do {
    const page = await (await asPost(req, "conversations.history", { channel: p.channel, limit: "1000", cursor })).json();
    if (!page.ok) return Response.json(page);
    all.push(...page.messages);
    cursor = page.response_metadata?.next_cursor;
  } while (cursor);
  const incl = ["true", "1", true].includes(p.inclusive);
  const lo = p.oldest ? Number(p.oldest) : -Infinity;
  const hi = p.latest ? Number(p.latest) : Infinity;
  const kept = all.filter((m) => (incl ? Number(m.ts) >= lo && Number(m.ts) <= hi : Number(m.ts) > lo && Number(m.ts) < hi));
  const limit = Math.min(Number(p.limit) || 100, 1000);
  const start = p.cursor ? kept.findIndex((m) => m.ts === p.cursor) : 0;
  if (start < 0) return Response.json({ ok: false, error: "invalid_cursor" });
  const more = start + limit < kept.length;
  return Response.json({ ok: true, messages: kept.slice(start, start + limit), has_more: more, pin_count: 0, response_metadata: { next_cursor: more ? kept[start + limit].ts : "" } });
}
async function slackSearch(req, p) {
  const auth = await (await asPost(req, "auth.test", {})).json();
  if (!auth.ok) return Response.json(auth);
  const preds = [];
  for (const raw of String(p.query ?? "").match(/"[^"]+"|\S+/g) ?? []) {
    const term = raw.replace(/^"|"$/g, "");
    const lower = term.toLowerCase();
    if (lower.startsWith("in:")) {
      const ch = ss.channels.findOneBy("name", term.slice(3).replace(/^#/, ""));
      preds.push((m) => m.channel_id === ch?.channel_id);
    } else if (lower.startsWith("from:")) {
      const name = term.slice(5).replace(/^[<@]+|>$/g, "");
      const u = ss.users.all().find((x) => x.name === name) ?? ss.users.findOneBy("user_id", name);
      preds.push((m) => m.user === u?.user_id);
    } else if (lower.startsWith("after:")) preds.push((m) => Number(m.ts) > Date.parse(term.slice(6) + "T23:59:59Z") / 1000);
    else if (lower.startsWith("before:")) preds.push((m) => Number(m.ts) < Date.parse(term.slice(7) + "T00:00:00Z") / 1000);
    else preds.push((m) => m.text.toLowerCase().includes(lower));
  }
  const visible = new Map(ss.channels.all().filter((c) => !c.is_private || c.members.includes(auth.user_id)).map((c) => [c.channel_id, c]));
  const dir = p.sort_dir === "asc" ? 1 : -1;
  const hits = ss.messages.all().filter((m) => visible.has(m.channel_id) && preds.every((f) => f(m))).sort((a, b) => dir * (Number(a.ts) - Number(b.ts)));
  const count = Math.min(Number(p.count) || 20, 100);
  const page = Math.max(Number(p.page) || 1, 1);
  const slice = hits.slice((page - 1) * count, page * count);
  return Response.json({
    ok: true,
    query: p.query ?? "",
    messages: {
      total: hits.length,
      paging: { count, total: hits.length, page, pages: Math.ceil(hits.length / count) },
      matches: slice.map((m) => {
        const ch = visible.get(m.channel_id);
        return { type: "message", channel: { id: ch.channel_id, name: ch.name }, user: m.user, username: ss.users.findOneBy("user_id", m.user)?.name ?? "", ts: m.ts, text: m.text, permalink: `https://${slackDomain}.slack.com/archives/${ch.channel_id}/p${m.ts.replace(".", "")}` };
      }),
    },
  });
}
async function slackRoute(req, url) {
  const method = url.pathname.slice(5);
  const p = await params(req);
  if (method === "search.messages") return slackSearch(req, p);
  if (method === "conversations.history" && (p.oldest || p.latest)) return slackHistory(req, p);
  if (req.method === "GET") return asPost(req, method, Object.fromEntries(url.searchParams));
  return s.app.fetch(req);
}

// Linear's public schema as its SDK publishes it (linear_schema.graphql.gz beside this file, see
// templates/environment/external_services/), so a query validates as the real API validates it:
// introspection shows Linear's own types, and a field, argument or type Linear does not have is the
// caller's mistake, refused with 400 as Linear refuses it. The resolvers in linearRoot serve what the
// fake holds; linearFieldResolver answers the rest of the public schema.
const linearSchema = buildSchema(gunzipSync(readFileSync(new URL("./linear_schema.graphql.gz", import.meta.url))).toString("utf-8"));
// The arguments the resolvers read. Any other argument of a public field is ignored, and counted.
const LINEAR_ARGUMENTS = new Set(["first", "after", "filter", "orderBy", "includeArchived", "includeDisabled", "query", "term", "id"]);
// What a public field the fake does not hold answers with: null where the schema allows it, else its
// type's empty value, so the query still gets everything the fake has. The field is counted under its
// public name; the fields of an empty object are not counted again. An abstract type has no empty
// value, so a non-null one errors as a missing field does.
const EMPTY = Symbol("empty");
function emptyValue(type) {
  if (!isNonNullType(type)) return null;
  const named = getNamedType(type);
  if (isListType(type.ofType)) return [];
  if (isEnumType(named)) return named.getValues()[0].value;
  if (isScalarType(named)) return named.name === "Boolean" ? false : named.name === "Int" || named.name === "Float" ? 0 : "";
  return isAbstractType(named) ? null : { [EMPTY]: true };
}
// Each request's unsupported keys, by the request, for the access log and the counts.
const linearUnsupported = new WeakMap();
function linearFieldResolver(source, args, context, info) {
  const name = `${info.parentType.name}.${info.fieldName}`;
  if (source != null && typeof source === "object" && info.fieldName in source) {
    for (const arg of Object.keys(args)) if (!LINEAR_ARGUMENTS.has(arg)) context.unsupported.add(`linear:argument:${name}.${arg}`);
    return defaultFieldResolver(source, args, context, info);
  }
  if (!source?.[EMPTY]) context.unsupported.add(`linear:field:${name}`);
  return emptyValue(info.returnType);
}

const PRIORITY_LABELS = ["No priority", "Urgent", "High", "Medium", "Low"];
const COMPARATORS = {
  eq: (v, x) => v === x,
  neq: (v, x) => v !== x,
  in: (v, x) => x.includes(v),
  nin: (v, x) => !x.includes(v),
  eqIgnoreCase: (v, x) => String(v ?? "").toLowerCase() === String(x).toLowerCase(),
  neqIgnoreCase: (v, x) => String(v ?? "").toLowerCase() !== String(x).toLowerCase(),
  contains: (v, x) => String(v ?? "").includes(x),
  containsIgnoreCase: (v, x) => String(v ?? "").toLowerCase().includes(String(x).toLowerCase()),
  notContains: (v, x) => !String(v ?? "").includes(x),
  startsWith: (v, x) => String(v ?? "").startsWith(x),
  endsWith: (v, x) => String(v ?? "").endsWith(x),
  null: (v, x) => (v == null) === x,
  gt: (v, x) => v != null && cmp(v, x) > 0,
  gte: (v, x) => v != null && cmp(v, x) >= 0,
  lt: (v, x) => v != null && cmp(v, x) < 0,
  lte: (v, x) => v != null && cmp(v, x) <= 0,
};
// Shaped as Linear shapes a query it validated and then refused.
const inputError = (message) => new GraphQLError(message, { extensions: { code: "GRAPHQL_VALIDATION_FAILED", type: "graphql error", userError: true, userPresentableMessage: message } });
// Linear's date comparators also take an ISO 8601 duration, added to the present: "-P2W1D" is two
// weeks and a day ago.
const DURATION = /^([+-])?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$/;
function durationFromNowMs(text) {
  const m = DURATION.exec(text);
  if (!m || !m.slice(2).some(Boolean)) return NaN;
  const [sign, years, months, weeks, days, hours, minutes, seconds] = [m[1] === "-" ? -1 : 1, ...m.slice(2).map((part) => Number(part ?? 0))];
  const at = new Date(trialNowMs());
  at.setUTCFullYear(at.getUTCFullYear() + sign * years, at.getUTCMonth() + sign * months);
  return at.getTime() + sign * (((weeks * 7 + days) * 24 + hours) * 3600000 + minutes * 60000 + seconds * 1000);
}
function cmp(v, x) {
  if (typeof v === "number") return v - Number(x);
  const a = Date.parse(v);
  const parsed = Date.parse(x);
  const b = Number.isNaN(parsed) ? durationFromNowMs(String(x)) : parsed;
  if (Number.isNaN(a) || Number.isNaN(b)) throw inputError(`Expected a valid ISO 8601 date, duration or number, got ${JSON.stringify(x)}`);
  return a - b;
}
// `view` is a plain object; nested objects are related entities and arrays are collections.
function matches(view, filter, path = "filter") {
  if (filter == null) return true;
  return Object.entries(filter).every(([key, want]) => {
    if (key === "and") return want.every((f) => matches(view, f, path));
    if (key === "or") return want.some((f) => matches(view, f, path));
    if (view !== null && typeof view === "object" && !Array.isArray(view)) {
      if (!(key in view)) throw inputError(`Field "${key}" is not defined by type "${path}".`);
      const v = view[key];
      if (Array.isArray(v)) {
        if (want.some) return v.some((x) => matches(x, want.some, `${path}.${key}`));
        if (want.every) return v.every((x) => matches(x, want.every, `${path}.${key}`));
        if ("length" in want) return matches(v.length, want.length, `${path}.${key}.length`);
        return v.some((x) => matches(x, want, `${path}.${key}`));
      }
      const relationNull = want !== null && typeof want === "object" && Object.keys(want).length === 1 && "null" in want;
      if (relationNull && (v === null || typeof v === "object")) return (v === null) === want.null;
      if (v !== null && typeof v === "object") return matches(v, want, `${path}.${key}`);
      if (v === null && want !== null && typeof want === "object" && !Object.keys(want).some((k) => k in COMPARATORS)) return "null" in want ? want.null === true : false;
      return matches(v, want, `${path}.${key}`);
    }
    const op = COMPARATORS[key];
    if (!op) throw inputError(`Field "${key}" is not defined by type "${path}".`);
    return op(view, want);
  });
}
function connection(items, args, format) {
  const first = Math.min(args.first ?? 50, 250);
  const start = args.after ? Number(Buffer.from(args.after, "base64url").toString().split(":")[1]) + 1 : 0;
  const slice = items.slice(start, start + first);
  const cursor = (i) => Buffer.from(`linear:${start + i}`).toString("base64url");
  const nodes = slice.map(format);
  return {
    nodes,
    edges: nodes.map((node, i) => ({ node, cursor: cursor(i) })),
    pageInfo: { hasNextPage: start + first < items.length, hasPreviousPage: start > 0, startCursor: slice.length ? cursor(0) : null, endCursor: slice.length ? cursor(slice.length - 1) : null },
  };
}
function isActiveCycle(c) {
  const now = trialNowMs();
  return Boolean(c.starts_at && c.ends_at && Date.parse(c.starts_at) <= now && now < Date.parse(c.ends_at));
}
function linearRoot(me) {
  const by = (coll, id) => (id ? coll.findOneBy("linear_id", id) : undefined);
  const order = (items, orderBy) => {
    const key = orderBy === "updatedAt" ? "updated_at" : "created_at";
    return [...items].sort((a, b) => b[key].localeCompare(a[key]) || b.id - a.id);
  };
  const fUser = (u) => u && {
    id: u.linear_id, name: u.name, displayName: u.display_name, email: u.email, active: u.active, admin: u.admin, isMe: u.linear_id === me?.linear_id,
    createdAt: u.created_at, updatedAt: u.updated_at, url: `${linearUrl}/profiles/${u.display_name}`,
    assignedIssues: (a) => issues(a, (i) => i.assignee_id === u.linear_id),
    createdIssues: (a) => issues(a, (i) => i.creator_id === u.linear_id),
  };
  const fState = (x) => x && { id: x.linear_id, name: x.name, type: x.type, position: x.position, color: "#95a2b3", team: () => fTeam(by(ls.teams, x.team_id)), createdAt: x.created_at, updatedAt: x.updated_at };
  const fLabel = (x) => x && { id: x.linear_id, name: x.name, color: x.color, description: x.description, team: () => fTeam(by(ls.teams, x.team_id)), createdAt: x.created_at, updatedAt: x.updated_at };
  const fCycle = (x) => x && { id: x.linear_id, number: x.number, name: x.name, startsAt: x.starts_at, endsAt: x.ends_at, createdAt: x.created_at, updatedAt: x.updated_at, team: () => fTeam(by(ls.teams, x.team_id)), issues: (a) => issues(a, (i) => i.cycle_id === x.linear_id) };
  const fProject = (x) => x && {
    id: x.linear_id, name: x.name, description: x.description ?? "", state: x.state, url: `${linearUrl}/project/${x.linear_id}`, createdAt: x.created_at, updatedAt: x.updated_at, startDate: null, targetDate: null,
    progress: () => {
      const mine = ls.issues.all().filter((i) => i.project_id === x.linear_id);
      return mine.length ? mine.filter((i) => i.completed_at).length / mine.length : 0;
    },
    teams: (a) => connection(ls.teams.all().filter((t) => ls.issues.all().some((i) => i.project_id === x.linear_id && i.team_id === t.linear_id)), a, fTeam),
    issues: (a) => issues(a, (i) => i.project_id === x.linear_id),
  };
  const fTeam = (t) => t && {
    id: t.linear_id, key: t.key, name: t.name, description: t.description, private: t.private, createdAt: t.created_at, updatedAt: t.updated_at,
    states: (a) => connection(ls.workflowStates.findBy("team_id", t.linear_id).sort((x, y) => x.position - y.position), a, fState),
    members: (a) => connection(ls.users.all(), a, fUser),
    labels: (a) => connection(ls.issueLabels.all().filter((x) => !x.team_id || x.team_id === t.linear_id), a, fLabel),
    cycles: (a) => connection(ls.cycles.findBy("team_id", t.linear_id).filter((c) => matches(cycleView(c), a.filter)), a, fCycle),
    projects: (a) => connection(ls.projects.all().filter((p) => ls.issues.all().some((i) => i.project_id === p.linear_id && i.team_id === t.linear_id)), a, fProject),
    activeCycle: () => fCycle(ls.cycles.findBy("team_id", t.linear_id).find(isActiveCycle)),
    issues: (a) => issues(a, (i) => i.team_id === t.linear_id),
  };
  const fComment = (c) => ({ id: c.linear_id, body: c.body, createdAt: c.created_at, updatedAt: c.updated_at, url: `${linearUrl}/issue/${by(ls.issues, c.issue_id).identifier}#comment-${c.linear_id.slice(0, 8)}`, user: () => fUser(by(ls.users, c.user_id)), issue: () => fIssue(by(ls.issues, c.issue_id)) });
  const fRelation = (r) => ({ id: r.id, type: r.type, createdAt: by(ls.issues, r.issue_id).created_at, issue: () => fIssue(by(ls.issues, r.issue_id)), relatedIssue: () => fIssue(by(ls.issues, r.related_issue_id)) });
  const fIssue = (i) => i && {
    id: i.linear_id, identifier: i.identifier, number: i.number, title: i.title, description: i.description, priority: i.priority, priorityLabel: PRIORITY_LABELS[i.priority],
    estimate: i.estimate ?? null, url: i.url, branchName: `${i.identifier.toLowerCase()}-${i.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 40)}`,
    createdAt: i.created_at, updatedAt: i.updated_at, archivedAt: i.archived_at, canceledAt: i.canceled_at, completedAt: i.completed_at, startedAt: i.started_at, dueDate: i.due_date, trashed: null,
    team: () => fTeam(by(ls.teams, i.team_id)), state: () => fState(by(ls.workflowStates, i.state_id)), assignee: () => fUser(by(ls.users, i.assignee_id)), creator: () => fUser(by(ls.users, i.creator_id)),
    project: () => fProject(by(ls.projects, i.project_id)), cycle: () => fCycle(by(ls.cycles, i.cycle_id)), parent: () => fIssue(by(ls.issues, i.parent_ref)),
    children: (a) => issues(a, (x) => x.parent_ref === i.linear_id),
    labels: (a) => connection(i.label_ids.map((id) => by(ls.issueLabels, id)).filter(Boolean), a, fLabel),
    comments: (a) => connection(ls.comments.findBy("issue_id", i.linear_id).filter((c) => matches(commentView(c), a.filter)).sort((x, y) => x.created_at.localeCompare(y.created_at)), a, fComment),
    relations: (a) => connection(linearRelations.filter((r) => r.issue_id === i.linear_id), a, fRelation),
    inverseRelations: (a) => connection(linearRelations.filter((r) => r.related_issue_id === i.linear_id), a, fRelation),
  };
  const userView = (u) => (u ? { id: u.linear_id, name: u.name, displayName: u.display_name, email: u.email, active: u.active, admin: u.admin, isMe: u.linear_id === me?.linear_id } : null);
  const teamView = (t) => (t ? { id: t.linear_id, key: t.key, name: t.name, private: t.private } : null);
  const cycleView = (c) => (c ? { id: c.linear_id, number: c.number, name: c.name, startsAt: c.starts_at, endsAt: c.ends_at, team: teamView(by(ls.teams, c.team_id)), isActive: isActiveCycle(c) } : null);
  const commentView = (c) => ({ id: c.linear_id, body: c.body, createdAt: c.created_at, updatedAt: c.updated_at, user: userView(by(ls.users, c.user_id)) });
  const issueView = (i, deep = true) => {
    const st = by(ls.workflowStates, i.state_id);
    const pr = by(ls.projects, i.project_id);
    return {
      id: i.linear_id, identifier: i.identifier, number: i.number, title: i.title, description: i.description, priority: i.priority, estimate: i.estimate ?? null,
      createdAt: i.created_at, updatedAt: i.updated_at, completedAt: i.completed_at, startedAt: i.started_at, canceledAt: i.canceled_at, archivedAt: i.archived_at, dueDate: i.due_date,
      team: teamView(by(ls.teams, i.team_id)), state: st ? { id: st.linear_id, name: st.name, type: st.type } : null, assignee: userView(by(ls.users, i.assignee_id)), creator: userView(by(ls.users, i.creator_id)),
      project: pr ? { id: pr.linear_id, name: pr.name, state: pr.state } : null, cycle: cycleView(by(ls.cycles, i.cycle_id)),
      labels: i.label_ids.map((id) => by(ls.issueLabels, id)).filter(Boolean).map((x) => ({ id: x.linear_id, name: x.name })),
      parent: deep && i.parent_ref ? issueView(by(ls.issues, i.parent_ref), false) : null,
      children: deep ? ls.issues.all().filter((x) => x.parent_ref === i.linear_id).map((x) => issueView(x, false)) : [],
      comments: deep ? ls.comments.findBy("issue_id", i.linear_id).map(commentView) : [],
      hasBlockedByRelations: linearRelations.some((r) => r.type === "blocks" && r.related_issue_id === i.linear_id),
      hasBlockingRelations: linearRelations.some((r) => r.type === "blocks" && r.issue_id === i.linear_id),
    };
  };
  const issues = (a, pred = () => true) => connection(order(ls.issues.all().filter((i) => (a.includeArchived || !i.archived_at) && pred(i) && matches(issueView(i), a.filter)), a.orderBy), a, fIssue);
  const one = (what, rec, format) => {
    if (!rec) throw new GraphQLError("Entity not found: " + what, { extensions: { code: "INPUT_ERROR", type: "invalid input", userError: true, userPresentableMessage: `Could not find referenced ${what}.` } });
    return format(rec);
  };
  return {
    viewer: () => fUser(me),
    organization: () => {
      const o = ls.organizations.all()[0];
      return { id: o.linear_id, name: o.name, urlKey: o.url_key, createdAt: o.created_at };
    },
    teams: (a) => connection(ls.teams.all().filter((t) => matches(teamView(t), a.filter)), a, fTeam),
    team: ({ id }) => one("Team", linear.resolveTeam(l.store, id), fTeam),
    users: (a) => connection(ls.users.all().filter((u) => matches(userView(u), a.filter)), a, fUser),
    user: ({ id }) => one("User", by(ls.users, id), fUser),
    issues: (a) => issues(a),
    issue: ({ id }) => one("Issue", linear.resolveIssue(l.store, id), fIssue),
    issueSearch: (a) => issues(a, (i) => `${i.identifier} ${i.title} ${i.description ?? ""}`.toLowerCase().includes((a.query ?? "").toLowerCase())),
    searchIssues: (a) => issues(a, (i) => a.term.toLowerCase().split(/\s+/).every((w) => `${i.identifier} ${i.title} ${i.description ?? ""}`.toLowerCase().includes(w))),
    projects: (a) => connection(order(ls.projects.all().filter((p) => matches({ id: p.linear_id, name: p.name, state: p.state }, a.filter)), a.orderBy), a, fProject),
    project: ({ id }) => one("Project", linear.resolveProject(l.store, id), fProject),
    cycles: (a) => connection(ls.cycles.all().filter((c) => matches(cycleView(c), a.filter)), a, fCycle),
    cycle: ({ id }) => one("Cycle", by(ls.cycles, id), fCycle),
    issueLabels: (a) => connection(ls.issueLabels.all().filter((x) => matches({ id: x.linear_id, name: x.name, team: teamView(by(ls.teams, x.team_id)) }, a.filter)), a, fLabel),
    workflowStates: (a) => connection(ls.workflowStates.all().filter((x) => matches({ id: x.linear_id, name: x.name, type: x.type, team: teamView(by(ls.teams, x.team_id)) }, a.filter)), a, fState),
    comments: (a) => connection(order(ls.comments.all().filter((c) => matches({ ...commentView(c), issue: issueView(by(ls.issues, c.issue_id), false) }, a.filter)), a.orderBy), a, fComment),
  };
}
const linearAuthFailure = () =>
  Response.json({ errors: [{ message: "Authentication required, not authenticated", extensions: { type: "authentication error", code: "AUTHENTICATION_ERROR", userError: true, userPresentableMessage: "You need to authenticate to access this operation." } }] }, { status: 401 });
async function linearRoute(req, url) {
  const token = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
  const record = token ? ls.tokens.findOneBy("token", token) : undefined;
  if (!record) return linearAuthFailure();
  if (url.pathname !== "/graphql" || req.method !== "POST") return l.app.fetch(req);
  let body;
  try {
    body = await req.clone().json();
  } catch {
    return Response.json({ errors: [{ message: "POST body missing, invalid Content-Type, or JSON object has no keys." }] }, { status: 400 });
  }
  let doc;
  try {
    doc = parse(body.query ?? "");
  } catch (e) {
    return Response.json({ errors: [{ message: e.message, extensions: { code: "GRAPHQL_PARSE_FAILED" } }] }, { status: 400 });
  }
  const ops = doc.definitions.filter((d) => d.kind === "OperationDefinition");
  const op = ops.find((d) => !body.operationName || d.name?.value === body.operationName) ?? ops[0];
  if (op?.operation === "mutation") return l.app.fetch(req);
  const me = ls.users.findOneBy("linear_id", record.user_id);
  const context = { unsupported: new Set() };
  const result = await graphql({ schema: linearSchema, source: body.query, rootValue: linearRoot(me), contextValue: context, fieldResolver: linearFieldResolver, variableValues: body.variables, operationName: body.operationName });
  linearUnsupported.set(req, [...context.unsupported]);
  // Real Linear answers a query it cannot validate with 400 and a partially failed one with 200.
  return Response.json(result, { status: result.errors && !("data" in result) ? 400 : 200 });
}

const accessLog = [];
// Slack takes a token as a query or form parameter too, so the logged URL and body are masked of
// every token the seed names.
function withoutTokens(text) {
  let masked = text;
  for (const tokens of Object.values(tokenUsers)) for (const token of tokens.keys()) masked = masked.split(token).join("[redacted]");
  return masked;
}
function countResults(service, json) {
  if (json == null || typeof json !== "object") return null;
  if (service === "slack") {
    for (const k of ["messages", "channels", "members", "files", "items"]) {
      if (Array.isArray(json[k])) return json[k].length;
      if (Array.isArray(json[k]?.matches)) return json[k].matches.length;
    }
    return null;
  }
  let n = null;
  const walk = (v) => {
    if (Array.isArray(v?.nodes)) n = (n ?? 0) + v.nodes.length;
    if (v && typeof v === "object") for (const x of Object.values(v)) walk(x);
  };
  walk(json.data);
  return n;
}
// What a call the fake does not support is counted under: nothing for one it does. A fake that
// answers "no such method" for a Slack method, or answers a public Linear field, argument or filter
// key empty, has a gap where the real service has data, and the count is how the gap is seen. A
// Linear query the public schema refuses is the caller's mistake, as it would be against Linear.
const unsupportedCalls = {};
function unsupportedKeys(service, host, url, json, req) {
  if (host !== REAL_HOST[service]) return [`${service}:host:${host}`];
  if (service === "slack") return json?.error === "unknown_method" ? [`slack:${url.pathname.replace(/^\/api\//, "")}`] : [];
  const keys = linearUnsupported.get(req) ?? [];
  // A filter key the evaluator refuses after validation passed is one Linear has and the fake lacks;
  // the same message before execution (no data) is Linear's own refusal of a key it does not have.
  if (json && "data" in json)
    for (const { message = "" } of json.errors ?? []) {
      const filter = message.match(/^Field "([^"]+)" is not defined by type "([^"]+)"/);
      if (filter) keys.push(`linear:filter:${filter[2]}.${filter[1]}`);
    }
  return keys;
}
const notFound = (service) => (service === "slack" ? Response.json({ ok: false, error: "unknown_method" }, { status: 404 }) : Response.json({ errors: [{ message: "Not Found" }] }, { status: 404 }));
function logged(service, route) {
  return async (req) => {
    const url = new URL(req.url);
    const began = Date.now();
    const bodyText = req.method === "GET" ? "" : await req.clone().text();
    // The shim names the host a request was for. A service's other hosts (file downloads, uploads)
    // are not served, and a request for one is answered here rather than by the real host.
    const host = (req.headers.get("x-forwarded-host") ?? REAL_HOST[service]).toLowerCase();
    let res = host === REAL_HOST[service] ? await route(req, url) : notFound(service);
    // The packages underneath serve pages of their own (an inspector, sign-in screens) that say what
    // they are. Neither real API serves a page at these hosts' API paths, so a page becomes a 404.
    if ((res.headers.get("content-type") ?? "").includes("text/html")) res = notFound(service);
    let text = await res.clone().text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch {}
    // Slack answers a method it does not have with a 200 that says so, where the packages
    // underneath answer a path they do not route with a 404 of their own.
    if (service === "slack" && host === REAL_HOST.slack && res.status === 404 && url.pathname.startsWith("/api/") && json?.ok === undefined) {
      json = { ok: false, error: "unknown_method" };
      text = JSON.stringify(json);
      res = Response.json(json);
    }
    const token = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
    const unsupported = unsupportedKeys(service, host, url, json, req);
    for (const key of unsupported) unsupportedCalls[key] = (unsupportedCalls[key] ?? 0) + 1;
    const rec = {
      ts: new Date(began).toISOString(),
      seq: accessLog.length + 1,
      service,
      method: req.method,
      url: withoutTokens(`https://${host}${url.pathname}${url.search}`),
      body: withoutTokens(bodyText.length > 8192 ? bodyText.slice(0, 8192) + `...[${bodyText.length} bytes]` : bodyText),
      content_type: req.headers.get("content-type"),
      // The credential itself never reaches the log: a digest tells two tokens apart.
      token_user: token ? tokenUsers[service].get(token) ?? null : null,
      token_sha256_8: token ? crypto.createHash("sha256").update(token).digest("hex").slice(0, 8) : null,
      user_agent: req.headers.get("user-agent"),
      caller: req.headers.get("x-eval-caller"),
      status: res.status,
      ok: service === "slack" ? json?.ok ?? null : json ? !json.errors : null,
      error: service === "slack" ? json?.error ?? null : json?.errors?.[0]?.message ?? null,
      // The first of a request's unsupported keys; the counts carry every one.
      unsupported: unsupported[0] ?? null,
      result_count: countResults(service, json),
      response_bytes: Buffer.byteLength(text),
      duration_ms: Date.now() - began,
    };
    accessLog.push(rec);
    if (accessLogPath) appendFileSync(accessLogPath, JSON.stringify(rec) + "\n");
    return res;
  };
}

const counts = () => ({
  ...(seeds.slack ? { slack: { users: ss.users.all().length, channels: ss.channels.all().length, messages: ss.messages.all().length } } : {}),
  ...(seeds.linear
    ? { linear: { users: ls.users.all().length, teams: ls.teams.all().length, projects: ls.projects.all().length, cycles: ls.cycles.all().length, labels: ls.issueLabels.all().length, issues: ls.issues.all().length, comments: ls.comments.all().length, relations: linearRelations.length } }
    : {}),
  requests: Object.fromEntries(served.map((name) => [name, accessLog.filter((rec) => rec.service === name).length])),
});
const STORES = { slack: s.store, linear: l.store };
async function control(req) {
  const url = new URL(req.url);
  if (url.pathname === "/_control/health") return Response.json({ ok: true, shift_ms: shiftMs, services: served, counts: counts(), unsupported_calls: unsupportedCalls });
  if (url.pathname === "/_control/log") return Response.json(accessLog);
  if (url.pathname === "/_control/state") {
    const redact = (snap) => JSON.parse(JSON.stringify(snap, (k, v) => (k === "token" || k === "client_secret" || k === "refresh_token" ? "[redacted]" : v)));
    return Response.json({
      dumped_at: new Date().toISOString(),
      shift_ms: shiftMs,
      counts: counts(),
      unsupported_calls: unsupportedCalls,
      ...Object.fromEntries(served.map((name) => [name, redact(STORES[name].snapshot())])),
      ...(seeds.linear ? { linear_relations: linearRelations } : {}),
    });
  }
  const records = url.pathname.match(/^\/_control\/records\/([a-z]+)$/);
  if (records && req.method === "POST") {
    const name = records[1];
    if (!served.includes(name)) return Response.json({ ok: false, error: `the ${name} service was not seeded, so it takes no update` }, { status: 400 });
    // An update lands whole or not at all: a half-applied one would leave the trial on data nobody authored.
    let update;
    try {
      update = await req.json();
    } catch {
      return Response.json({ ok: false, error: "the update is not JSON" }, { status: 400 });
    }
    const before = { store: JSON.stringify(STORES[name].snapshot()), relations: linearRelations.length };
    try {
      return Response.json({ ok: true, added: ADD[name](update), counts: counts() });
    } catch (e) {
      STORES[name].restore(JSON.parse(before.store));
      if (name === "linear") linearRelations.length = before.relations;
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
const ROUTES = { slack: (req, url) => (url.pathname.startsWith("/api/") ? slackRoute(req, url) : s.app.fetch(req)), linear: linearRoute };
const ports = { control: await listen(control, env.CONTROL_PORT) };
for (const name of served) ports[name] = await listen(logged(name, ROUTES[name]), env[`${name.toUpperCase()}_PORT`]);
console.log(JSON.stringify({ ready: true, ports, shift_ms: shiftMs, seed_ms: Math.round(performance.now() - t0), seeded, counts: counts() }));
