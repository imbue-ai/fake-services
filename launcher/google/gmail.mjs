// Gmail v1 for the fake Google: @emulators/google's Gmail routes and store, with what a trial needs on
// top. Messages are inserted straight into the store, with ids, threads, Message-IDs and historyIds
// derived from the seed, so they read the same in every trial; a message keeps the UTC offset its seed
// states in its Date header, and its labels are derived from who sent it;
// users.getProfile; a search operator the package answers wrongly is counted; a list leaves out an
// empty list and refuses a malformed page token, a listed label carries no counts, and a message's
// headers in every format are those of its MIME form, as Gmail does. See google.mjs for the product
// interface this module exports.
import crypto from "node:crypto";
import { seedFromConfig } from "@emulators/google";
import { googleError, isUnrouted, noSuchPath } from "./errors.mjs";

export const hosts = new Set(["gmail.googleapis.com", "www.googleapis.com"]);
export const prefixes = ["/gmail/v1/", "/upload/gmail/v1/"];
const BASE_URL = "https://oauth2.googleapis.com";

// Gmail's limits, as its documentation states them (developers.google.com/workspace/gmail/api/
// reference/quota, last updated 2026-10-16, read 2026-10-29): 6,000 quota units per minute per user
// per project, and each method's cost in units. A project created before 2026-05-01 keeps its earlier
// quota, which the page does not state; a task's integration is held to the current one. The
// per-project limit (1,200,000 a minute) is out of reach of one trial's few users and is not kept.
// A get costs more when its format is full (the default), raw or html: the page states that for
// messages.get and drafts.get, and full alone for threads.get, which takes no other of the three.
// A refusal is Gmail's userRateLimitExceeded (developers.google.com/workspace/gmail/api/guides/
// handle-errors, read 2026-10-29): a 403 in the usageLimits domain. Gmail documents no rate limit
// headers, so an answer carries none.
// Gmail reads `format` whatever its case (measured 2026-10-31: RAW answers as raw does), so it is
// costed, and given to the package, in lower case.
const PER_USER_PER_MINUTE = 6000;
const formatOf = (url) => (url.searchParams.get("format") ?? "full").toLowerCase();
const FULL_FORMATS = new Set(["full", "raw", "html"]);
// "<method> <path after users/{userId}/>" -> [cost, cost of a full get]
const COSTS = new Map(
  Object.entries({
    "GET profile": [1],
    "GET history": [2],
    "POST watch": [100],
    "POST stop": [50],
    "GET labels": [1],
    "GET labels/{id}": [1],
    "POST labels": [5],
    "PUT labels/{id}": [5],
    "PATCH labels/{id}": [5],
    "DELETE labels/{id}": [5],
    "GET drafts": [5],
    "GET drafts/{id}": [20, 60],
    "POST drafts": [10],
    "PUT drafts/{id}": [15],
    "DELETE drafts/{id}": [10],
    "POST drafts/send": [100],
    "GET messages": [5],
    "GET messages/{id}": [20, 60],
    "GET messages/{id}/attachments/{id}": [20],
    "POST messages": [25],
    "POST messages/import": [25],
    "POST messages/send": [100],
    "POST messages/batchModify": [50],
    "POST messages/batchDelete": [50],
    "POST messages/{id}/modify": [5],
    "POST messages/{id}/trash": [20],
    "POST messages/{id}/untrash": [5],
    "DELETE messages/{id}": [10],
    "GET threads": [10],
    "GET threads/{id}": [40, 120],
    "POST threads/{id}/modify": [10],
    "POST threads/{id}/trash": [20],
    "POST threads/{id}/untrash": [10],
    "DELETE threads/{id}": [20],
    "GET settings/delegates": [1],
    "GET settings/delegates/{id}": [1],
    "POST settings/delegates": [100],
    "DELETE settings/delegates/{id}": [5],
    "GET settings/filters": [1],
    "GET settings/filters/{id}": [1],
    "POST settings/filters": [5],
    "DELETE settings/filters/{id}": [5],
    "GET settings/forwardingAddresses": [1],
    "GET settings/forwardingAddresses/{id}": [1],
    "POST settings/forwardingAddresses": [100],
    "DELETE settings/forwardingAddresses/{id}": [5],
    "GET settings/sendAs": [1],
    "GET settings/sendAs/{id}": [1],
    "POST settings/sendAs": [100],
    "PUT settings/sendAs/{id}": [100],
    "PATCH settings/sendAs/{id}": [100],
    "DELETE settings/sendAs/{id}": [5],
    "POST settings/sendAs/{id}/verify": [100],
    "GET settings/autoForwarding": [1],
    "PUT settings/autoForwarding": [5],
    "GET settings/imap": [1],
    "PUT settings/imap": [5],
    "GET settings/pop": [1],
    "PUT settings/pop": [100],
    "GET settings/vacation": [1],
    "PUT settings/vacation": [5],
  }),
);

