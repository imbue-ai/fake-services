// The fake Linear: one GraphQL endpoint served from Linear's public schema by the resolvers here, on
// a store that @emulators/linear seeds and resolves references in. Issues keep the seed's
// identifiers and timestamps, with estimate, parent/children and relations; every other record takes
// the dates the seed states or derives them from the seed's records (settle()), and a project serves
// its lead and planned dates; queries take nested filters, date/number comparators, orderBy and
// searchIssues; a public field or argument the fake does not hold is answered empty and counted; a
// mutation or subscription is refused. See launcher.mjs for the interface this module exports.
import crypto from "node:crypto";
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { Store } from "@emulators/core";
import * as emulator from "@emulators/linear";
import { GraphQLError, buildSchema, execute, getNamedType, getOperationAST, isAbstractType, isEnumType, isListType, isNonNullType, isScalarType, parse, validate, valueFromAST } from "graphql";

export const name = "linear";
export const host = "api.linear.app";
// What an error answer points its reader at. Nothing a caller is sent names what is serving it.
const DOCS = "https://linear.app/developers/graphql";

// Linear's public schema as its SDK publishes it (linear_schema.graphql.gz in the directory above,
// see templates/environment/external_services/), so a query validates as the real API validates it:
// introspection shows Linear's own types, and a field, argument or type Linear does not have is the
// caller's mistake, refused with 400 as Linear refuses it. The resolvers in root() serve what the
// fake holds; fieldResolver answers the rest of the public schema.
const schema = buildSchema(gunzipSync(readFileSync(new URL("../linear_schema.graphql.gz", import.meta.url))).toString("utf-8"));

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
// An argument's default as the schema states it: the SDL gives it as a literal.
const defaultOf = (arg) => (arg.default == null ? undefined : "literal" in arg.default ? valueFromAST(arg.default.literal, arg.type) : arg.default.value);
// A resolver is handed its arguments through a proxy that notes which it read. An argument the
// schema has and the resolver never read is ignored, and counted, unless it holds the schema's own
// default, which the caller did not ask for. The counting is per field: a resolver that reads
// `includeArchived` says nothing about another that does not. A resolver therefore reads every
// argument it serves before it returns, not inside a per-record callback: with no records to visit,
// an argument read there is never read, and counted as ignored.
function fieldResolver(source, args, context, info) {
  const fieldName = `${info.parentType.name}.${info.fieldName}`;
  if (source != null && typeof source === "object" && info.fieldName in source) {
    const property = source[info.fieldName];
    if (typeof property !== "function") return property;
    const read = new Set();
    const noted = new Proxy(args, {
      get(target, key) {
        if (typeof key === "string") read.add(key);
        return target[key];
      },
    });
    const result = property(noted, context, info);
    for (const arg of info.parentType.getFields()[info.fieldName].args)
      if (arg.name in args && !read.has(arg.name) && JSON.stringify(args[arg.name]) !== JSON.stringify(defaultOf(arg))) context.unsupported.add(`linear:argument:${fieldName}.${arg.name}`);
    return result;
  }
  if (!source?.[EMPTY]) context.unsupported.add(`linear:field:${fieldName}`);
  return emptyValue(info.returnType);
}

