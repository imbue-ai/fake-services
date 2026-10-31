// Calendar v3 for the fake Google. Every route is this module's own, on @emulators/google's calendar
// collections: the package serves too little of Calendar (no calendars.get, calendarList.get or
// events.get, no organizer, creator or iCalUID on an event, dates from the wall clock) for its
// routes to be worth wrapping. Each account has one calendar, its primary, whose id is its address;
// a seed's events are placed on their owner's primary calendar with ids derived from the seed, so
// they read the same in every trial. See google.mjs for the product interface this module exports.
import crypto from "node:crypto";
import { noSuchPath } from "./errors.mjs";

export const prefixes = ["/calendar/v3/"];

// Calendar's limits, as its documentation states them (developers.google.com/workspace/calendar/api/
// guides/quota, last updated 2026-09-11, read 2026-10-30): 600 requests per minute per user per
// project, over a sliding window, every request alike (the page states no per-method costs for the
// API). The per-project limit (10,000 a minute) is out of reach of one trial's few users and is not
// kept. The quota page says only that a caller past it gets "a 403 usageLimits status code or a 429
// usageLimits status code"; the error guide (developers.google.com/workspace/calendar/api/guides/
// errors, last updated 2026-09-11, read 2026-10-30) gives the per-user one as a 403
// userRateLimitExceeded, which is what this fake answers. Which of the 403 and the 429 the real API
// sends a single user who outruns the per-user limit is not stated, and the pages document no rate
// limit headers, so an answer carries none.
const PER_USER_PER_MINUTE = 600;
const BUCKET = "calendar_per_user";

// Calendar's error shape, as its error guide gives it: no `status`, unlike Gmail's.
function calendarError(code, message, reason, { domain = "global", ...extra } = {}) {
  return Response.json({ error: { errors: [{ domain, reason, message, ...extra }], code, message } }, { status: code });
}
const notFound = () => calendarError(404, "Not Found", "notFound");
const badRequest = (message = "Bad Request", extra = {}) => calendarError(400, message, "badRequest", extra);
const invalidParameter = (name, value) => calendarError(400, `Invalid value '${value}'.`, "invalid", { locationType: "parameter", location: name });
const invalidField = (message) => calendarError(400, message, "invalid");
const requiredField = (message) => calendarError(400, message, "required");
const timeRangeEmpty = (location) =>
  calendarError(400, "The specified time range is empty.", "timeRangeEmpty", { domain: "calendar", ...(location ? { locationType: "parameter", location } : {}) });

// See rate_limits.mjs for what a policy holds. Every request a seeded token makes of Calendar costs one.
export const limits = {
  buckets: { [BUCKET]: { limit: PER_USER_PER_MINUTE, windowSeconds: 60, algorithm: "window" } },
  bucketOf: (req, url, user) => [{ bucket: BUCKET, scope: user, cost: 1 }],
  refuse: () => calendarError(403, "User Rate Limit Exceeded", "userRateLimitExceeded", { domain: "usageLimits" }),
};

const hash = (text) => crypto.createHash("sha1").update(text).digest();
// An event id is lowercase base32hex, as Google's are (RFC 2938's digits and the letters a-v).
const BASE32HEX = "0123456789abcdefghijklmnopqrstuv";
function base32hex(bytes, length) {
  let bits = 0n;
  for (const b of bytes) bits = (bits << 8n) | BigInt(b);
  let out = "";
  for (let i = 0; i < length; i++) out += BASE32HEX[Number((bits >> BigInt(5 * i)) & 31n)];
  return out;
}
const eventIdOf = (key) => base32hex(hash(`calendar-event\n${key}`), 26);
const EVENT_ID = /^[0-9a-v]{5,1024}$/;

// An etag is twice the record's last change in microseconds, as Calendar's read.
const etagOf = (ms) => `"${BigInt(ms) * 2000n}"`;
const DEFAULT_REMINDERS = [{ method: "popup", minutes: 10 }];
const RESPONSE_STATUSES = new Set(["needsAction", "accepted", "declined", "tentative"]);