// A path under /gmail/v1/, split into the mailbox it names and the rest as a template: the segment
// after a collection is a record's id, unless it is one of the collection's own methods.
const COLLECTIONS = new Set(["messages", "threads", "labels", "drafts", "attachments", "delegates", "filters", "forwardingAddresses", "sendAs", "smimeInfo", "identities", "keypairs"]);
const COLLECTION_METHODS = new Set(["send", "import", "batchModify", "batchDelete", "modify", "trash", "untrash", "verify", "setDefault"]);
function templateOf(segments) {
  return segments.map((s, i) => (i > 0 && COLLECTIONS.has(segments[i - 1]) && !COLLECTION_METHODS.has(s) ? "{id}" : s)).join("/");
}
function parsePath(pathname) {
  const m = pathname.match(/^(\/upload)?\/gmail\/v1\/(.*)$/);
  const segments = m[2].split("/").filter(Boolean);
  if (segments[0] !== "users" || segments.length < 2) return { upload: m[1] ?? "", userId: null, rest: [], template: templateOf(segments) };
  const rest = segments.slice(2);
  return { upload: m[1] ?? "", userId: decodeURIComponent(segments[1]), rest, template: templateOf(rest) };
}

const BUCKET = "gmail_per_user";
// See rate_limits.mjs for what a policy holds.
export const limits = {
  buckets: { [BUCKET]: { limit: PER_USER_PER_MINUTE, windowSeconds: 60, algorithm: "window" } },
  bucketOf(req, url, user) {
    const { userId, template } = parsePath(url.pathname);
    const costs = userId === null ? undefined : COSTS.get(`${req.method} ${template}`);
    if (!costs) return [];
    const [cost, fullCost] = costs;
    const format = formatOf(url);
    return [{ bucket: BUCKET, scope: user, cost: fullCost !== undefined && FULL_FORMATS.has(format) ? fullCost : cost }];
  },
  refuse: () => googleError(403, "User Rate Limit Exceeded", "userRateLimitExceeded", "PERMISSION_DENIED", { domain: "usageLimits" }),
};

