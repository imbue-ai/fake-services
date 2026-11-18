// OpenID Connect's userinfo for the fake Google: `GET https://openidconnect.googleapis.com/v1/userinfo`
// answers the token's account as Google's OpenID Connect provider does. Latchkey checks that a Google
// credential is valid by calling this endpoint with it, once per Google credential when the box's
// gateway starts, so the fake serves it for every seeded token. The account's names are served only
// when its seed gives one; the package names an unnamed account after its address, which Google
// would not. Google's other userinfo paths (`/oauth2/v2/userinfo`, `/oauth2/v3/userinfo`) are not
// served. See google.mjs for the product interface this module exports.
import { noSuchPath } from "./errors.mjs";

export const hosts = new Set(["openidconnect.googleapis.com"]);
export const prefixes = ["/v1/userinfo"];
const PATH = "/v1/userinfo";

// Google documents no limit for userinfo, so it draws on none.
export const limits = { buckets: {}, bucketOf: () => [], refuse: null };

export function create({ gs }) {
  function add(cfg) {
    for (const u of cfg.users ?? []) if (u.name) gs.users.update(gs.users.findOneBy("email", u.email).id, { name_seeded: true });
    return {};
  }

  const unsupportedOf = new WeakMap();
  async function route(req, url, email) {
    if (req.method !== "GET" || url.pathname !== PATH) {
      unsupportedOf.set(req, [`google:path:${PATH}`]);
      return noSuchPath(url.pathname);
    }
    const user = gs.users.findOneBy("email", email);
    const names = user.name_seeded ? { name: user.name, given_name: user.given_name || undefined, family_name: user.family_name || undefined } : {};
    return Response.json({ sub: user.uid, ...names, email: user.email, email_verified: true });
  }

  return { add, route, earliest: () => null, unsupportedKeys: (req) => unsupportedOf.get(req) ?? [], counts: () => ({}) };
}
