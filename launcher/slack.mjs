// The fake Slack: @emulators/slack's app and store, with what a trial needs on top. Messages, threads,
// direct messages and ids are inserted straight into the store, so they read the same in every trial;
// channels, direct messages and users are dated, and channels peopled, from the seed's messages
// (settle()); Slack methods answer GET; conversations.history takes oldest/latest/inclusive;
// search.messages reads the live store; a permalink names the workspace's own domain. See
// launcher.mjs for the interface this module exports.
import crypto from "node:crypto";
import { Hono, Store, WebhookDispatcher, authMiddleware, cors, createApiErrorHandler, createErrorHandler } from "@emulators/core";
import { getSlackStore, seedFromConfig, slackPlugin } from "@emulators/slack";

export const name = "slack";
export const hosts = new Set(["slack.com"]);
const REAL_URL = "https://slack.com";
// What an error answer points its reader at. Nothing a caller is sent names what is serving it.
const DOCS = "https://api.slack.com/methods";

// Slack's limits, per method and per workspace and app, as its documentation states them
// (docs.slack.dev/apis/web-api/rate-limits and each method's page, read 2026-10-29): Tier 1 "1+ per
// minute", Tier 2 "20+", Tier 3 "50+", Tier 4 "100+". A tier allows its number within any minute plus
// a burst, which Slack does not publish; BURST is the share added for it. The method pages state:
// conversations.history, conversations.replies, conversations.info and users.lookupByEmail Tier 3;
// conversations.list, users.list and search.messages Tier 2; users.info and conversations.members
// Tier 4; auth.test "special" ("hundreds of requests per minute"), held to Tier 4 here. Since
// 2025-05-29 conversations.history and conversations.replies are Tier 1 for a commercially
// distributed app outside the Slack Marketplace; an internal customer-built app keeps Tier 3, and a
// task's integration is one. Any other method is held to Tier 3.
const PER_MINUTE = { 1: 1, 2: 20, 3: 50, 4: 100 };
const BURST = 0.2;
const METHOD_TIERS = {
  "conversations.history": 3,
  "conversations.replies": 3,
  "conversations.list": 2,
  "conversations.info": 3,
  "conversations.members": 4,
  "users.list": 2,
  "users.info": 4,
  "users.lookupByEmail": 3,
  "search.messages": 2,
  "auth.test": 4,
};
const tier = (n) => ({ limit: PER_MINUTE[n] + Math.ceil(PER_MINUTE[n] * BURST), windowSeconds: 60, algorithm: "window" });
// A request's token, from the header or, as Slack also takes it, a query or form parameter.
async function tokenOf(req, url) {
  const header = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
  if (header) return header;
  if (url.searchParams.get("token")) return url.searchParams.get("token");
  if (req.method === "GET" || (req.headers.get("content-type") ?? "").includes("json")) return "";
  return new URLSearchParams(await req.clone().text()).get("token") ?? "";
}
// See rate_limits.mjs for what a policy holds. One fake is one workspace, so a method's window is
// shared by every token the seed names. A request with no seeded token is refused by authentication,
// not counted.
export const limits = {
  buckets: { ...Object.fromEntries(Object.entries(METHOD_TIERS).map(([method, n]) => [method, tier(n)])), other: tier(3) },
  async bucketOf(req, url, service) {
    if (!url.pathname.startsWith("/api/") || !service.tokenUsers.has(await tokenOf(req, url))) return [];
    const method = url.pathname.slice(5);
    return [{ bucket: method in METHOD_TIERS ? method : "other", scope: method, cost: 1 }];
  },
  refuse: () => Response.json({ ok: false, error: "ratelimited" }, { status: 429 }),
};

const unknownMethod = (status) => Response.json({ ok: false, error: "unknown_method" }, { status });
// Stable ids: an id derived from the name survives a re-seed and reads the same in every trial.
const slackId = (prefix, text) => prefix + crypto.createHash("sha1").update(text).digest("hex").slice(0, 10).toUpperCase();
const isPage = (res) => (res.headers.get("content-type") ?? "").includes("text/html");
async function answerOf(res) {
  try {
    return await res.clone().json();
  } catch {
    return null;
  }
}