// The system labels a seed's message may carry besides the user labels of its mailbox. DRAFT is not
// one: a draft is a record of its own, which a seed does not hold.
const SEED_SYSTEM_LABELS = new Set(["INBOX", "SENT", "UNREAD", "STARRED", "IMPORTANT", "TRASH", "SPAM", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"]);
// Where historyIds start: one count for every mailbox, rising from here through each seed's or
// update's messages in date order, after everything the fake already held.
const HISTORY_BASE = 1000000n;
const OFFSET = /(Z|([+-])(\d{2}):(\d{2}))$/;
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n) => String(n).padStart(2, "0");
// An RFC 5322 date in the offset the sender's clock kept, as a Date header states it.
function dateHeader(ms, offsetMinutes) {
  const d = new Date(ms + offsetMinutes * 60000);
  const abs = Math.abs(offsetMinutes);
  const zone = `${offsetMinutes < 0 ? "-" : "+"}${pad(Math.floor(abs / 60))}${pad(abs % 60)}`;
  return `${DAYS[d.getUTCDay()]}, ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} ${zone}`;
}
const hash = (text) => crypto.createHash("sha1").update(text).digest("hex");
// A system label's visibility as Gmail serves it (measured 2026-10-30): INBOX, SENT, DRAFT, STARRED
// and UNREAD carry none, and every other system label is hidden from both lists.
const UNHIDDEN_SYSTEM_LABELS = new Set(["INBOX", "SENT", "DRAFT", "STARRED", "UNREAD"]);
function withSystemVisibility(label) {
  if (label.type !== "system") return label;
  const { messageListVisibility, labelListVisibility, ...rest } = label;
  return UNHIDDEN_SYSTEM_LABELS.has(label.id) ? rest : { ...rest, messageListVisibility: "hide", labelListVisibility: "labelHide" };
}

// Ids derived from the seed: a message's id (and the id of a thread it starts) is 16 hex digits as
// Gmail's are, a user label's is Label_ and a number.
const messageIdOf = (key) => hash(`message\n${key}`).slice(0, 16);
const labelIdOf = (mailbox, name) => `Label_${BigInt("0x" + hash(`label\n${mailbox}\n${name}`).slice(0, 13))}`;
const ADDRESS = /[^\s<>,;"']+@[^\s<>,;"']+/g;
const addressesOf = (...headers) => headers.flatMap((h) => (h ?? "").match(ADDRESS) ?? []).map((a) => a.toLowerCase());

// A message's MIME structure: its text alone, or its text and its HTML as the two parts of a
// multipart/alternative. format=raw serves it as MIME, and format=full and metadata show the same
// headers, so the formats agree. The top-level headers are in the order Gmail serves them.
const textPartHeaders = (type) => [
  ["Content-Type", `${type}; charset="UTF-8"`],
  ["Content-Transfer-Encoding", "8bit"],
];
function mimeOf(m) {
  const headers = [
    ["MIME-Version", "1.0"],
    ["Date", m.date_header],
    ["References", m.references],
    ["In-Reply-To", m.in_reply_to],
    ["Message-ID", m.message_id],
    ["Subject", m.subject],
    ["From", m.from],
    ["To", m.to],
    ["Cc", m.cc],
    ["Bcc", m.bcc],
  ].filter(([, value]) => value !== null && value !== undefined);
  if (!m.body_html) return { headers: [...headers, ...textPartHeaders("text/plain")], boundary: null, parts: [] };
  const boundary = `000000000000${hash(m.message_id).slice(0, 16)}`;
  return {
    headers: [...headers, ["Content-Type", `multipart/alternative; boundary="${boundary}"`]],
    boundary,
    parts: [
      { headers: textPartHeaders("text/plain"), text: m.body_text },
      { headers: textPartHeaders("text/html"), text: m.body_html },
    ],
  };
}

// The message as Gmail serves it in format=raw, base64url-encoded.
function rawOf(m) {
  const { headers, boundary, parts } = mimeOf(m);
  const block = (lines) => lines.map(([name, value]) => `${name}: ${value}\r\n`).join("");
  const text = boundary
    ? `${block(headers)}\r\n${parts.map((part) => `--${boundary}\r\n${block(part.headers)}\r\n${part.text}\r\n`).join("")}--${boundary}--\r\n`
    : `${block(headers)}\r\n${m.body_text}`;
  return Buffer.from(text, "utf-8").toString("base64url");
}

// A message resource of format full or metadata with the headers its MIME structure holds; a
// metadata get that names headers gets those alone, matched whatever their case, as Gmail matches
// them.
function withMimeHeaders(resource, m, named) {
  const { headers, parts } = mimeOf(m);
  const asHeaders = (lines) => lines.map(([name, value]) => ({ name, value }));
  const wanted = named.length ? new Set(named.map((h) => h.toLowerCase())) : null;
  resource.payload.headers = asHeaders(wanted ? headers.filter(([name]) => wanted.has(name.toLowerCase())) : headers);
  resource.payload.mimeType = headers.find(([name]) => name === "Content-Type")[1].split(";")[0];
  for (const [i, part] of (resource.payload.parts ?? []).entries()) part.headers = asHeaders(parts[i].headers);
}

// Search. The package's own parser takes from:, to:, subject:, label:, -label:, in:, is:, after: and
// before: in epoch seconds, has:attachment and words and phrases. Each term is checked here first:
//   - an operator's quoted value (subject:"March invoice") is one term, as Gmail reads it. The
//     package would split it at its spaces, but reads a quoted term that holds the operator too, so
//     it is given as "subject:March invoice" and matches the phrase whatever its case. The term's
//     parentheses are dropped, as the package drops them anyway.
//   - from:me and to:me name the mailbox's own address, which the package reads as the word "me"
//   - in:anywhere searches SPAM and TRASH too: the term is dropped and the list is asked with
//     includeSpamTrash, which the package reads
//   - a date in after:, before: and their synonyms newer: and older: is midnight Pacific time, as
//     Gmail reads it, given to the package in seconds, which it otherwise reads in the box's zone;
//     newer_than:/older_than: in days are taken from the trial's present
//   - an operator the package answers wrongly with a 200 is passed on as it is and counted, under
//     google:gmail:q:<operator>
//   - `or` and `and` written in anything but capitals are words to Gmail, which the package drops as
//     it drops OR and AND; such a term is passed on as it is and counted, under google:gmail:q:or-word
//     and google:gmail:q:and-word. AND itself is Gmail's implicit AND, which dropping it keeps.
const UNSUPPORTED_OPERATORS = new Set(["cc", "bcc", "deliveredto", "list", "filename", "category", "size", "larger", "smaller", "header", "rfc822msgid"]);
const DATE_OPERATORS = new Map([["after", "after"], ["before", "before"], ["newer", "after"], ["older", "before"]]);
// Every operator Gmail has: a word with a colon that names none of these is a word to Gmail too.
const OPERATORS = new Set(["from", "to", "subject", "label", "in", "is", "has", "newer_than", "older_than", ...DATE_OPERATORS.keys(), ...UNSUPPORTED_OPERATORS]);
const PACIFIC = new Intl.DateTimeFormat("en-US", { timeZone: "America/Los_Angeles", timeZoneName: "longOffset" });
function pacificMidnightSeconds(year, month, day) {
  // 08:00 UTC is before 02:00 in Los Angeles, when its offset changes, so it keeps midnight's offset.
  const zone = PACIFIC.formatToParts(new Date(Date.UTC(year, month - 1, day, 8))).find((p) => p.type === "timeZoneName").value;
  const [, sign, hours, minutes] = zone.match(/GMT([+-])(\d{2}):(\d{2})/);
  const offsetMs = (sign === "-" ? -1 : 1) * (Number(hours) * 60 + Number(minutes)) * 60000;
  return (Date.UTC(year, month - 1, day) - offsetMs) / 1000;
}
function secondsOfDate(value) {
  let m = value.match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$/);
  if (m) return pacificMidnightSeconds(Number(m[1]), Number(m[2]), Number(m[3]));
  m = value.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  return m ? pacificMidnightSeconds(Number(m[3]), Number(m[1]), Number(m[2])) : null;
}
function rewriteQuery(q, email, nowMs) {
  const keys = [];
  let anywhere = false;
  const terms = (q.match(/[^\s"]*"[^"]+"[^\s"]*|\S+/g) ?? []).map((term) => {
    if (term.startsWith('"')) return term;
    if (term === "OR" || term === "AROUND") return keys.push(term), term;
    if (term !== "AND" && /^(or|and)$/i.test(term)) return keys.push(`${term.toLowerCase()}-word`), term;
    if (/[{}]/.test(term)) return keys.push("{}"), term;
    if (term.length > 1 && term.startsWith("+")) return keys.push("+"), term;
    const negated = term.length > 1 && term.startsWith("-");
    const [, open, core, close] = (negated ? term.slice(1) : term).match(/^(\(*)(.*?)(\)*)$/);
    const op = core.match(/^([A-Za-z_]+):(.*)$/);
    if (negated && op?.[1].toLowerCase() !== "label") return keys.push("-"), term;
    if (!op) return term;
    const [, written, operand] = op;
    const name = written.toLowerCase();
    if (!OPERATORS.has(name)) return term;
    if (operand.startsWith("(")) return keys.push(`${name}:()`), term;
    const quoted = operand.match(/^"(.*)"$/);
    const value = quoted ? quoted[1] : operand;
    const lower = value.toLowerCase();
    const rebuilt = (operator, given) =>
      quoted ? `"${negated ? "-" : ""}${operator}:${given}"` : `${negated ? "-" : ""}${open}${operator}:${given}${close}`;
    if (UNSUPPORTED_OPERATORS.has(name)) return keys.push(name), term;
    if ((name === "from" || name === "to") && lower === "me") return rebuilt(written, email);
    if (name === "in" && lower === "anywhere") return (anywhere = true), "";
    if (name === "in" && lower === "archive") return keys.push("in:archive"), term;
    if (name === "has" && lower !== "attachment") return keys.push("has"), term;
    if (DATE_OPERATORS.has(name)) {
      const seconds = /^\d+$/.test(value) ? Number(value) : secondsOfDate(value);
      return seconds === null ? (keys.push(name), term) : rebuilt(DATE_OPERATORS.get(name), seconds);
    }
    if (name === "newer_than" || name === "older_than") {
      const days = value.match(/^(\d+)d$/i);
      if (!days) return keys.push(name), term;
      return rebuilt(name === "newer_than" ? "after" : "before", Math.floor(nowMs / 1000) - Number(days[1]) * 86400);
    }
    return quoted ? rebuilt(written, value) : term;
  });
  // A gap is counted once per request, however many of the query's terms meet it.
  return { q: terms.filter(Boolean).join(" "), keys: [...new Set(keys)].map((k) => `google:gmail:q:${k}`), anywhere };
}

// The headers a metadata get may name, as the package spells them; Gmail matches the names whatever
// their case, the package only as spelled.
const HEADER_NAMES = new Map(["From", "To", "Cc", "Bcc", "Reply-To", "Subject", "Date", "Message-ID", "References", "In-Reply-To"].map((h) => [h.toLowerCase(), h]));
const PAGED = new Set(["messages", "threads", "drafts", "history"]);

export function create({ app, store, gs, clock }) {
  const owned = (email) => gs.messages.findBy("user_email", email);
  const currentHistoryId = (email) => {
    const ids = [...owned(email).map((m) => m.history_id), ...gs.history.findBy("user_email", email).map((h) => h.gmail_id)].map(BigInt);
    return String(ids.reduce((a, b) => (b > a ? b : a), HISTORY_BASE));
  };
  const earliest = (email) => {
    const dates = (email ? owned(email) : gs.messages.all()).map((m) => Number(m.internal_date));
    return dates.length ? Math.min(...dates) : null;
  };

  function labelIdFor(mailbox, name) {
    if (SEED_SYSTEM_LABELS.has(name)) return name;
    return gs.labels.findBy("user_email", mailbox).find((l) => l.type === "user" && l.name === name)?.gmail_id ?? null;
  }

  function add(cfg) {
    const requireMailbox = (email, what) => {
      if (!gs.users.findOneBy("email", email)) throw new Error(`gmail ${what} is in mailbox ${email}, which no google user is`);
    };
    const labels = cfg.labels ?? [];
    const listed = new Set();
    for (const l of labels) {
      requireMailbox(l.mailbox, `label ${l.name}`);
      const id = `${l.mailbox}\n${l.name.toLowerCase()}`;
      if (listed.has(id) || gs.labels.findBy("user_email", l.mailbox).some((x) => x.name.toLowerCase() === l.name.toLowerCase()))
        throw new Error(`gmail label ${l.name} of ${l.mailbox} is listed again`);
      listed.add(id);
    }
    if (labels.length) seedFromConfig(store, BASE_URL, { labels: labels.map((l) => ({ id: labelIdOf(l.mailbox, l.name), user_email: l.mailbox, name: l.name })) });

    // A message's in_reply_to names an earlier message by key; the store ids are looked up afresh,
    // since a store update replaces the record.
    const byKey = new Map(gs.messages.all().filter((m) => m.seed_key).map((m) => [m.seed_key, m.id]));
    const inserted = [];
    for (const m of cfg.messages ?? []) {
      const what = `message ${m.key}`;
      if (byKey.has(m.key)) throw new Error(`gmail ${what} is listed again`);
      requireMailbox(m.mailbox, what);
      for (const field of ["from", "to", "subject", "body"]) if (typeof m[field] !== "string") throw new Error(`gmail ${what} has no ${field}`);
      const offset = OFFSET.exec(m.date ?? "");
      if (!offset || Number.isNaN(Date.parse(m.date))) throw new Error(`gmail ${what} has no date with a UTC offset: ${m.date}`);
      const offsetMinutes = offset[1] === "Z" ? 0 : (offset[2] === "-" ? -1 : 1) * (Number(offset[3]) * 60 + Number(offset[4]));
      const internalMs = Date.parse(m.date) + clock.shiftMs;
      let inReplyTo = null;
      let references = null;
      if (m.in_reply_to?.startsWith("<")) inReplyTo = references = m.in_reply_to;
      else if (m.in_reply_to) {
        const parent = gs.messages.get(byKey.get(m.in_reply_to));
        if (!parent) throw new Error(`gmail ${what} replies to ${m.in_reply_to}, which no earlier message is`);
        inReplyTo = parent.message_id;
        references = [parent.references, parent.message_id].filter(Boolean).join(" ");
      }
      const gmailId = messageIdOf(m.key);
      const sender = addressesOf(m.from)[0] ?? "";
      const messageId = m.message_id ?? `<${hash(`message-id\n${m.key}`).slice(0, 32)}@${sender.split("@")[1] ?? "mail.gmail.com"}>`;
      // A message is in the thread of the mailbox's message it refers to, else it starts one, whose id
      // is its own, as Gmail ids a thread by its first message.
      const linked = new Set((references ?? "").split(/\s+/).filter(Boolean));
      const threadId = owned(m.mailbox).find((x) => linked.has(x.message_id))?.thread_id ?? gmailId;
      const stated = (m.labels ?? []).map((name) => {
        const id = labelIdFor(m.mailbox, name);
        if (!id) throw new Error(`gmail ${what} carries label ${name}, which is neither a system label nor one of ${m.mailbox}'s`);
        return id;
      });
      // Mail the mailbox sent is SENT; mail it received, its own to itself included, is in the INBOX
      // unless the seed archives it or puts it in TRASH or SPAM. UNREAD and the rest are the seed's.
      const owner = m.mailbox.toLowerCase();
      const derived = [];
      if (sender === owner) derived.push("SENT");
      if ((sender !== owner || addressesOf(m.to, m.cc, m.bcc).includes(owner)) && !m.archived && !stated.some((l) => l === "TRASH" || l === "SPAM")) derived.push("INBOX");
      const record = {
        gmail_id: gmailId,
        thread_id: threadId,
        user_email: m.mailbox,
        history_id: "0",
        internal_date: String(internalMs),
        label_ids: [...new Set([...derived, ...stated])],
        snippet: m.body.replace(/\s+/g, " ").trim().slice(0, 140) || m.subject,
        subject: m.subject,
        from: m.from,
        to: m.to,
        cc: m.cc ?? null,
        bcc: m.bcc ?? null,
        reply_to: null,
        message_id: messageId,
        references,
        in_reply_to: inReplyTo,
        date_header: dateHeader(internalMs, offsetMinutes),
        body_text: m.body,
        body_html: m.body_html ?? null,
        seed_key: m.key,
      };
      const rec = gs.messages.insert({ ...record, raw: rawOf(record) });
      const iso = new Date(internalMs).toISOString();
      Object.assign(rec, { created_at: iso, updated_at: iso });
      byKey.set(m.key, rec.id);
      inserted.push(rec.id);
    }

    // historyIds follow the order the messages were written in, after every message the fake already
    // held, so an update's messages are history newer than the seed's.
    const held = [...gs.messages.all().map((m) => m.history_id), ...gs.history.all().map((h) => h.gmail_id)].map(BigInt);
    let next = held.reduce((a, b) => (b > a ? b : a), HISTORY_BASE);
    const fresh = inserted.map((id) => gs.messages.get(id)).sort((a, b) => Number(a.internal_date) - Number(b.internal_date));
    for (const rec of fresh) {
      next += 1n;
      rec.history_id = String(next);
      const event = gs.history.insert({ gmail_id: rec.history_id, user_email: rec.user_email, change_type: "messageAdded", message_gmail_id: rec.gmail_id, thread_id: rec.thread_id, label_ids: rec.label_ids });
      Object.assign(event, { created_at: rec.created_at, updated_at: rec.created_at });
    }

    // A label new to the fake (a mailbox's system labels, or one the file lists) is dated at its
    // mailbox's earliest message, else the earliest the fake holds, else the anchor: the package dates
    // it with the wall clock.
    const floor = earliest(null);
    for (const label of gs.labels.all().filter((l) => !l.dated)) {
      const at = earliest(label.user_email) ?? floor;
      const iso = at === null ? clock.anchor : new Date(at).toISOString();
      Object.assign(label, { created_at: iso, updated_at: iso, dated: true });
    }
    return { gmail_labels: labels.length, gmail_messages: inserted.length };
  }

  const unsupportedOf = new WeakMap();
  async function forward(req, target) {
    const init = { method: req.method, headers: req.headers };
    if (req.method !== "GET" && req.method !== "HEAD") init.body = await req.clone().arrayBuffer();
    return app.fetch(new Request(target, init));
  }

  async function route(req, url, email) {
    const keys = [];
    unsupportedOf.set(req, keys);
    const path = parsePath(url.pathname);
    const unsupported = () => {
      keys.push(`google:gmail:${req.method} ${path.userId === null ? "" : "users/{userId}/"}${path.template}`);
      return noSuchPath(url.pathname);
    };
    if (path.userId === null) return unsupported();
    // Another mailbox is refused as Gmail refuses a mailbox the credential is not delegated, naming
    // the caller's own address rather than the mailbox asked for.
    if (path.userId !== "me" && path.userId.toLowerCase() !== email.toLowerCase())
      return googleError(403, `Delegation denied for ${email}`, "forbidden", "PERMISSION_DENIED");
    const action = `${req.method} ${path.template}`;
    if (action === "GET profile") {
      const messages = owned(email);
      return Response.json({ emailAddress: email, messagesTotal: messages.length, threadsTotal: new Set(messages.map((m) => m.thread_id)).size, historyId: currentHistoryId(email) });
    }
    const target = new URL(url);
    target.pathname = `${path.upload}/gmail/v1/users/me/${path.rest.join("/")}`;
    const listed = req.method === "GET" && path.rest.length === 1 && PAGED.has(path.rest[0]) ? path.rest[0] : null;
    if (listed) {
      // The package reads any page token as a number, a malformed one as the first page.
      const token = url.searchParams.get("pageToken");
      if (token && !/^\d+$/.test(token)) return googleError(400, "Invalid pageToken", "invalidArgument", "INVALID_ARGUMENT");
      const q = url.searchParams.get("q");
      if (q && (listed === "messages" || listed === "threads")) {
        const rewritten = rewriteQuery(q, email, clock.now());
        keys.push(...rewritten.keys);
        target.searchParams.set("q", rewritten.q);
        if (rewritten.anywhere) target.searchParams.set("includeSpamTrash", "true");
      }
    }
    if (target.searchParams.has("format")) target.searchParams.set("format", formatOf(url));
    const named = target.searchParams.getAll("metadataHeaders");
    if (named.length) {
      target.searchParams.delete("metadataHeaders");
      for (const h of named) target.searchParams.append("metadataHeaders", HEADER_NAMES.get(h.toLowerCase()) ?? h);
    }
    const res = await forward(req, target);
    if (isUnrouted(res) || (res.headers.get("content-type") ?? "").includes("text/html")) return unsupported();
    if (req.method !== "GET" || res.status !== 200) return res;
    const answer = await res.json();
    // A listed label carries no counts; labels.get alone serves them.
    if (action === "GET labels")
      answer.labels = answer.labels.map(({ id, name, messageListVisibility, labelListVisibility, type, color }) =>
        withSystemVisibility({ id, name, messageListVisibility, labelListVisibility, type, color }),
      );
    if (action === "GET labels/{id}") {
      const shown = withSystemVisibility(answer);
      for (const key of Object.keys(answer)) if (!(key in shown)) delete answer[key];
      Object.assign(answer, shown);
    }
    // A message the fake placed is shown with the headers of the raw form it was given; one the
    // package made keeps the package's.
    const format = formatOf(url);
    if (path.rest.length === 2 && (path.rest[0] === "messages" || path.rest[0] === "threads") && (format === "full" || format === "metadata")) {
      const placed = new Map(owned(email).filter((m) => m.seed_key).map((m) => [m.gmail_id, m]));
      const asked = format === "metadata" ? url.searchParams.getAll("metadataHeaders") : [];
      for (const resource of answer.messages ?? [answer]) if (placed.has(resource.id)) withMimeHeaders(resource, placed.get(resource.id), asked);
    }
    // Google leaves an empty list out of an answer.
    for (const [k, v] of Object.entries(answer)) if (Array.isArray(v) && !v.length) delete answer[k];
    return Response.json(answer);
  }

  return {
    add,
    route,
    earliest,
    unsupportedKeys: (req) => unsupportedOf.get(req) ?? [],
    counts: () => ({
      labels: gs.labels.all().filter((l) => l.type === "user").length,
      messages: gs.messages.all().length,
      threads: new Set(gs.messages.all().map((m) => `${m.user_email}\n${m.thread_id}`)).size,
    }),
  };
}
