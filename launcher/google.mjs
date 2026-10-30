// The fake Google: one fake for every Google API a task reads, on @emulators/google's app and store,
// served on each host those APIs live on and routed by path to a product module under google/.
// A request for a path no product serves is answered as Google answers a method it does not have,
// and counted. The accounts (`users`) are Google's own,
// shared by every product; each product seeds its own records for them. See launcher.mjs for the
// interface this module exports.
//
// A product module exports:
//   prefixes        the paths it serves, by prefix
//   limits          {buckets, bucketOf(req, url, user), refuse(req, retryAfterSeconds, bucket, cost)}:
//                   its part of the rate limit policy (see rate_limits.mjs), its bucket names its own
//   create({app, store, gs, clock}) -> {
//     add(records)   places a seed's or an update's records of the product, throws on one it cannot
//                    place, and returns what it added
//     route(req, url, email)
//                    answers an authenticated request for one of its paths, as the account `email`
//     unsupportedKeys(req)
//                    what the request asked for that the real API has and the product lacks
//     earliest(email) the earliest date (epoch ms) of the account's records, or null
//     counts()       its record counts for health
//   }
import crypto from "node:crypto";
import { Hono, Store, WebhookDispatcher, authMiddleware } from "@emulators/core";
import { getGoogleStore, googlePlugin, seedFromConfig } from "@emulators/google";
import { credentialsInvalid, credentialsMissing, internalError, notFound, unrouted } from "./google/errors.mjs";
import * as gmail from "./google/gmail.mjs";

export const name = "google";
export const hosts = new Set(["gmail.googleapis.com", "www.googleapis.com"]);
const PRODUCTS = [gmail];
const BASE_URL = "https://oauth2.googleapis.com";

const productOf = (url) => PRODUCTS.findIndex((p) => p.prefixes.some((prefix) => url.pathname.startsWith(prefix)));
const tokenOf = (req) => (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "").trim();

// See rate_limits.mjs for what a policy holds. Each product limits its own requests; a request no
// product serves, or with no seeded token, draws on nothing.
export const limits = {
  buckets: Object.assign({}, ...PRODUCTS.map((p) => p.limits.buckets)),
  bucketOf(req, url, service) {
    const user = service.tokenUsers.get(tokenOf(req));
    const index = productOf(url);
    return user && index >= 0 ? PRODUCTS[index].limits.bucketOf(req, url, user) : [];
  },
  refuse: (req, retryAfterSeconds, bucket, cost) => PRODUCTS.find((p) => bucket in p.limits.buckets).limits.refuse(req, retryAfterSeconds, bucket, cost),
};

// A Google account id is a 21-digit number; one derived from the address reads the same in every trial.
const accountId = (email) => "1" + BigInt("0x" + crypto.createHash("sha1").update(email).digest("hex").slice(0, 16)).toString().padStart(20, "0").slice(0, 20);

export function create(seed, clock) {
  const app = new Hono();
  const store = new Store();
  // A route that throws is answered as Google answers its own failure, and the error goes to the
  // launcher's log.
  app.onError((err, c) => {
    console.error(`google: ${c.req.method} ${c.req.path} failed:`, err);
    return internalError();
  });
  const tokenUsers = new Map((seed.tokens ?? []).map((entry) => [entry.token, entry.user]));
  // The package's routes read the caller's account from this map: a token the seed names, as its
  // account's address.
  const tokenMap = new Map([...tokenUsers].map(([token, email]) => [token, { login: email, id: 0, scopes: [] }]));
  app.use("*", authMiddleware(tokenMap, undefined, undefined));
  googlePlugin.register(app, store, new WebhookDispatcher(), BASE_URL, tokenMap);
  app.notFound(() => unrouted());
  const gs = getGoogleStore(store);
  const products = PRODUCTS.map((p) => p.create({ app, store, gs, clock }));

  // An account is dated at its earliest record in any product, else at the earliest record the fake
  // holds, else at the anchor: the package dates it with the wall clock.
  function settle(newEmails) {
    const earliestOf = (email) => Math.min(...products.map((p) => p.earliest(email) ?? Infinity));
    const floor = Math.min(...gs.users.all().map((u) => earliestOf(u.email)));
    for (const email of newEmails) {
      const at = earliestOf(email);
      const iso = Number.isFinite(at) ? new Date(at).toISOString() : Number.isFinite(floor) ? new Date(floor).toISOString() : clock.anchor;
      Object.assign(gs.users.findOneBy("email", email), { created_at: iso, updated_at: iso });
    }
  }

  function add(cfg) {
    const users = cfg.users ?? [];
    for (const u of users) if (gs.users.findOneBy("email", u.email)) throw new Error(`google user ${u.email} is listed again`);
    if (users.length) seedFromConfig(store, BASE_URL, { users: users.map((u) => ({ email: u.email, name: u.name })) });
    for (const u of users) gs.users.update(gs.users.findOneBy("email", u.email).id, { uid: accountId(u.email) });
    if (!gs.users.all().length) throw new Error("google seed names no users");
    const added = { google_users: users.length };
    for (const product of products) Object.assign(added, product.add(cfg));
    settle(users.map((u) => u.email));
    return added;
  }

  const unsupportedOf = new WeakMap();
  async function route(req, url) {
    const index = productOf(url);
    if (index < 0) {
      const [first = "", second = ""] = url.pathname.split("/").filter(Boolean);
      unsupportedOf.set(req, [`google:path:/${[first, second].filter(Boolean).join("/")}`]);
      return notFound();
    }
    const token = tokenOf(req);
    if (!token) return credentialsMissing();
    const email = tokenUsers.get(token);
    if (!email) return credentialsInvalid();
    const res = await products[index].route(req, url, email);
    unsupportedOf.set(req, products[index].unsupportedKeys(req));
    return res;
  }

  return {
    name,
    hosts,
    store,
    tokenUsers,
    add,
    route,
    notFound,
    counts: () => Object.assign({ users: gs.users.all().length }, ...products.map((p) => p.counts())),
    outcome: (json) => ({ ok: json ? !json.error : null, error: json?.error?.message ?? null, result_count: resultCount(json) }),
    unsupportedKeys: (url, json, req) => unsupportedOf.get(req) ?? [],
  };
}

function resultCount(json) {
  if (json == null || typeof json !== "object") return null;
  for (const k of ["messages", "threads", "labels", "history", "drafts"]) if (Array.isArray(json[k])) return json[k].length;
  // A list answer leaves out an empty list, as Google does.
  return "resultSizeEstimate" in json ? 0 : null;
}
