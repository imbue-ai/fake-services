// The fake Slack: @emulators/slack's app and store, with what a trial needs on top. Messages, threads
// and ids are inserted straight into the store, so they read the same in every trial; channels and
// users are dated, and channels peopled, from the seed's messages (settle()); Slack methods answer
// GET; conversations.history takes oldest/latest/inclusive; a basic search.messages. See launcher.mjs
// for the interface this module exports.
import crypto from "node:crypto";
import { Hono, Store, WebhookDispatcher, authMiddleware, cors, createApiErrorHandler, createErrorHandler } from "@emulators/core";
import { getSlackStore, seedFromConfig, slackPlugin } from "@emulators/slack";

export const name = "slack";
export const host = "slack.com";
const REAL_URL = `https://${host}`;
// What an error answer points its reader at. Nothing a caller is sent names what is serving it.
const DOCS = "https://api.slack.com/methods";

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
  //   else the first seeded user). A new channel was created, and its topic and purpose set, at the
  //   date its file states, else at its first message, else at the earliest message the fake holds,
  //   else at the anchor.
  //   A new user was last updated at the date their file states as `created`, else at their first
  //   message, else at the earliest message; their last activity is their latest message.
  //   A channel or user that was there before keeps its dates. Reading a public channel needs no
  //   membership, as with a Slack user token, so membership changes what a channel lists, not who
  //   may read it.
  function settle(newChannels, newUsers, statedMembers) {
    const messages = ss.messages.all().sort(byTs);
    const floor = messages[0] ? isoOfTs(messages[0].ts) : clock.anchor;
    for (const m of messages) m.created_at = m.updated_at = isoOfTs(m.ts);
    for (const ch of ss.channels.all()) {
      const posted = messages.filter((m) => m.channel_id === ch.channel_id);
      const named = statedMembers.get(ch.name) ?? [];
      const members = [...new Set([...ch.members, ...named, ...posted.map((m) => m.user)])];
      const fresh = newChannels.get(ch.name);
      const creator = fresh ? posted[0]?.user ?? named[0] ?? ss.users.all()[0]?.user_id ?? null : ch.creator;
      const created = fresh ? clock.iso(fresh.created) ?? (posted[0] ? isoOfTs(posted[0].ts) : floor) : ch.created_at;
      Object.assign(ch, {
        members,
        num_members: members.length,
        creator,
        topic: fresh ? { ...ch.topic, creator, last_set: secondsOf(created) } : ch.topic,
        purpose: fresh ? { ...ch.purpose, creator, last_set: secondsOf(created) } : ch.purpose,
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

  function add(cfg) {
    const newChannels = new Map((cfg.channels ?? []).filter((c) => !ss.channels.findOneBy("name", c.name)).map((c) => [c.name, c]));
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
      const rec = ss.channels.findOneBy("name", c.name);
      const id = c.id ?? slackId("C", c.name);
      if (rec.channel_id !== id) ss.channels.update(rec.id, { channel_id: id });
    }
    const chan = (ref) => (ss.channels.findOneBy("name", ref) ?? ss.channels.findOneBy("channel_id", ref))?.channel_id;
    const user = (ref) => (ss.users.all().find((x) => x.name === ref) ?? ss.users.findOneBy("user_id", ref))?.user_id;
    // A new channel starts with no members: settle() gives it its posters and the users it names.
    for (const name of newChannels.keys()) ss.channels.findOneBy("name", name).members = [];
    const statedMembers = new Map();
    for (const c of cfg.channels ?? []) {
      const ids = (c.members ?? []).map((ref) => {
        const id = user(ref);
        if (!id) throw new Error(`slack channel ${c.name} names an unknown member ${ref}`);
        return id;
      });
      statedMembers.set(c.name, ids);
    }
    let n = 0;
    for (const m of cfg.messages ?? []) {
      const channel_id = chan(m.channel);
      const uid = user(m.user);
      if (!channel_id || !uid) throw new Error(`slack message refers to unknown channel/user: ${m.channel}/${m.user}`);
      const ts = clock.slackTs(m.ts);
      const thread_ts = clock.slackTs(m.thread_ts) ?? undefined;
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
    settle(newChannels, newUsers, statedMembers);
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
  async function search(req, p) {
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
        const who = term.slice(5).replace(/^[<@]+|>$/g, "");
        const u = ss.users.all().find((x) => x.name === who) ?? ss.users.findOneBy("user_id", who);
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
          return { type: "message", channel: { id: ch.channel_id, name: ch.name }, user: m.user, username: ss.users.findOneBy("user_id", m.user)?.name ?? "", ts: m.ts, text: m.text, permalink: `https://${domain}.slack.com/archives/${ch.channel_id}/p${m.ts.replace(".", "")}` };
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
  async function apiRoute(req, url) {
    const method = url.pathname.slice(5);
    const p = await params(req);
    if (method === "search.messages") return search(req, p);
    if (method === "conversations.history" && (p.oldest || p.latest)) return history(req, p);
    const res = await (req.method === "GET" ? asPost(req, method, Object.fromEntries(url.searchParams)) : app.fetch(req));
    return method in USER_ANSWERS ? withUpdated(res, USER_ANSWERS[method]) : res;
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
    host,
    store,
    tokenUsers,
    add,
    route,
    notFound: () => unknownMethod(404),
    counts: () => ({ users: ss.users.all().length, channels: ss.channels.all().length, messages: ss.messages.all().length }),
    outcome: (json) => ({ ok: json?.ok ?? null, error: json?.error ?? null, result_count: resultCount(json) }),
    unsupportedKeys: (url, json) => (json?.error === "unknown_method" ? [`slack:${url.pathname.replace(/^\/api\//, "")}`] : []),
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
