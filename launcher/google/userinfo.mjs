// The caller's profile for the fake Google, as Google's userinfo endpoints serve it: the OpenID
// Connect shape on openidconnect.googleapis.com/v1/userinfo and www.googleapis.com/oauth2/v3/userinfo,
// and the legacy shape on www.googleapis.com/oauth2/v2/userinfo. The account is the seed's: its id,
// its address (verified), and its name with the given and family names split from it when the seed
// gives one. A seed holds no picture, so none is served. See google.mjs for the product interface
// this module exports.
import { noSuchPath } from "./errors.mjs";

const OIDC_PATHS = new Set(["/v1/userinfo", "/oauth2/v3/userinfo"]);
const LEGACY_PATH = "/oauth2/v2/userinfo";
export const prefixes = [...OIDC_PATHS, LEGACY_PATH];

// Google states no rate limit for userinfo, so a request draws on none.
export const limits = { buckets: {}, bucketOf: () => [], refuse: () => null };

export function create({ gs }) {
  const named = new Map();

  function add(cfg) {
    for (const u of cfg.users ?? []) if (u.name) named.set(u.email, u.name);
    return {};
  }

  const unsupportedOf = new WeakMap();
  function route(req, url, email) {
    // OpenID Connect has the endpoint take GET and POST alike.
    const served = (req.method === "GET" || req.method === "POST") && prefixes.includes(url.pathname);
    if (!served) {
      unsupportedOf.set(req, [`google:path:${url.pathname}`]);
      return noSuchPath(url.pathname);
    }
    const user = gs.users.findOneBy("email", email);
    const name = named.get(email);
    // A name part the seed's name lacks is left out, as Google leaves out a field it holds no value for.
    const names = Object.fromEntries(Object.entries(name ? { name, given_name: user.given_name, family_name: user.family_name } : {}).filter(([, v]) => v));
    if (url.pathname === LEGACY_PATH) return Response.json({ id: user.uid, email, verified_email: true, ...names });
    return Response.json({ sub: user.uid, ...names, email, email_verified: true });
  }

  return {
    add,
    route,
    earliest: () => null,
    unsupportedKeys: (req) => unsupportedOf.get(req) ?? [],
    counts: () => ({}),
  };
}