// Linear's limits for an API key, as its documentation states them (linear.app/developers/
// rate-limiting, read 2026-10-29): 2,500 requests and 3,000,000 complexity points per hour per user,
// both leaky buckets refilling at limit/hour, and no single query above 10,000 points. An
// unauthenticated request is refused by authentication here, so Linear's per-address limits for
// those are not kept.
const MAX_QUERY_COMPLEXITY = 10000;
// A query's complexity as Linear charges it, measured against api.linear.app on 2026-10-29 (the rule
// its documentation states -- 0.1 a property, 1 an object, a connection multiplying its children by
// its page size -- charges a nested query orders of magnitude more than the API does). A selection on
// an object costs 0.2 when it asks for `id` alone, else 1 plus 0.1 for each scalar field plus the
// cost of each object field's own selection. A connection field multiplies the cost of its
// selection's fields (`nodes`, `pageInfo`, each a selection of its own) by its pagination argument
// (`first` or `last`), else 50 -- except the connections in UNMULTIPLIED_CONNECTIONS, which Linear
// prices like a plain object: 1 plus the cost of its selection's fields. The sum is rounded up.
// Counted in tenths, so the sum is exact. A document that does not parse, or names no operation to
// run, costs nothing: Linear refuses it before it runs.
// The connections Linear prices without a page-size multiplier: the root issues list, and the lists
// an issue carries along. Every other connection measured (the root teams, projects, users, cycles,
// comments and labels; a team's, project's, cycle's or user's issues; a project's members, teams,
// milestones and updates; an issue's relations, history and documents; a comment's children) is
// multiplied.
const UNMULTIPLIED_CONNECTIONS = new Set(["Query.issues", "Issue.comments", "Issue.labels", "Issue.children", "Issue.attachments", "Issue.subscribers", "Issue.needs"]);
function complexityOf(body) {
  let doc;
  try {
    doc = parse(body.query ?? "");
  } catch {
    return 0;
  }
  const op = getOperationAST(doc, body.operationName);
  if (!op) return 0;
  const fragments = new Map(doc.definitions.filter((d) => d.kind === "FragmentDefinition").map((d) => [d.name.value, d]));
  // A variable the request leaves out runs with the default its operation declares.
  const defaults = Object.fromEntries((op.variableDefinitions ?? []).filter((d) => d.defaultValue?.kind === "IntValue").map((d) => [d.variable.name.value, Number(d.defaultValue.value)]));
  const variables = { ...defaults, ...(body.variables ?? {}) };
  const pageSize = (field) => {
    for (const arg of field.arguments ?? [])
      if (arg.name.value === "first" || arg.name.value === "last") {
        const value = arg.value.kind === "Variable" ? variables[arg.value.name.value] : arg.value.kind === "IntValue" ? Number(arg.value.value) : undefined;
        if (Number.isInteger(value)) return value;
      }
    return 50;
  };
  // The fragments being expanded, so a cycle (which validation refuses anyway) ends.
  const expanding = new Set();
  // The fields a selection set names, fragments expanded, each with the type it is read on.
  const picked = (selectionSet, type, into) => {
    for (const sel of selectionSet.selections) {
      if (sel.kind === "InlineFragment") picked(sel.selectionSet, sel.typeCondition ? schema.getType(sel.typeCondition.name.value) : type, into);
      else if (sel.kind === "FragmentSpread") {
        const fragment = fragments.get(sel.name.value);
        if (!fragment || expanding.has(sel.name.value)) continue;
        expanding.add(sel.name.value);
        picked(fragment.selectionSet, schema.getType(fragment.typeCondition.name.value), into);
        expanding.delete(sel.name.value);
      } else into.push({ field: sel, owner: type });
    }
    return into;
  };
  const fieldsCost = (selectionSet, type) => picked(selectionSet, type, []).reduce((sum, { field, owner }) => sum + fieldCost(field, owner), 0);
  const selectionCost = (selectionSet, type) => {
    const fields = picked(selectionSet, type, []);
    if (fields.length === 1 && fields[0].field.name.value === "id") return 2;
    return 10 + fields.reduce((sum, { field, owner }) => sum + fieldCost(field, owner), 0);
  };
  const fieldCost = (field, owner) => {
    const def = owner?.getFields?.()[field.name.value];
    if (!def || !field.selectionSet) return 1;
    const named = getNamedType(def.type);
    if (!named.name.endsWith("Connection")) return selectionCost(field.selectionSet, named);
    if (UNMULTIPLIED_CONNECTIONS.has(`${owner.name}.${field.name.value}`)) return 10 + fieldsCost(field.selectionSet, named);
    return pageSize(field) * fieldsCost(field.selectionSet, named);
  };
  return Math.ceil(fieldsCost(op.selectionSet, schema.getRootType(op.operation)) / 10);
}
// A /graphql request's document, variables and operation, from the URL for a GET and the body
// otherwise: {body}, or {error}, what Linear answers a request it cannot read with.
async function graphqlBody(req, url) {
  if (req.method !== "GET") {
    const body = jsonOf(await req.clone().text());
    return body && typeof body === "object" ? { body } : { error: "POST body missing, invalid Content-Type, or JSON object has no keys." };
  }
  const variablesText = url.searchParams.get("variables");
  const variables = variablesText === null ? null : jsonOf(variablesText);
  if (variables === undefined || typeof variables !== "object") return { error: "Variables are invalid JSON." };
  return { body: { query: url.searchParams.get("query") ?? "", variables, operationName: url.searchParams.get("operationName") ?? undefined } };
}
function jsonOf(text) {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}
const tokenOf = (req) => (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
const rateLimited = (message) => ({ errors: [{ message, extensions: { type: "ratelimited", code: "RATELIMITED", userError: true, userPresentableMessage: message } }] });
// Shaped as Linear refuses a query past its per-query maximum (measured 2026-10-29): an HTTP 400 with no
// standing headers.
const tooComplex = (cost) =>
  Response.json(
    { errors: [{ message: "Query too complex", extensions: { type: "invalid input", code: "INPUT_ERROR", statusCode: 400, userError: true, userPresentableMessage: `The query is too complex. Complexity: ${cost}. Maximum allowed complexity: ${MAX_QUERY_COMPLEXITY}.`, http: { status: 400 } } }] },
    { status: 400 },
  );
// See rate_limits.mjs for what a policy holds.
export const limits = {
  buckets: {
    requests: { limit: 2500, windowSeconds: 3600, algorithm: "leaky" },
    complexity: { limit: 3000000, windowSeconds: 3600, algorithm: "leaky", maxCost: MAX_QUERY_COMPLEXITY },
  },
  async bucketOf(req, url, service) {
    const user = service.tokenUsers.get(tokenOf(req));
    if (!user || url.pathname !== "/graphql") return [];
    const { body } = await graphqlBody(req, url);
    return [
      { bucket: "requests", scope: user, cost: 1 },
      { bucket: "complexity", scope: user, cost: body ? complexityOf(body) : 0 },
    ];
  },
  // A query above the per-query maximum is refused as Linear refuses it: an input error, not a rate
  // limit, since waiting never admits it.
  refuse: (req, retryAfterSeconds, bucket, cost) =>
    retryAfterSeconds === null
      ? tooComplex(cost)
      : Response.json(
          rateLimited(
            bucket === "complexity"
              ? "Rate limit exceeded: too much query complexity in the last hour."
              : "Rate limit exceeded: too many requests in the last hour.",
          ),
          { status: 429 },
        ),
  headers: ({ requests, complexity }) => ({
    "X-RateLimit-Requests-Limit": requests.limit,
    "X-RateLimit-Requests-Remaining": requests.remaining,
    "X-RateLimit-Requests-Reset": requests.resetMs,
    "X-Complexity": complexity.cost,
    "X-RateLimit-Complexity-Limit": complexity.limit,
    "X-RateLimit-Complexity-Remaining": complexity.remaining,
    "X-RateLimit-Complexity-Reset": complexity.resetMs,
  }),
};

const PRIORITY_LABELS = ["No priority", "Urgent", "High", "Medium", "Low"];
// Shaped as Linear shapes a query it validated and then refused.
const inputError = (message) => new GraphQLError(message, { extensions: { code: "GRAPHQL_VALIDATION_FAILED", type: "graphql error", userError: true, userPresentableMessage: message } });
const notFoundError = (what) => new GraphQLError("Entity not found: " + what, { extensions: { code: "INPUT_ERROR", type: "invalid input", userError: true, userPresentableMessage: `Could not find referenced ${what}.` } });
const authFailure = () =>
  Response.json({ errors: [{ message: "Authentication required, not authenticated", extensions: { type: "authentication error", code: "AUTHENTICATION_ERROR", userError: true, userPresentableMessage: "You need to authenticate to access this operation." } }] }, { status: 401 });
// A write is refused as Linear's SDK reads a 403: its `forbidden` error type.
const forbidden = (operation) => {
  const message = `You do not have the permissions to perform this ${operation}.`;
  return Response.json({ errors: [{ message, extensions: { type: "forbidden", code: "FORBIDDEN", userError: true, userPresentableMessage: message } }] }, { status: 403 });
};
// Linear's date comparators also take an ISO 8601 duration, added to the present: "-P2W1D" is two
// weeks and a day ago.
const DURATION = /^([+-])?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$/;
// The filter argument's input type of the field being resolved, which names the filter's keys as
// Linear names them in an error.
const filterTypes = new WeakMap();
function filterTypeOf(info) {
  const field = info.parentType.getFields()[info.fieldName];
  if (!filterTypes.has(field)) filterTypes.set(field, getNamedType(field.args.find((arg) => arg.name === "filter").type));
  return filterTypes.get(field);
}

export function create(seed, clock) {
  const store = new Store();
  const ls = emulator.getLinearStore(store);
  // Relations are the launcher's own collection in the emulator's store, so a snapshot of the store
  // holds them and a restore brings them back with everything else.
  const relations = store.collection("linear.issue_relations", ["issue_id", "related_issue_id"]);
  const orgUrl = `https://linear.app/${seed.organization?.url_key ?? "workspace"}`;
  const tokenUsers = new Map((seed.tokens ?? []).map((entry) => [entry.token, entry.user]));

  function durationFromNowMs(text) {
    const m = DURATION.exec(text);
    if (!m || !m.slice(2).some(Boolean)) return NaN;
    const [sign, years, months, weeks, days, hours, minutes, seconds] = [m[1] === "-" ? -1 : 1, ...m.slice(2).map((part) => Number(part ?? 0))];
    const at = new Date(clock.now());
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
  // `view` is a plain object whose keys are the filter type's fields; nested objects are related
  // entities and arrays are collections. `type` is the schema's input type for `filter`, which
  // validation has already held the keys to, so a key the view lacks is one Linear filters by and
  // the fake does not.
  function matches(view, filter, type) {
    if (filter == null) return true;
    return Object.entries(filter).every(([key, want]) => {
      if (key === "and") return want.every((f) => matches(view, f, type));
      if (key === "or") return want.some((f) => matches(view, f, type));
      const unsupported = () => inputError(`Field "${key}" is not defined by type "${type.name}".`);
      if (view !== null && typeof view === "object" && !Array.isArray(view)) {
        if (!(key in view)) throw unsupported();
        const v = view[key];
        const inner = getNamedType(type.getFields()[key].type);
        if (Array.isArray(v)) {
          const of = (field) => getNamedType(inner.getFields()[field].type);
          if (want.some) return v.some((x) => matches(x, want.some, of("some")));
          if (want.every) return v.every((x) => matches(x, want.every, of("every")));
          if ("length" in want) return matches(v.length, want.length, of("length"));
          return v.some((x) => matches(x, want, inner));
        }
        const relationNull = want !== null && typeof want === "object" && Object.keys(want).length === 1 && "null" in want;
        if (relationNull && (v === null || typeof v === "object")) return (v === null) === want.null;
        if (v !== null && typeof v === "object") return matches(v, want, inner);
        if (v === null && want !== null && typeof want === "object" && !Object.keys(want).some((k) => k in COMPARATORS)) return "null" in want ? want.null === true : false;
        return matches(v, want, inner);
      }
      const op = COMPARATORS[key];
      if (!op) throw unsupported();
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
    const now = clock.now();
    return Boolean(c.starts_at && c.ends_at && Date.parse(c.starts_at) <= now && now < Date.parse(c.ends_at));
  }

  // The records the settle step dates, by collection. Issues and comments carry the seed's own dates.
  const DATED = { organizations: ls.organizations, users: ls.users, teams: ls.teams, workflowStates: ls.workflowStates, issueLabels: ls.issueLabels, projects: ls.projects, cycles: ls.cycles };
  const earliest = (stamps) => stamps.filter(Boolean).sort()[0] ?? null;
  const latest = (stamps) => stamps.filter(Boolean).sort().at(-1) ?? null;
  const membersOf = (project) => ls.issues.findBy("project_id", project.linear_id);

  // Dates every team, user, label, project, cycle, workflow state and the organization once a seed's
  // or an update's records are all in. The store stamps a record with the wall clock on insert and on
  // every update, so the dates are written on the record itself, after the last update, and the ones
  // a record had before this add are taken from `before`, captured ahead of it.
  //   A record the add created takes the dates its file states (`stated`, already shifted); a date it
  //   leaves out is derived from the records around it:
  //     project       created: its earliest member issue's creation; updated: its latest member
  //                   issue's update or comment
  //     cycle         created: its start; updated: the later of its start and its latest member
  //                   issue's update
  //     user          the earliest issue they created or were assigned, or comment they wrote
  //     label         the earliest issue carrying it
  //     team, state   the team's earliest issue
  //     organization  the earliest creation of every record above
  //   A record with nothing to derive its creation from was created at the earliest issue, comment or
  //   cycle start the fake holds, else at the anchor, and last updated then too. An update is never
  //   earlier than the record's creation.
  //   A record that was there before keeps its creation date, and its update date moves only forward.
  function settle(before, stated) {
    const floor = earliest([...ls.issues.all().map((i) => i.created_at), ...ls.comments.all().map((c) => c.created_at), ...ls.cycles.all().map((c) => c.starts_at)]) ?? clock.anchor;
    const issuesBy = (pred) => ls.issues.all().filter(pred);
    const teamStart = (teamId) => earliest(issuesBy((i) => i.team_id === teamId).map((i) => i.created_at));
    const once = (created) => ({ created, updated: created });
    // Each kind's derived dates; null where the records around it hold nothing to derive one from.
    const derive = {
      users: (u) =>
        once(earliest([...issuesBy((i) => i.creator_id === u.linear_id || i.assignee_id === u.linear_id).map((i) => i.created_at), ...ls.comments.findBy("user_id", u.linear_id).map((c) => c.created_at)])),
      teams: (t) => once(teamStart(t.linear_id)),
      workflowStates: (x) => once(teamStart(x.team_id)),
      issueLabels: (x) => once(earliest(issuesBy((i) => i.label_ids.includes(x.linear_id)).map((i) => i.created_at))),
      projects: (p) => {
        const members = membersOf(p);
        return {
          created: earliest(members.map((i) => i.created_at)),
          updated: latest([...members.map((i) => i.updated_at), ...members.flatMap((i) => ls.comments.findBy("issue_id", i.linear_id).map((c) => c.created_at))]),
        };
      },
      cycles: (c) => ({ created: c.starts_at, updated: latest([c.starts_at, ...ls.issues.findBy("cycle_id", c.linear_id).map((i) => i.updated_at)]) }),
    };
    const date = (kind, rec, derived) => {
      const prior = before.get(`${kind}:${rec.id}`);
      if (prior) {
        rec.created_at = prior.created_at;
        rec.updated_at = latest([prior.updated_at, derived.updated]);
        return;
      }
      const own = stated.get(`${kind}:${rec.id}`) ?? {};
      rec.created_at = own.created ?? derived.created ?? floor;
      rec.updated_at = latest([rec.created_at, own.updated ?? derived.updated]);
    };
    for (const [kind, coll] of Object.entries(DATED)) if (kind !== "organizations") for (const rec of coll.all()) date(kind, rec, derive[kind](rec));
    const first = earliest([...Object.entries(DATED).filter(([kind]) => kind !== "organizations").flatMap(([, coll]) => coll.all().map((rec) => rec.created_at)), floor]);
    for (const rec of ls.organizations.all()) date("organizations", rec, once(first));
  }

  function add(cfg) {
    const before = new Map(Object.entries(DATED).flatMap(([kind, coll]) => coll.all().map((rec) => [`${kind}:${rec.id}`, { created_at: rec.created_at, updated_at: rec.updated_at }])));
    emulator.seedFromConfig(store, orgUrl, {
      organization: cfg.organization,
      users: cfg.users,
      teams: cfg.teams,
      labels: cfg.labels,
      projects: cfg.projects,
      cycles: (cfg.cycles ?? []).map((c) => ({ ...c, starts_at: clock.iso(c.starts_at) ?? undefined, ends_at: clock.iso(c.ends_at) ?? undefined })),
      tokens: cfg.tokens,
    });
    const need = (what, ref, rec) => {
      if (ref && !rec) throw new Error(`linear ${what} not found: ${ref}`);
      return rec?.linear_id ?? null;
    };
    // What the file states of the records this add created, which the emulator does not keep: their
    // dates for the settle step (by the same key as `before`: the store's update replaces a record's
    // object, as placing an issue does its team's, and keeps its id), and a project's lead and planned
    // dates.
    const stated = new Map();
    const isNew = (kind, rec) => rec && !before.has(`${kind}:${rec.id}`);
    const state = (kind, rec, entry) => {
      if (isNew(kind, rec)) stated.set(`${kind}:${rec.id}`, { created: clock.iso(entry.created_at), updated: clock.iso(entry.updated_at) });
    };
    // The organization may state only its creation; its last update is derived.
    if (cfg.organization) state("organizations", ls.organizations.all()[0], { created_at: cfg.organization.created_at });
    for (const u of cfg.users ?? []) state("users", ls.users.findOneBy("email", u.email), u);
    for (const t of cfg.teams ?? []) state("teams", ls.teams.findOneBy("key", t.key), t);
    for (const x of cfg.labels ?? []) {
      const teamId = x.team ? emulator.resolveTeam(store, x.team)?.linear_id ?? null : null;
      state("issueLabels", ls.issueLabels.all().find((l) => l.name === x.name && (l.team_id === teamId || l.team_id === null)), x);
    }
    for (const c of cfg.cycles ?? []) state("cycles", emulator.resolveCycle(store, c.name ?? String(c.number), emulator.resolveTeam(store, c.team)?.linear_id), c);
    for (const p of cfg.projects ?? []) {
      const rec = emulator.resolveProject(store, p.name);
      if (!isNew("projects", rec)) continue;
      state("projects", rec, p);
      rec.lead_id = need("user", p.lead, emulator.resolveUser(store, p.lead));
      rec.start_date = clock.date(p.start_date);
      rec.target_date = clock.date(p.target_date);
    }
    let issues = 0;
    for (const i of cfg.issues ?? []) {
      const team = emulator.resolveTeam(store, i.team);
      if (!team) throw new Error(`linear team not found: ${i.team}`);
      if (ls.issues.findOneBy("identifier", i.identifier)) throw new Error(`duplicate issue ${i.identifier}`);
      const state = emulator.resolveState(store, i.state, team.linear_id);
      if (!state) throw new Error(`linear state not found: ${i.state}`);
      const number = Number(i.identifier.split("-")[1]);
      const created = clock.iso(i.created_at);
      const updated = clock.iso(i.updated_at ?? i.created_at);
      const rec = ls.issues.insert({
        linear_id: i.id ?? crypto.createHash("sha1").update(i.identifier).digest("hex").replace(/^(.{8})(.{4})(.{4})(.{4})(.{12}).*/, "$1-$2-$3-$4-$5"),
        identifier: i.identifier,
        number,
        team_id: team.linear_id,
        title: i.title,
        description: i.description ?? null,
        priority: i.priority ?? 0,
        state_id: state.linear_id,
        assignee_id: need("user", i.assignee, emulator.resolveUser(store, i.assignee)),
        creator_id: need("user", i.creator, emulator.resolveUser(store, i.creator)),
        delegate_id: null,
        project_id: need("project", i.project, emulator.resolveProject(store, i.project)),
        cycle_id: need("cycle", i.cycle, emulator.resolveCycle(store, i.cycle, team.linear_id)),
        label_ids: (i.labels ?? []).map((x) => need("label", x, emulator.resolveLabel(store, x, team.linear_id))),
        url: `${orgUrl}/issue/${i.identifier}`,
        archived_at: null,
        canceled_at: null,
        completed_at: state.type === "completed" ? updated : null,
        started_at: state.type === "started" || state.type === "completed" ? created : null,
        due_date: clock.date(i.due_date),
        create_as_user: null,
        display_icon_url: null,
        estimate: i.estimate ?? null,
        parent_ref: null,
      });
      // The store's insert and update stamp these with the wall clock, so the seed's own are written
      // on the record itself; neither field is indexed.
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
        else if (r.type === "blocked_by") relations.insert({ linear_id: crypto.randomUUID(), type: "blocks", issue_id: other.linear_id, related_issue_id: self.linear_id });
        else relations.insert({ linear_id: crypto.randomUUID(), type: r.type, issue_id: self.linear_id, related_issue_id: other.linear_id });
      }
    }
    let comments = 0;
    for (const c of cfg.comments ?? []) {
      const issue = ls.issues.findOneBy("identifier", c.issue);
      if (!issue) throw new Error(`comment names an unknown issue ${c.issue}`);
      const rec = ls.comments.insert({
        linear_id: crypto.randomUUID(),
        issue_id: issue.linear_id,
        user_id: c.user ? need("user", c.user, emulator.resolveUser(store, c.user)) : null,
        body: c.body,
        create_as_user: null,
        display_icon_url: null,
      });
      rec.created_at = rec.updated_at = clock.iso(c.created_at);
      comments++;
    }
    settle(before, stated);
    return { linear_issues: issues, linear_comments: comments };
  }

  // The share of issues in a completed state, which is what an issue's completed_at marks.
  const completedShare = (items) => (items.length ? items.filter((i) => i.completed_at).length / items.length : 0);

  function root(me) {
    const by = (coll, id) => (id ? coll.findOneBy("linear_id", id) : undefined);
    const order = (items, orderBy) => {
      const key = orderBy === "updatedAt" ? "updated_at" : "created_at";
      return [...items].sort((a, b) => b[key].localeCompare(a[key]) || b.id - a.id);
    };
    // The views a filter is matched against. Related records and collections are read only when
    // the filter asks for them.
    const userView = (u) => (u ? { id: u.linear_id, name: u.name, displayName: u.display_name, email: u.email, active: u.active, admin: u.admin, isMe: u.linear_id === me?.linear_id } : null);
    const teamView = (t) => (t ? { id: t.linear_id, key: t.key, name: t.name, private: t.private } : null);
    const stateView = (x) => (x ? { id: x.linear_id, name: x.name, type: x.type, team: teamView(by(ls.teams, x.team_id)) } : null);
    const labelView = (x) => ({ id: x.linear_id, name: x.name, team: teamView(by(ls.teams, x.team_id)) });
    const projectView = (p) => (p ? { id: p.linear_id, name: p.name, state: p.state } : null);
    const cycleView = (c) => (c ? { id: c.linear_id, number: c.number, name: c.name, startsAt: c.starts_at, endsAt: c.ends_at, team: teamView(by(ls.teams, c.team_id)), isActive: isActiveCycle(c) } : null);
    const commentView = (c) => ({ id: c.linear_id, body: c.body, createdAt: c.created_at, updatedAt: c.updated_at, user: userView(by(ls.users, c.user_id)) });
    const issueView = (i, deep = true) => ({
      id: i.linear_id, identifier: i.identifier, number: i.number, title: i.title, description: i.description, priority: i.priority, estimate: i.estimate ?? null,
      createdAt: i.created_at, updatedAt: i.updated_at, completedAt: i.completed_at, startedAt: i.started_at, canceledAt: i.canceled_at, archivedAt: i.archived_at, dueDate: i.due_date,
      get team() { return teamView(by(ls.teams, i.team_id)); },
      get state() { return stateView(by(ls.workflowStates, i.state_id)); },
      get assignee() { return userView(by(ls.users, i.assignee_id)); },
      get creator() { return userView(by(ls.users, i.creator_id)); },
      get project() { return projectView(by(ls.projects, i.project_id)); },
      get cycle() { return cycleView(by(ls.cycles, i.cycle_id)); },
      get labels() { return i.label_ids.map((id) => by(ls.issueLabels, id)).filter(Boolean).map(labelView); },
      get parent() { return deep && i.parent_ref ? issueView(by(ls.issues, i.parent_ref), false) : null; },
      get children() { return deep ? ls.issues.all().filter((x) => x.parent_ref === i.linear_id).map((x) => issueView(x, false)) : []; },
      get comments() { return deep ? ls.comments.findBy("issue_id", i.linear_id).map(commentView) : []; },
      get hasBlockedByRelations() { return relations.findBy("related_issue_id", i.linear_id).some((r) => r.type === "blocks"); },
      get hasBlockingRelations() { return relations.findBy("issue_id", i.linear_id).some((r) => r.type === "blocks"); },
    });
    // The predicate a field's `filter` argument makes of a view.
    const where = (a, info, view) => {
      const { filter } = a;
      const type = filterTypeOf(info);
      return (x) => matches(view(x), filter, type);
    };
    // What a query is answered with. A field that is a function is resolved on demand with the
    // query's arguments, so a record's relations are read only when asked for.
    const fUser = (u) => u && {
      id: u.linear_id, name: u.name, displayName: u.display_name, email: u.email, active: u.active, admin: u.admin, isMe: u.linear_id === me?.linear_id,
      createdAt: u.created_at, updatedAt: u.updated_at, url: `${orgUrl}/profiles/${u.display_name}`,
      assignedIssues: issuesOf((i) => i.assignee_id === u.linear_id),
      createdIssues: issuesOf((i) => i.creator_id === u.linear_id),
    };
    const fState = (x) => x && { id: x.linear_id, name: x.name, type: x.type, position: x.position, color: "#95a2b3", team: () => fTeam(by(ls.teams, x.team_id)), createdAt: x.created_at, updatedAt: x.updated_at };
    const fLabel = (x) => x && { id: x.linear_id, name: x.name, color: x.color, description: x.description, team: () => fTeam(by(ls.teams, x.team_id)), createdAt: x.created_at, updatedAt: x.updated_at };
    const fCycle = (x) => x && {
      id: x.linear_id, number: x.number, name: x.name, startsAt: x.starts_at, endsAt: x.ends_at, createdAt: x.created_at, updatedAt: x.updated_at, team: () => fTeam(by(ls.teams, x.team_id)), issues: issuesOf((i) => i.cycle_id === x.linear_id),
      progress: () => completedShare(ls.issues.findBy("cycle_id", x.linear_id)),
    };
    // A project's planned dates are the seed's when it states them, else its member issues': it
    // starts when the first of them was started (when none was, when the project was created) and
    // targets the last due date among them. As in Linear, only a project in the completed or canceled
    // state has a completedAt or canceledAt: a completed one was completed when its last member issue
    // was (at its own last update with none), a canceled one at its last update.
    const fProject = (x) => x && {
      id: x.linear_id, name: x.name, description: x.description ?? "", state: x.state, url: `${orgUrl}/project/${x.linear_id}`, createdAt: x.created_at, updatedAt: x.updated_at,
      lead: () => fUser(by(ls.users, x.lead_id)),
      startDate: () => x.start_date ?? (earliest(membersOf(x).map((i) => i.started_at)) ?? x.created_at).slice(0, 10),
      targetDate: () => x.target_date ?? latest(membersOf(x).map((i) => i.due_date)),
      completedAt: () => (x.state === "completed" ? latest(membersOf(x).map((i) => i.completed_at)) ?? x.updated_at : null),
      canceledAt: () => (x.state === "canceled" ? x.updated_at : null),
      progress: () => completedShare(membersOf(x)),
      teams: (a, _context, info) => {
        const wanted = where(a, info, teamView);
        return connection(ls.teams.all().filter((t) => ls.issues.all().some((i) => i.project_id === x.linear_id && i.team_id === t.linear_id) && wanted(t)), a, fTeam);
      },
      issues: issuesOf((i) => i.project_id === x.linear_id),
    };
    const fTeam = (t) => t && {
      id: t.linear_id, key: t.key, name: t.name, description: t.description, private: t.private, createdAt: t.created_at, updatedAt: t.updated_at,
      issueCount: ({ includeArchived }) => ls.issues.findBy("team_id", t.linear_id).filter((i) => includeArchived || !i.archived_at).length,
      states: (a, _context, info) => connection(ls.workflowStates.findBy("team_id", t.linear_id).filter(where(a, info, stateView)).sort((x, y) => x.position - y.position), a, fState),
      members: (a, _context, info) => connection(ls.users.all().filter(where(a, info, userView)), a, fUser),
      labels: (a, _context, info) => connection(ls.issueLabels.all().filter((x) => !x.team_id || x.team_id === t.linear_id).filter(where(a, info, labelView)), a, fLabel),
      cycles: (a, _context, info) => connection(ls.cycles.findBy("team_id", t.linear_id).filter(where(a, info, cycleView)), a, fCycle),
      projects: (a, _context, info) => connection(ls.projects.all().filter((p) => ls.issues.all().some((i) => i.project_id === p.linear_id && i.team_id === t.linear_id)).filter(where(a, info, projectView)), a, fProject),
      activeCycle: () => fCycle(ls.cycles.findBy("team_id", t.linear_id).find(isActiveCycle)),
      issues: issuesOf((i) => i.team_id === t.linear_id),
    };
    const fComment = (c) => ({ id: c.linear_id, body: c.body, createdAt: c.created_at, updatedAt: c.updated_at, url: `${orgUrl}/issue/${by(ls.issues, c.issue_id).identifier}#comment-${c.linear_id.slice(0, 8)}`, user: () => fUser(by(ls.users, c.user_id)), issue: () => fIssue(by(ls.issues, c.issue_id)) });
    const fRelation = (r) => ({ id: r.linear_id, type: r.type, createdAt: by(ls.issues, r.issue_id).created_at, issue: () => fIssue(by(ls.issues, r.issue_id)), relatedIssue: () => fIssue(by(ls.issues, r.related_issue_id)) });
    const fIssue = (i) => i && {
      id: i.linear_id, identifier: i.identifier, number: i.number, title: i.title, description: i.description, priority: i.priority, priorityLabel: PRIORITY_LABELS[i.priority],
      estimate: i.estimate ?? null, url: i.url, branchName: `${i.identifier.toLowerCase()}-${i.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 40)}`,
      createdAt: i.created_at, updatedAt: i.updated_at, archivedAt: i.archived_at, canceledAt: i.canceled_at, completedAt: i.completed_at, startedAt: i.started_at, dueDate: i.due_date, trashed: null,
      team: () => fTeam(by(ls.teams, i.team_id)), state: () => fState(by(ls.workflowStates, i.state_id)), assignee: () => fUser(by(ls.users, i.assignee_id)), creator: () => fUser(by(ls.users, i.creator_id)),
      project: () => fProject(by(ls.projects, i.project_id)), cycle: () => fCycle(by(ls.cycles, i.cycle_id)), parent: () => fIssue(by(ls.issues, i.parent_ref)),
      children: issuesOf((x) => x.parent_ref === i.linear_id),
      labels: (a, _context, info) => connection(i.label_ids.map((id) => by(ls.issueLabels, id)).filter(Boolean).filter(where(a, info, labelView)), a, fLabel),
      comments: (a, _context, info) => connection(ls.comments.findBy("issue_id", i.linear_id).filter(where(a, info, commentView)).sort((x, y) => x.created_at.localeCompare(y.created_at)), a, fComment),
      relations: (a) => connection(relations.findBy("issue_id", i.linear_id), a, fRelation),
      inverseRelations: (a) => connection(relations.findBy("related_issue_id", i.linear_id), a, fRelation),
    };
    const issues = (a, info, pred = () => true) => {
      const { includeArchived, orderBy } = a;
      const wanted = where(a, info, issueView);
      return connection(order(ls.issues.all().filter((i) => (includeArchived || !i.archived_at) && pred(i) && wanted(i)), orderBy), a, fIssue);
    };
    function issuesOf(pred) {
      return (a, _context, info) => issues(a, info, pred);
    }
    const one = (what, rec, format) => {
      if (!rec) throw notFoundError(what);
      return format(rec);
    };
    const text = (i) => `${i.identifier} ${i.title} ${i.description ?? ""}`.toLowerCase();
    return {
      viewer: () => fUser(me),
      organization: () => {
        const o = ls.organizations.all()[0];
        return { id: o.linear_id, name: o.name, urlKey: o.url_key, createdAt: o.created_at, updatedAt: o.updated_at };
      },
      teams: (a, _context, info) => connection(ls.teams.all().filter(where(a, info, teamView)), a, fTeam),
      team: ({ id }) => one("Team", emulator.resolveTeam(store, id), fTeam),
      users: (a, _context, info) => connection(ls.users.all().filter(where(a, info, userView)), a, fUser),
      user: ({ id }) => one("User", by(ls.users, id), fUser),
      issues: issuesOf(),
      issue: ({ id }) => one("Issue", emulator.resolveIssue(store, id), fIssue),
      issueSearch: (a, _context, info) => {
        const query = (a.query ?? "").toLowerCase();
        return issues(a, info, (i) => text(i).includes(query));
      },
      searchIssues: (a, _context, info) => {
        const words = a.term.toLowerCase().split(/\s+/);
        return issues(a, info, (i) => words.every((w) => text(i).includes(w)));
      },
      projects: (a, _context, info) => connection(order(ls.projects.all().filter(where(a, info, projectView)), a.orderBy), a, fProject),
      project: ({ id }) => one("Project", emulator.resolveProject(store, id), fProject),
      cycles: (a, _context, info) => connection(ls.cycles.all().filter(where(a, info, cycleView)), a, fCycle),
      cycle: ({ id }) => one("Cycle", by(ls.cycles, id), fCycle),
      issueLabels: (a, _context, info) => connection(ls.issueLabels.all().filter(where(a, info, labelView)), a, fLabel),
      workflowStates: (a, _context, info) => connection(ls.workflowStates.all().filter(where(a, info, stateView)), a, fState),
      comments: (a, _context, info) => connection(order(ls.comments.all().filter(where(a, info, (c) => ({ ...commentView(c), issue: issueView(by(ls.issues, c.issue_id), false) }))), a.orderBy), a, fComment),
    };
  }

  // Each request's unsupported keys, by the request, for the access log and the counts.
  const unsupportedOf = new WeakMap();
  // Every request for /graphql, GET with the document in the URL or POST with it in the body, goes
  // through the public schema: validated, then a query is answered by root() and a mutation or
  // subscription refused and counted. No request reaches the emulator's own schema.
  async function route(req, url) {
    const token = tokenOf(req);
    const record = token ? ls.tokens.findOneBy("token", token) : undefined;
    if (!record) return authFailure();
    if (url.pathname !== "/graphql") return Response.json({ message: "Not Found", documentation_url: DOCS }, { status: 404 });
    const { body, error } = await graphqlBody(req, url);
    if (error) return Response.json({ errors: [{ message: error }] }, { status: 400 });
    let doc;
    try {
      doc = parse(body.query ?? "");
    } catch (e) {
      return Response.json({ errors: [{ message: e.message, extensions: { code: "GRAPHQL_PARSE_FAILED" } }] }, { status: 400 });
    }
    // Real Linear answers a query it cannot validate with 400 and a partially failed one with 200.
    const invalid = validate(schema, doc);
    if (invalid.length) return Response.json({ errors: invalid }, { status: 400 });
    // The operation execute() would run; none when it would refuse the request, which it then does.
    const op = getOperationAST(doc, body.operationName);
    const unsupported = new Set();
    if (op && op.operation !== "query") {
      // The operation's root fields, including those a fragment selects.
      const fragments = new Map(doc.definitions.filter((d) => d.kind === "FragmentDefinition").map((d) => [d.name.value, d]));
      const addRootFields = (selectionSet) => {
        for (const selection of selectionSet.selections) {
          if (selection.kind === "Field") unsupported.add(`linear:${op.operation}:${selection.name.value}`);
          else if (selection.kind === "InlineFragment") addRootFields(selection.selectionSet);
          else addRootFields(fragments.get(selection.name.value).selectionSet);
        }
      };
      addRootFields(op.selectionSet);
      unsupportedOf.set(req, [...unsupported]);
      return forbidden(op.operation);
    }
    const me = ls.users.findOneBy("linear_id", record.user_id);
    const result = await execute({ schema, document: doc, rootValue: root(me), contextValue: { unsupported }, fieldResolver, variableValues: body.variables, operationName: body.operationName });
    unsupportedOf.set(req, [...unsupported]);
    // A request execute() refuses before running it (a variable missing or of the wrong type, an
    // operation it cannot pick) has no data, and is a 400 as an invalid query is.
    return Response.json(result, { status: "data" in result ? 200 : 400 });
  }

  return {
    name,
    host,
    store,
    tokenUsers,
    add,
    route,
    notFound: () => Response.json({ errors: [{ message: "Not Found" }] }, { status: 404 }),
    counts: () => ({
      users: ls.users.all().length,
      teams: ls.teams.all().length,
      projects: ls.projects.all().length,
      cycles: ls.cycles.all().length,
      labels: ls.issueLabels.all().length,
      issues: ls.issues.all().length,
      comments: ls.comments.all().length,
      relations: relations.all().length,
    }),
    outcome: (json) => ({ ok: json ? !json.errors : null, error: json?.errors?.[0]?.message ?? null, result_count: resultCount(json) }),
    unsupportedKeys: (url, json, req) => {
      const keys = unsupportedOf.get(req) ?? [];
      // A filter key the evaluator refuses after validation passed is one Linear has and the fake
      // lacks; the same message before execution (no data) is Linear's own refusal of a key it does
      // not have.
      if (json && "data" in json)
        for (const { message = "" } of json.errors ?? []) {
          const filter = message.match(/^Field "([^"]+)" is not defined by type "([^"]+)"/);
          if (filter) keys.push(`linear:filter:${filter[2]}.${filter[1]}`);
        }
      return keys;
    },
  };
}

function resultCount(json) {
  if (json == null || typeof json !== "object") return null;
  let n = null;
  const walk = (v) => {
    if (Array.isArray(v?.nodes)) n = (n ?? 0) + v.nodes.length;
    if (v && typeof v === "object") for (const x of Object.values(v)) walk(x);
  };
  walk(json.data);
  return n;
}