// Time. A seed's instant keeps the UTC offset it was written in, and its event is in the zone of that
// offset: the IANA Etc/GMT zone of a whole-hour offset (whose sign is POSIX's, the inverse of the
// offset's), UTC for any other. A calendar is in the zone most of its owner's first events are in.
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})$/i;
const LOCAL_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const DAY_MS = 24 * 60 * 60 * 1000;
const pad = (n) => String(n).padStart(2, "0");
function offsetMinutesOf(stamp) {
  const m = stamp.match(/(Z|([+-])(\d{2}):(\d{2}))$/i);
  return m[1].toUpperCase() === "Z" ? 0 : (m[2] === "-" ? -1 : 1) * (Number(m[3]) * 60 + Number(m[4]));
}
const zoneOfOffset = (minutes) => (minutes === 0 || minutes % 60 !== 0 ? "UTC" : `Etc/GMT${minutes < 0 ? "+" : "-"}${Math.abs(minutes) / 60}`);
// The offset a zone keeps at an instant.
function zoneOffsetMinutes(zone, ms) {
  const name = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "longOffset" }).formatToParts(new Date(ms)).find((p) => p.type === "timeZoneName").value;
  const m = name.match(/GMT([+-])(\d{2}):(\d{2})/);
  return m ? (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3])) : 0;
}
const isZone = (zone) => {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
};
// An RFC 3339 instant in a zone's offset at that instant, as Calendar serves a dateTime.
function inZone(ms, zone) {
  const offset = zoneOffsetMinutes(zone, ms);
  const d = new Date(ms + offset * 60000);
  const abs = Math.abs(offset);
  const suffix = offset === 0 ? "Z" : `${offset < 0 ? "-" : "+"}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}${suffix}`;
}
// A wall-clock time in a zone, as epoch ms.
function fromLocal(local, zone) {
  const [, y, mo, d, h, mi, s = "0"] = local.match(LOCAL_TIMESTAMP);
  const asUtc = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s));
  const first = asUtc - zoneOffsetMinutes(zone, asUtc) * 60000;
  return asUtc - zoneOffsetMinutes(zone, first) * 60000;
}
// An all-day event's day starts at midnight in its calendar's zone.
const dayStartMs = (day, zone) => fromLocal(`${day}T00:00:00`, zone);

// One side of an event, {ms, date, zone}, from what a seed or an insert states; a string saying why
// it cannot be read otherwise.
function sideOfSeed(value) {
  if (typeof value !== "string") return "is not a string";
  if (DATE.test(value)) return Number.isNaN(Date.parse(`${value}T00:00:00Z`)) ? "is not a date" : { date: value };
  if (!TIMESTAMP.test(value) || Number.isNaN(Date.parse(value))) return "is neither a date nor a timestamp with a UTC offset";
  return { ms: Date.parse(value), zone: zoneOfOffset(offsetMinutesOf(value)) };
}