export function create(seed, clock) {
  const app = new Hono();
  const store = new Store();
  app.onError(createApiErrorHandler(DOCS));
  app.use("*", cors());
  app.use("*", createErrorHandler(DOCS));
  // No fallback user: a token the seed does not name is anonymous.
  app.use("*", authMiddleware(new Map(), undefined, undefined));
  slackPlugin.register(app, store, new WebhookDispatcher(), REAL_URL, new Map());
  app.notFound((c) => c.json({ message: "Not Found", documentation_url: DOCS }, 404));
  const ss = getSlackStore(store);
  const domain = seed.team?.domain ?? "workspace";
  const tokenUsers = new Map((seed.tokens ?? []).map((entry) => [entry.token, entry.user]));

  const isoOfTs = (ts) => new Date(Number(ts) * 1000).toISOString();
  const secondsOf = (iso) => Math.floor(Date.parse(iso) / 1000);
  const byTs = (a, b) => Number(a.ts) - Number(b.ts);

  // Dates the channels and users, and gives the channels their members, once a seed's or an
  // update's records are all in: the emulator stamps both with the wall clock and makes every user a
  // member of every channel.
  //   A channel's members are the users who posted in it plus those its `members` names, for a public
  //   channel as for a private one; its creator is its first poster (else the first user it names,
  //   else the first seeded user). A new channel or dm was created, and its topic and purpose set, at
  //   the date its file states, else at its first message, else at the earliest message the fake
  //   holds, else at the anchor.
  //   A new user was last updated at the date their file states as `created`, else at their first
  //   message, else at the earliest message; their last activity is their latest message.
  //   A channel or user that was there before keeps its dates. Reading a public channel needs no
  //   membership, as with a Slack user token, so membership changes what a channel lists, not who
  //   may read it.
  function settle(newConversations, newUsers, statedMembers) {
    const messages = ss.messages.all().sort(byTs);
    const floor = messages[0] ? isoOfTs(messages[0].ts) : clock.anchor;
    for (const m of messages) m.created_at = m.updated_at = isoOfTs(m.ts);
    for (const ch of ss.channels.all()) {
      const posted = messages.filter((m) => m.channel_id === ch.channel_id);
      const named = statedMembers.get(ch.channel_id) ?? [];
      const members = [...new Set([...ch.members, ...named, ...posted.map((m) => m.user)])];
      const stated = newConversations.get(ch.channel_id);
      const creator = stated ? posted[0]?.user ?? named[0] ?? ss.users.all()[0]?.user_id ?? null : ch.creator;
      const created = stated ? clock.iso(stated.created) ?? (posted[0] ? isoOfTs(posted[0].ts) : floor) : ch.created_at;
      Object.assign(ch, {
        members,
        num_members: members.length,
        creator,
        topic: stated ? { ...ch.topic, creator, last_set: secondsOf(created) } : ch.topic,
        purpose: stated ? { ...ch.purpose, creator, last_set: secondsOf(created) } : ch.purpose,
        created_at: created,
        updated_at: created,
      });
    }
    for (const u of ss.users.all()) {
      const own = messages.filter((m) => m.user === u.user_id);
      const fresh = newUsers.get(u.name);
      const updated = fresh ? clock.iso(fresh.created) ?? (own[0] ? isoOfTs(own[0].ts) : floor) : u.created_at;
      Object.assign(u, { created_at: updated, updated_at: updated, last_activity: own.length ? secondsOf(isoOfTs(own.at(-1).ts)) : secondsOf(updated) });
    }
  }

  const isDm = (ch) => Boolean(ch.is_im || ch.is_mpim);
  // A channel by its name: a direct message's name is no channel's, so it never answers to one.
  const namedChannel = (name) => ss.channels.all().find((ch) => !isDm(ch) && ch.name === name);
  const dmOfKey = (key) => ss.channels.all().find((ch) => isDm(ch) && ch.seed_key === key);
  const sameMembers = (a, b) => a.length === b.length && [...a].sort().join(",") === [...b].sort().join(",");

  function add(cfg) {
    const dms = cfg.dms ?? [];
    const dmKeys = new Set(dms.map((d) => d.key));
    for (const c of cfg.channels ?? []) {
      if (dmKeys.has(c.name) || dmOfKey(c.name)) throw new Error(`slack channel ${c.name} takes the key of a dm`);
      // The package finds a channel to keep by name alone, a direct message's included.
      if (!namedChannel(c.name) && ss.channels.all().some((ch) => isDm(ch) && ch.name === c.name)) throw new Error(`slack channel ${c.name} takes the name a dm is served under`);
    }
    for (const d of dms) if ((cfg.channels ?? []).some((c) => c.name === d.key) || namedChannel(d.key)) throw new Error(`slack dm ${d.key} takes the name of a channel`);
    const newChannelNames = (cfg.channels ?? []).filter((c) => !namedChannel(c.name)).map((c) => c.name);
    const newUsers = new Map((cfg.users ?? []).filter((u) => !ss.users.all().some((x) => x.name === u.name)).map((u) => [u.name, u]));
    // The emulator only renames a team it already has, and answers for a missing one under its own name.
    if (!ss.teams.all()[0]) ss.teams.insert({ team_id: "T" + slackId("", domain), name: cfg.team?.name ?? domain, domain });
    seedFromConfig(store, REAL_URL, {
      team: cfg.team,
      users: (cfg.users ?? []).map((u) => ({ name: u.name, real_name: u.real_name, email: u.email ?? `${u.name}@${domain}.example`, profile: { title: u.title ?? "" } })),
      channels: (cfg.channels ?? []).map((c) => ({ name: c.name, topic: c.topic, is_private: c.is_private })),
      tokens: cfg.tokens,
    });
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
      const rec = namedChannel(c.name);
      const id = c.id ?? slackId("C", c.name);
      if (rec.channel_id !== id) ss.channels.update(rec.id, { channel_id: id });
    }
    const user = (ref) => (ss.users.all().find((x) => x.name === ref) ?? ss.users.findOneBy("user_id", ref))?.user_id;
    // What a message names its conversation by: a dm's key, a channel's name, or either's id.
    const conversation = (ref) => dmOfKey(ref) ?? namedChannel(ref) ?? ss.channels.findOneBy("channel_id", ref);
    // The records settle() dates afresh, and the members each states, by channel id. A new channel
    // starts with no members: settle() gives it its posters and the users it names.
    const newConversations = new Map();
    const statedMembers = new Map();
    for (const name of newChannelNames) {
      const rec = namedChannel(name);
      rec.members = [];
      newConversations.set(rec.channel_id, (cfg.channels ?? []).find((c) => c.name === name));
    }
    const memberIds = (what, refs) =>
      refs.map((ref) => {
        const id = user(ref);
        if (!id) throw new Error(`slack ${what} names an unknown member ${ref}`);
        return id;
      });
    for (const c of cfg.channels ?? []) statedMembers.set(namedChannel(c.name).channel_id, memberIds(`channel ${c.name}`, c.members ?? []));
    // A direct message is Slack's im between two members or its mpim among three or more, read by its
    // members alone, and one at most for a set of members, as Slack keeps it. It is listed only to a
    // token's user, so one with no such member could never be read and is refused. One the trial
    // opened already (conversations.open) takes the dm's key and keeps its id.
    const tokenIds = [...tokenUsers.values()].map(user).filter(Boolean);
    const team = ss.teams.all()[0];
    for (const d of dms) {
      const what = `dm ${d.key}`;
      if (typeof d.key !== "string" || !d.key) throw new Error(`slack dm needs a key: ${JSON.stringify(d)}`);
      if (dmOfKey(d.key)) throw new Error(`slack ${what} is listed again`);
      if (!Array.isArray(d.members) || d.members.length < 2 || new Set(d.members).size !== d.members.length) throw new Error(`slack ${what} needs two or more distinct members`);
      const ids = memberIds(what, d.members);
      if (!ids.some((id) => tokenIds.includes(id))) throw new Error(`slack ${what} has no member a token belongs to, so nothing could read it`);
      const isMpim = ids.length > 2;
      const held = ss.channels.all().find((ch) => isDm(ch) && sameMembers(ch.members, ids));
      if (held?.seed_key) throw new Error(`slack ${what} has the members of dm ${held.seed_key}`);
      if (held) {
        ss.channels.update(held.id, { seed_key: d.key });
        continue;
      }
      // An im is served under its other party's id (the member no token belongs to, else the
      // second), as Slack names an im in a search match; an mpim under Slack's mpdm- name.
      const other = ids.find((id) => !tokenIds.includes(id)) ?? ids[1];
      const names = ids.map((id) => ss.users.findOneBy("user_id", id).name);
      const rec = ss.channels.insert({
        channel_id: slackId(isMpim ? "G" : "D", [...ids].sort().join("\n")),
        team_id: team.team_id,
        name: isMpim ? `mpdm-${names.join("--")}-1` : other,
        is_channel: false,
        is_private: true,
        is_im: !isMpim,
        is_mpim: isMpim,
        is_open_by_user: Object.fromEntries(ids.map((id) => [id, true])),
        user: isMpim ? undefined : other,
        is_archived: false,
        topic: { value: "", creator: "", last_set: 0 },
        purpose: { value: "", creator: "", last_set: 0 },
        members: ids,
        creator: ids[0],
        num_members: ids.length,
        last_read: {},
        seed_key: d.key,
      });
      newConversations.set(rec.channel_id, d);
      statedMembers.set(rec.channel_id, ids);
    }
    let n = 0;
    for (const m of cfg.messages ?? []) {
      const ch = conversation(m.channel);
      const uid = user(m.user);
      if (!ch || !uid) throw new Error(`slack message refers to unknown channel/user: ${m.channel}/${m.user}`);
      if (isDm(ch) && !ch.members.includes(uid)) throw new Error(`slack message ${m.channel}/${m.ts} is from ${m.user}, who is no member of that dm`);
      const channel_id = ch.channel_id;
      const ts = clock.slackTs(m.ts);
      const thread_ts = clock.slackTs(m.thread_ts) ?? undefined;
      const p = thread_ts && thread_ts !== ts ? ss.messages.all().find((x) => x.channel_id === channel_id && x.ts === thread_ts) ?? null : null;
      if (thread_ts && thread_ts !== ts) {
        if (!p) throw new Error(`thread parent missing for ${m.channel}/${m.ts}`);
        // conversations.replies serves a thread parent first, then its replies oldest first.
        if (Number(ts) <= Number(thread_ts)) throw new Error(`slack reply ${m.channel}/${m.ts} is no later than its parent ${m.thread_ts}`);
      }
      ss.messages.insert({ ts, channel_id, user: uid, text: m.text, type: "message", thread_ts, reply_count: 0, reply_users: [], reactions: [] });
      if (p) {
        ss.messages.update(p.id, {
          thread_ts: p.ts,
          reply_count: p.reply_count + 1,
          reply_users: p.reply_users.includes(uid) ? p.reply_users : [...p.reply_users, uid],
          latest_reply: ts,
        });
      }
      n++;
    }
    settle(newConversations, newUsers, statedMembers);
    return { slack_messages: n };
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
    app.fetch(
      new Request(new URL(`/api/${method}`, req.url), {
        method: "POST",
        headers: { authorization: req.headers.get("authorization") ?? "", "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(body).toString(),
      }),
    );
  async function history(req, p) {
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
  // A message's permalink as Slack serves it: on the workspace's own domain, and a reply's naming its
  // thread's parent and channel.
  const permalinkOf = (channelId, m) => {
    const link = `https://${domain}.slack.com/archives/${channelId}/p${m.ts.replace(".", "")}`;
    return m.thread_ts && m.thread_ts !== m.ts ? `${link}?${new URLSearchParams({ thread_ts: m.thread_ts, cid: channelId })}` : link;
  };
  // A date of search's, as [start, end) in epoch seconds: a day (YYYY-MM-DD, today, yesterday), a
  // month (YYYY-MM) or a year (YYYY), read in UTC, which is the zone of every seeded user. today is
  // the trial's present: under a time shift the seed's data ends the day before it.
  function spanOf(value) {
    const DAY = 86400;
    const now = new Date(clock.now());
    const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) / 1000;
    const lower = value.toLowerCase();
    if (lower === "today") return [today, today + DAY];
    if (lower === "yesterday") return [today - DAY, today];
    let m = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (m) {
      const start = Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000;
      return [start, start + DAY];
    }
    m = value.match(/^(\d{4})-(\d{2})$/);
    if (m) return [Date.UTC(+m[1], +m[2] - 1, 1) / 1000, Date.UTC(+m[1], +m[2], 1) / 1000];
    m = value.match(/^(\d{4})$/);
    return m ? [Date.UTC(+m[1], 0, 1) / 1000, Date.UTC(+m[1] + 1, 0, 1) / 1000] : null;
  }
  // Slack's search operators the fake does not read. A term naming one, an excluded term (-word), a
  // wildcard (word*) or a date the fake cannot read matches nothing and is counted under
  // slack:search.messages:<operator>, exclude or wildcard.
  const UNREAD_OPERATORS = new Set(["to", "with", "has", "is"]);
  const unreadShapeOf = (raw) => {
    if (raw.startsWith('"') || raw.length < 2) return null;
    if (raw.startsWith("-")) return "exclude";
    return raw.endsWith("*") ? "wildcard" : null;
  };
  const unsupportedOf = new WeakMap();
  // search.messages over the live store, so a message posted in the trial is found as a seeded one
  // is. Words and phrases match the text without regard to case; in:#channel, in:<channel id> and
  // in:@user (the token's user's im with them); from:user or from:<@id>; after:, before:, on: and
  // during: over the dates spanOf() reads, after: and before: excluding the day they name. Sorted by
  // score (the default: how many times the text holds the query's words and phrases, ties newest
  // first) or by timestamp, in sort_dir (desc by default); count (20, at most 100) per page, page (at
  // most 100).
  async function search(req, p) {
    const auth = await (await asPost(req, "auth.test", {})).json();
    if (!auth.ok) return Response.json(auth);
    const keys = [];
    unsupportedOf.set(req, keys);
    const preds = [];
    const words = [];
    const dated = (op, value, test) => {
      const span = spanOf(value);
      if (!span) keys.push(`slack:search.messages:${op}`);
      preds.push((m) => span !== null && test(Number(m.ts), span));
    };
    for (const raw of String(p.query ?? "").match(/"[^"]+"|\S+/g) ?? []) {
      const term = raw.replace(/^"|"$/g, "");
      const lower = term.toLowerCase();
      const op = raw.startsWith('"') ? null : lower.match(/^([a-z]+):(.+)$/);
      const shape = unreadShapeOf(raw);
      if (shape) {
        keys.push(`slack:search.messages:${shape}`);
        preds.push(() => false);
      } else if (op?.[1] === "in") {
        const target = term.slice(3);
        let ch;
        if (target.startsWith("@")) {
          const who = ss.users.all().find((x) => x.name === target.slice(1))?.user_id;
          ch = ss.channels.all().find((c) => c.is_im && c.members.includes(auth.user_id) && c.members.includes(who));
        } else ch = namedChannel(target.replace(/^#/, "")) ?? ss.channels.findOneBy("channel_id", target.replace(/^<#|(\|[^>]*)?>$/g, ""));
        preds.push((m) => m.channel_id === ch?.channel_id);
      } else if (op?.[1] === "from") {
        const who = term.slice(5).replace(/^[<@]+|>$/g, "");
        const u = ss.users.all().find((x) => x.name === who) ?? ss.users.findOneBy("user_id", who);
        preds.push((m) => m.user === u?.user_id);
      } else if (op?.[1] === "after") dated("after", term.slice(6), (ts, [, end]) => ts >= end);
      else if (op?.[1] === "before") dated("before", term.slice(7), (ts, [start]) => ts < start);
      else if (op?.[1] === "on" || op?.[1] === "during") dated(op[1], term.slice(op[1].length + 1), (ts, [start, end]) => ts >= start && ts < end);
      else if (op && UNREAD_OPERATORS.has(op[1])) {
        keys.push(`slack:search.messages:${op[1]}`);
        preds.push(() => false);
      } else {
        words.push(lower);
        preds.push((m) => m.text.toLowerCase().includes(lower));
      }
    }
    const visible = new Map(ss.channels.all().filter((c) => !c.is_private || c.members.includes(auth.user_id)).map((c) => [c.channel_id, c]));
    const scoreOf = (m) => words.reduce((sum, w) => sum + m.text.toLowerCase().split(w).length - 1, 0);
    const dir = p.sort_dir === "asc" ? 1 : -1;
    const byTime = (a, b) => dir * (Number(a.ts) - Number(b.ts));
    const order = p.sort === "timestamp" ? byTime : (a, b) => dir * (scoreOf(a) - scoreOf(b)) || -(Number(a.ts) - Number(b.ts));
    const hits = ss.messages.all().filter((m) => visible.has(m.channel_id) && preds.every((f) => f(m))).sort(order);
    const count = Math.min(Math.max(Number(p.count) || 20, 1), 100);
    const page = Math.min(Math.max(Number(p.page) || 1, 1), 100);
    const slice = hits.slice((page - 1) * count, page * count);
    const pages = Math.ceil(hits.length / count);
    const team = ss.teams.all()[0]?.team_id;
    return Response.json({
      ok: true,
      query: p.query ?? "",
      messages: {
        total: hits.length,
        paging: { count, total: hits.length, page, pages },
        pagination: { total_count: hits.length, page, per_page: count, page_count: pages, first: Math.min((page - 1) * count + 1, hits.length), last: Math.min(page * count, hits.length) },
        matches: slice.map((m) => {
          const ch = visible.get(m.channel_id);
          // An im is named by its other party, as the searching user sees it.
          const channel = { id: ch.channel_id, is_channel: Boolean(ch.is_channel), is_group: Boolean(ch.is_private && !isDm(ch)), is_im: Boolean(ch.is_im), is_mpim: Boolean(ch.is_mpim), is_private: Boolean(ch.is_private), is_shared: false, is_ext_shared: false, is_org_shared: false, is_pending_ext_shared: false, pending_shared: [], name: ch.is_im ? ch.members.find((id) => id !== auth.user_id) ?? ch.name : ch.name };
          return { type: "message", channel, user: m.user, username: ss.users.findOneBy("user_id", m.user)?.name ?? "", ts: m.ts, text: m.text, team, permalink: permalinkOf(ch.channel_id, m) };
        }),
      },
    });
  }
  // The emulator's user objects lack Slack's `updated`, which the answers of these methods gain from
  // the store (the date settle() gave the user), under the key each answer holds its users in.
  const USER_ANSWERS = { "users.list": "members", "users.info": "user", "users.lookupByEmail": "user" };
  async function withUpdated(res, key) {
    const answer = await answerOf(res);
    if (!answer?.ok || !answer[key]) return res;
    const stamped = (u) => ({ ...u, updated: secondsOf(ss.users.findOneBy("user_id", u.id).updated_at) });
    answer[key] = Array.isArray(answer[key]) ? answer[key].map(stamped) : stamped(answer[key]);
    const headers = new Headers(res.headers);
    headers.delete("content-length");
    return new Response(JSON.stringify(answer), { status: res.status, headers });
  }
  // The package links a permalink to slack.com itself; Slack links it to the workspace's domain.
  async function withPermalink(res) {
    const answer = await answerOf(res);
    if (!answer?.ok) return res;
    answer.permalink = answer.permalink.replace(/^https:\/\/[^/]+/, `https://${domain}.slack.com`);
    return Response.json(answer, { status: res.status });
  }
  // A reply posted in the trial makes its parent a thread's parent, which Slack serves with its own
  // ts as thread_ts and the newest reply as latest_reply; the package counts the reply alone.
  async function markThreadParent(res) {
    const answer = await answerOf(res);
    const thread_ts = answer?.ok ? answer.message?.thread_ts : undefined;
    if (!thread_ts || thread_ts === answer.ts) return;
    const parent = ss.messages.all().find((m) => m.channel_id === answer.channel && m.ts === thread_ts);
    if (parent) ss.messages.update(parent.id, { thread_ts: parent.ts, latest_reply: answer.ts });
  }
  // Slack serves a thread's parent first, then its replies oldest first. The package orders all of
  // them by ts, and a reply posted in a trial whose seed is shifted past the box's clock is stamped
  // before its parent.
  async function withParentFirst(res, ts) {
    const answer = await answerOf(res);
    if (!answer?.ok) return res;
    answer.messages = [...answer.messages.filter((m) => m.ts === ts), ...answer.messages.filter((m) => m.ts !== ts)];
    return Response.json(answer, { status: res.status });
  }
  async function apiRoute(req, url) {
    const method = url.pathname.slice(5);
    const p = await params(req);
    if (method === "search.messages") return search(req, p);
    if (method === "conversations.history" && (p.oldest || p.latest)) return history(req, p);
    const res = await (req.method === "GET" ? asPost(req, method, Object.fromEntries(url.searchParams)) : app.fetch(req));
    if (method in USER_ANSWERS) return withUpdated(res, USER_ANSWERS[method]);
    if (method === "chat.getPermalink") return withPermalink(res);
    if (method === "conversations.replies") return withParentFirst(res, p.ts);
    if (method === "chat.postMessage") await markThreadParent(res);
    return res;
  }
  async function route(req, url) {
    const isMethod = url.pathname.startsWith("/api/");
    const res = await (isMethod ? apiRoute(req, url) : app.fetch(req));
    // The package underneath serves pages of its own (an inspector, sign-in screens) that say what
    // they are. Real Slack serves no page at an API path, so a page becomes a 404.
    if (isPage(res)) return unknownMethod(404);
    // Slack answers a method it does not have with a 200 that says so, where the package underneath
    // answers a path it does not route with a 404 of its own.
    if (isMethod && res.status === 404 && (await answerOf(res))?.ok === undefined) return unknownMethod(200);
    return res;
  }

  return {
    name,
    hosts,
    store,
    tokenUsers,
    add,
    route,
    notFound: () => unknownMethod(404),
    counts: () => ({ users: ss.users.all().length, channels: ss.channels.all().filter((ch) => !isDm(ch)).length, dms: ss.channels.all().filter(isDm).length, messages: ss.messages.all().length }),
    outcome: (json) => ({ ok: json?.ok ?? null, error: json?.error ?? null, result_count: resultCount(json) }),
    unsupportedKeys: (url, json, req) => (json?.error === "unknown_method" ? [`slack:${url.pathname.replace(/^\/api\//, "")}`] : [...new Set(unsupportedOf.get(req) ?? [])]),
  };
}

function resultCount(json) {
  if (json == null || typeof json !== "object") return null;
  for (const k of ["messages", "channels", "members", "files", "items"]) {
    if (Array.isArray(json[k])) return json[k].length;
    if (Array.isArray(json[k]?.matches)) return json[k].matches.length;
  }
  return null;
}