export function create({ gs, clock }) {
  const calendarOf = (email) => gs.calendars.findBy("user_email", email).find((c) => c.primary);
  const eventsOf = (email) => gs.calendarEvents.findBy("user_email", email);
  const startMs = (e, zone) => e.start_ms ?? dayStartMs(e.start_date, zone);
  const endMs = (e, zone) => e.end_ms ?? dayStartMs(e.end_date, zone);
  const earliest = (email) => {
    const dates = (email ? eventsOf(email) : gs.calendarEvents.all()).map((e) => Date.parse(e.created_at));
    return dates.length ? Math.min(...dates) : null;
  };
  // A calendar was last changed with its latest event, else when it was made.
  const calendarUpdatedMs = (calendar) => Math.max(Date.parse(calendar.created_at), ...eventsOf(calendar.user_email).map((e) => Date.parse(e.updated_at)));

  // The organizer is a guest of an event that has guests, as Calendar lists them; an attendee is
  // `self` when the calendar is theirs.
  function attendeesOf(owner, listed) {
    const attendees = listed.map((a) => ({
      email: a.email,
      display_name: a.name ?? null,
      response_status: a.response_status ?? "needsAction",
      organizer: a.email.toLowerCase() === owner.toLowerCase(),
      self: a.email.toLowerCase() === owner.toLowerCase(),
    }));
    if (attendees.length && !attendees.some((a) => a.organizer)) attendees.unshift({ email: owner, display_name: null, response_status: "accepted", organizer: true, self: true });
    return attendees;
  }

  function insertEvent(fields) {
    const rec = gs.calendarEvents.insert({
      user_email: fields.owner,
      calendar_google_id: fields.owner,
      status: "confirmed",
      summary: fields.summary ?? "",
      description: fields.description ?? null,
      location: fields.location ?? null,
      html_link: `https://www.google.com/calendar/event?eid=${Buffer.from(`${fields.id} ${fields.owner}`).toString("base64").replace(/=+$/, "")}`,
      hangout_link: null,
      ...fields.times,
      attendees: attendeesOf(fields.owner, fields.attendees ?? []),
      conference_entry_points: [],
      transparency: null,
      google_id: fields.id,
      ical_uid: `${fields.id}@google.com`,
      sequence: 0,
      seed_key: fields.key ?? null,
    });
    Object.assign(rec, { created_at: fields.dated, updated_at: fields.dated });
    return rec;
  }

  // The times of a seed's or an insert's event, stored as the fake reads them back: an instant in
  // epoch ms and the zone it is in, or a day.
  const timesOf = (start, end) => ({
    start_ms: start.ms ?? null,
    start_date_time: start.ms === undefined ? null : new Date(start.ms).toISOString(),
    start_date: start.date ?? null,
    start_time_zone: start.zone ?? null,
    end_ms: end.ms ?? null,
    end_date_time: end.ms === undefined ? null : new Date(end.ms).toISOString(),
    end_date: end.date ?? null,
    end_time_zone: end.zone ?? null,
  });

  function add(cfg) {
    const events = cfg.events ?? [];
    const byKey = new Set(gs.calendarEvents.all().map((e) => e.seed_key).filter(Boolean));
    const checked = events.map((e) => {
      const what = `calendar event ${e.key}`;
      if (typeof e.key !== "string" || !e.key) throw new Error(`a calendar event has no key: ${JSON.stringify(e).slice(0, 200)}`);
      if (byKey.has(e.key)) throw new Error(`${what} is listed again`);
      byKey.add(e.key);
      if (!gs.users.findOneBy("email", e.owner ?? "")) throw new Error(`${what} is on the calendar of ${e.owner}, which no google user is`);
      if (typeof e.summary !== "string") throw new Error(`${what} has no summary`);
      for (const field of ["description", "location"]) if (e[field] != null && typeof e[field] !== "string") throw new Error(`${what} has a ${field} that is not a string`);
      const [start, end] = ["start", "end"].map((side) => {
        const read = sideOfSeed(e[side]);
        if (typeof read === "string") throw new Error(`${what}'s ${side} ${read}: ${e[side]}`);
        return read;
      });
      if ((start.date === undefined) !== (end.date === undefined)) throw new Error(`${what} has one all-day side and one timed side`);
      if (start.date !== undefined ? end.date <= start.date : end.ms <= start.ms) throw new Error(`${what} ends no later than it starts`);
      if (!Array.isArray(e.attendees ?? [])) throw new Error(`${what}'s attendees is not a list`);
      const seen = new Set();
      for (const a of e.attendees ?? []) {
        if (typeof a?.email !== "string" || !a.email.includes("@")) throw new Error(`${what} has an attendee with no address`);
        if (seen.has(a.email.toLowerCase())) throw new Error(`${what} lists attendee ${a.email} again`);
        seen.add(a.email.toLowerCase());
        if (a.name != null && typeof a.name !== "string") throw new Error(`${what}'s attendee ${a.email} has a name that is not a string`);
        if (a.response_status != null && !RESPONSE_STATUSES.has(a.response_status)) throw new Error(`${what}'s attendee ${a.email} has response status ${a.response_status}`);
      }
      const shifted = (side) => (side.date !== undefined ? { date: clock.date(side.date) } : { ms: side.ms + clock.shiftMs, zone: side.zone });
      return { e, start: shifted(start), end: shifted(end) };
    });

    // Every account without a calendar gets its primary, in the zone most of its owner's events here
    // are in (the earliest of those tied), else UTC; it is dated with its account (settle).
    let calendars = 0;
    for (const user of gs.users.all()) {
      if (calendarOf(user.email)) continue;
      const zones = checked.filter((c) => c.e.owner === user.email && c.start.zone).sort((a, b) => a.start.ms - b.start.ms).map((c) => c.start.zone);
      const tally = new Map();
      for (const z of zones) tally.set(z, (tally.get(z) ?? 0) + 1);
      const zone = [...tally].reduce((best, entry) => (entry[1] > best[1] ? entry : best), ["UTC", 0])[0];
      const calendar = gs.calendars.insert({
        google_id: user.email,
        user_email: user.email,
        summary: user.email,
        description: null,
        time_zone: zone,
        primary: true,
        selected: true,
        access_role: "owner",
        background_color: "#9fe1e7",
        foreground_color: "#000000",
      });
      Object.assign(calendar, { created_at: clock.anchor, updated_at: clock.anchor });
      calendars += 1;
    }

    // A seed states no event's creation. An event was put on its calendar no later than it began nor
    // later than the seed's data ends: it was created, and last changed, at the earlier of its start
    // (midnight in its calendar's zone for an all-day one) and the anchor, both shifted.
    const anchorMs = Date.parse(clock.anchor);
    for (const { e, start, end } of checked) {
      const zone = calendarOf(e.owner).time_zone;
      const begins = start.ms ?? dayStartMs(start.date, zone);
      insertEvent({
        owner: e.owner,
        id: eventIdOf(e.key),
        key: e.key,
        summary: e.summary,
        description: e.description,
        location: e.location,
        attendees: e.attendees,
        times: timesOf(start, end),
        dated: new Date(Math.min(begins, anchorMs)).toISOString(),
      });
    }
    return { calendar_calendars: calendars, calendar_events: checked.length };
  }

  // A new account's calendar is made with the account.
  function settle(email, iso) {
    Object.assign(calendarOf(email), { created_at: iso, updated_at: iso });
  }

  function eventResource(e) {
    const calendar = calendarOf(e.user_email);
    const side = (prefix) =>
      e[`${prefix}_date`] !== null ? { date: e[`${prefix}_date`] } : { dateTime: inZone(e[`${prefix}_ms`], calendar.time_zone), timeZone: e[`${prefix}_time_zone`] };
    const resource = {
      kind: "calendar#event",
      etag: etagOf(Date.parse(e.updated_at)),
      id: e.google_id,
      status: e.status,
      htmlLink: e.html_link,
      created: e.created_at,
      updated: e.updated_at,
      summary: e.summary,
    };
    if (e.description !== null) resource.description = e.description;
    if (e.location !== null) resource.location = e.location;
    Object.assign(resource, {
      creator: { email: e.user_email, self: true },
      organizer: { email: e.user_email, self: true },
      start: side("start"),
      end: side("end"),
      iCalUID: e.ical_uid,
      sequence: e.sequence,
    });
    if (e.attendees.length)
      resource.attendees = e.attendees.map((a) => ({
        email: a.email,
        ...(a.display_name ? { displayName: a.display_name } : {}),
        ...(a.organizer ? { organizer: true } : {}),
        ...(a.self ? { self: true } : {}),
        responseStatus: a.response_status,
      }));
    return Object.assign(resource, { reminders: { useDefault: true }, eventType: "default" });
  }

  function calendarListEntry(calendar) {
    return {
      kind: "calendar#calendarListEntry",
      etag: etagOf(Date.parse(calendar.created_at)),
      id: calendar.google_id,
      summary: calendar.summary,
      timeZone: calendar.time_zone,
      colorId: "14",
      backgroundColor: calendar.background_color,
      foregroundColor: calendar.foreground_color,
      selected: true,
      accessRole: calendar.access_role,
      defaultReminders: DEFAULT_REMINDERS,
      notificationSettings: { notifications: ["eventCreation", "eventChange", "eventCancellation", "eventResponse"].map((type) => ({ type, method: "email" })) },
      primary: true,
      conferenceProperties: { allowedConferenceSolutionTypes: ["hangoutsMeet"] },
    };
  }

  // The caller's calendar by the id a path names it by: `primary` or its address. Any other calendar,
  // another account's included, is one the caller has no access to, which Calendar answers as not
  // found.
  const calendarNamed = (id, email) => (id === "primary" || id.toLowerCase() === email.toLowerCase() ? calendarOf(email) : null);

  // A page token is opaque to a caller; this fake's holds the offset of the page's first item.
  const pageTokenOf = (offset) => Buffer.from(`page:${offset}`).toString("base64url");
  function offsetOfPageToken(token) {
    const m = Buffer.from(token, "base64url").toString("utf-8").match(/^page:(\d+)$/);
    return m && pageTokenOf(Number(m[1])) === token ? Number(m[1]) : null;
  }

  function listEvents(url, calendar, keys) {
    const params = url.searchParams;
    const zone = calendar.time_zone;
    const bound = (name) => {
      const value = params.get(name);
      if (value === null) return null;
      return TIMESTAMP.test(value) && !Number.isNaN(Date.parse(value)) ? Date.parse(value) : NaN;
    };
    const timeMin = bound("timeMin");
    const timeMax = bound("timeMax");
    if (Number.isNaN(timeMin) || Number.isNaN(timeMax)) return badRequest();
    if (timeMin !== null && timeMax !== null && timeMin >= timeMax) return timeRangeEmpty("timeMax");
    const orderBy = params.get("orderBy");
    if (orderBy !== null && orderBy !== "startTime" && orderBy !== "updated") return invalidParameter("orderBy", orderBy);
    // A seed holds no recurring event, so singleEvents changes nothing but which orderings are open.
    if (orderBy === "startTime" && params.get("singleEvents") !== "true") return badRequest("The requested ordering is not available for the particular query.");
    let maxResults = 250;
    if (params.has("maxResults")) {
      const raw = params.get("maxResults");
      if (!/^\d+$/.test(raw) || Number(raw) < 1) return invalidParameter("maxResults", raw);
      maxResults = Math.min(Number(raw), 2500);
    }
    const token = params.get("pageToken");
    const offset = token === null ? 0 : offsetOfPageToken(token);
    if (offset === null) return invalidParameter("pageToken", token);

    let events = eventsOf(calendar.user_email).filter((e) => params.get("showDeleted") === "true" || e.status !== "cancelled");
    // timeMin bounds an event's end and timeMax its start, both exclusively.
    if (timeMin !== null) events = events.filter((e) => endMs(e, zone) > timeMin);
    if (timeMax !== null) events = events.filter((e) => startMs(e, zone) < timeMax);
    // Calendar's search is over its full text, which this fake does not have: each word of q is
    // looked for in an event's summary, description, location, guests and organizer, and the gap is
    // counted.
    const q = params.get("q");
    if (q !== null) {
      keys.push("google:calendar:q");
      const words = q.toLowerCase().split(/\s+/).filter(Boolean);
      const text = (e) => [e.summary, e.description ?? "", e.location ?? "", e.user_email, ...e.attendees.flatMap((a) => [a.email, a.display_name ?? ""])].join(" ").toLowerCase();
      events = events.filter((e) => words.every((w) => text(e).includes(w)));
    }
    // The order Calendar leaves unspecified is stable here: by start, then by id.
    const byStart = (a, b) => startMs(a, zone) - startMs(b, zone) || a.google_id.localeCompare(b.google_id);
    events.sort(orderBy === "updated" ? (a, b) => Date.parse(a.updated_at) - Date.parse(b.updated_at) || byStart(a, b) : byStart);
    const page = events.slice(offset, offset + maxResults);
    const updatedMs = calendarUpdatedMs(calendar);
    const answer = {
      kind: "calendar#events",
      etag: etagOf(updatedMs),
      summary: calendar.summary,
      updated: new Date(updatedMs).toISOString(),
      timeZone: zone,
      accessRole: calendar.access_role,
      defaultReminders: DEFAULT_REMINDERS,
    };
    if (offset + maxResults < events.length) answer.nextPageToken = pageTokenOf(offset + maxResults);
    answer.items = page.map(eventResource);
    return Response.json(answer);
  }

  // An inserted event's side, from the resource's {date} or {dateTime, timeZone}; a refusal otherwise.
  function sideOfInsert(value, name, calendarZone) {
    if (!value || typeof value !== "object") return requiredField(`Missing ${name} time.`);
    if (typeof value.date === "string") return DATE.test(value.date) ? { date: value.date } : invalidField(`Invalid ${name} time.`);
    if (typeof value.dateTime !== "string") return requiredField(`Missing ${name} time.`);
    const zone = value.timeZone ?? null;
    if (zone !== null && (typeof zone !== "string" || !isZone(zone))) return invalidField(`Invalid time zone definition for ${name} time.`);
    if (TIMESTAMP.test(value.dateTime) && !Number.isNaN(Date.parse(value.dateTime))) return { ms: Date.parse(value.dateTime), zone: zone ?? calendarZone };
    if (!LOCAL_TIMESTAMP.test(value.dateTime)) return invalidField(`Invalid ${name} time.`);
    if (zone === null) return requiredField(`Missing time zone definition for ${name} time.`);
    return { ms: fromLocal(value.dateTime, zone), zone };
  }

  // What an insert's resource may state and this fake keeps; any other field is counted, under
  // google:calendar:body:<field>, and not kept.
  const INSERTED_FIELDS = new Set(["kind", "id", "status", "summary", "description", "location", "start", "end", "attendees"]);
  async function insert(req, calendar, keys) {
    let body;
    try {
      body = await req.json();
    } catch {
      return calendarError(400, "Parse Error", "parseError");
    }
    if (!body || typeof body !== "object" || Array.isArray(body)) return calendarError(400, "Parse Error", "parseError");
    for (const field of Object.keys(body)) if (!INSERTED_FIELDS.has(field)) keys.push(`google:calendar:body:${field}`);
    const zone = calendar.time_zone;
    const start = sideOfInsert(body.start, "start", zone);
    if (start instanceof Response) return start;
    const end = sideOfInsert(body.end, "end", zone);
    if (end instanceof Response) return end;
    if ((start.date === undefined) !== (end.date === undefined)) return invalidField("Start and end times must either both be date or both be dateTime.");
    if (start.date !== undefined ? end.date <= start.date : end.ms <= start.ms) return timeRangeEmpty(null);
    for (const field of ["summary", "description", "location"]) if (body[field] != null && typeof body[field] !== "string") return invalidField(`Invalid value for: ${field}`);
    const attendees = body.attendees ?? [];
    if (!Array.isArray(attendees) || attendees.some((a) => typeof a?.email !== "string" || !a.email.includes("@"))) return invalidField("Invalid attendee email.");
    let id = body.id;
    if (id !== undefined) {
      if (typeof id !== "string" || !EVENT_ID.test(id)) return invalidField("Invalid resource id value.");
      if (eventsOf(calendar.user_email).some((e) => e.google_id === id)) return calendarError(409, "The requested identifier already exists.", "duplicate");
    } else id = base32hex(crypto.randomBytes(17), 26);
    // An event an agent writes is dated by the trial's present.
    const rec = insertEvent({
      owner: calendar.user_email,
      id,
      summary: body.summary,
      description: body.description,
      location: body.location,
      attendees: attendees.map((a) => ({ email: a.email, name: a.displayName, response_status: RESPONSE_STATUSES.has(a.responseStatus) ? a.responseStatus : undefined })),
      times: timesOf(start, end),
      dated: new Date(clock.now()).toISOString(),
    });
    return Response.json(eventResource(rec));
  }

  // A path under /calendar/v3/ as a template: the segment after a collection is a record's id, unless
  // it is one of the collection's own methods.
  const ID_OF = { calendars: "{calendarId}", calendarList: "{calendarId}", events: "{eventId}", acl: "{ruleId}", settings: "{setting}" };
  const COLLECTION_METHODS = new Set(["watch", "quickAdd", "import", "clear", "stop"]);
  const templateOf = (segments) =>
    segments
      .map((s, i) => {
        const before = segments[i - 1];
        if (before === "users") return s === "me" ? "me" : "{userId}";
        return before in ID_OF && i > 0 && !COLLECTION_METHODS.has(s) ? ID_OF[before] : s;
      })
      .join("/");

  // The query parameters each method serves; any other but the few that change nothing in an answer
  // is counted, under google:calendar:<parameter>, and ignored.
  const SERVED_PARAMETERS = {
    "GET users/me/calendarList": ["maxResults", "pageToken", "showDeleted", "showHidden", "minAccessRole"],
    "GET users/me/calendarList/{calendarId}": [],
    "GET calendars/{calendarId}": [],
    "GET calendars/{calendarId}/events": ["timeMin", "timeMax", "singleEvents", "orderBy", "maxResults", "pageToken", "showDeleted", "q"],
    "POST calendars/{calendarId}/events": ["sendUpdates", "sendNotifications"],
    "GET calendars/{calendarId}/events/{eventId}": [],
    "DELETE calendars/{calendarId}/events/{eventId}": ["sendUpdates", "sendNotifications"],
  };
  const INERT_PARAMETERS = new Set(["alt", "prettyPrint", "key", "quotaUser", "alwaysIncludeEmail", "access_token", "$.xgafv", "callback"]);

  const unsupportedOf = new WeakMap();
  async function route(req, url, email) {
    const keys = [];
    unsupportedOf.set(req, keys);
    const segments = url.pathname.slice("/calendar/v3/".length).split("/").filter(Boolean).map(decodeURIComponent);
    const action = `${req.method} ${templateOf(segments)}`;
    const served = SERVED_PARAMETERS[action];
    if (!served) {
      keys.push(`google:calendar:${action}`);
      return noSuchPath(url.pathname);
    }
    for (const name of new Set(url.searchParams.keys())) if (!served.includes(name) && !INERT_PARAMETERS.has(name)) keys.push(`google:calendar:${name}`);

    if (action === "GET users/me/calendarList") return Response.json({ kind: "calendar#calendarList", etag: etagOf(Date.parse(calendarOf(email).created_at)), items: [calendarListEntry(calendarOf(email))] });
    const calendar = calendarNamed(segments[segments[0] === "users" ? 3 : 1], email);
    if (!calendar) return notFound();
    if (action === "GET users/me/calendarList/{calendarId}") return Response.json(calendarListEntry(calendar));
    if (action === "GET calendars/{calendarId}")
      return Response.json({
        kind: "calendar#calendar",
        etag: etagOf(Date.parse(calendar.created_at)),
        id: calendar.google_id,
        summary: calendar.summary,
        timeZone: calendar.time_zone,
        conferenceProperties: { allowedConferenceSolutionTypes: ["hangoutsMeet"] },
      });
    if (action === "GET calendars/{calendarId}/events") return listEvents(url, calendar, keys);
    if (action === "POST calendars/{calendarId}/events") return insert(req, calendar, keys);
    const event = eventsOf(email).find((e) => e.google_id === segments[3]);
    if (!event) return notFound();
    // A deleted event is still read, as cancelled; deleting it again is refused as gone.
    if (req.method === "GET") return Response.json(eventResource(event));
    if (event.status === "cancelled") return calendarError(410, "Resource has been deleted", "deleted");
    Object.assign(event, { status: "cancelled", updated_at: new Date(clock.now()).toISOString() });
    return new Response(null, { status: 204 });
  }

  return {
    add,
    route,
    earliest,
    settle,
    unsupportedKeys: (req) => unsupportedOf.get(req) ?? [],
    counts: () => ({ calendars: gs.calendars.all().length, calendar_events: gs.calendarEvents.all().filter((e) => e.status !== "cancelled").length }),
  };
}
