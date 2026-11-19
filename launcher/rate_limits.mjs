// Rate limits: each service declares the limits its real API enforces (its module's `limits` export,
// see launcher.mjs), and this module enforces them in the request path, so a caller that outruns the
// real service meets the refusals the real service would send. Nothing here knows a service: what
// is limited, by how much and what a refusal looks like are all the policy's.
//
// A policy is:
//   buckets      {name: {limit, windowSeconds, algorithm, maxCost?}}: what one bucket holds, in its own
//                unit (requests, or points for a quota weighted by cost). With algorithm "window" a
//                scope may spend at most `limit` within any `windowSeconds`; with "leaky" it holds at
//                most `limit`, which drains at limit/windowSeconds. A request costing more than
//                `maxCost` or `limit` never fits and is refused outright.
//   bucketOf(req, url, service) -> [{bucket, scope, cost}]
//                the buckets a request draws on, each with the scope whose window it shares (a
//                workspace, a user) and what it costs there; empty for a request nothing limits
//   refuse(req, retryAfterSeconds, bucket, cost) -> Response
//                the vendor's own refusal; retryAfterSeconds is null for a request that never fits,
//                and cost is what the request would have drawn on the refusing bucket
//   headers(standing) -> {name: value}, optional
//                the vendor's headers, set on every answer to a limited request, from each bucket's
//                standing once an admitted request is counted, or as the scope stands for a refused
//                one: {bucket: {limit, remaining, resetMs, cost}}, resetMs being when the scope's
//                bucket is next whole (epoch ms)
// A refusal that waiting cures carries the standing headers and Retry-After, in whole seconds; one
// that never fits carries neither, as a vendor refusing a malformed request sends none. A refused
// request draws on no bucket, so a caller that retries after Retry-After is admitted.

// A bucket's name outside its service, "<service>:<bucket>".
export const bucketName = (serviceName, bucket) => `${serviceName}:${bucket}`;

// createRateLimits({enabled, now, windowSeconds, bucketLimits}): `now()` is the launcher's clock in
// epoch ms; `windowSeconds`, when set, replaces every bucket's window (a test's, so one can be waited
// out); `bucketLimits` holds, by bucket name, a limit that replaces that bucket's own (a test's, so
// one fills in a few requests). It returns guard(service, policy), the service with its route
// limited (unchanged when disabled), and rateLimited, the count of refusals by bucket name.
export function createRateLimits({ enabled, now, windowSeconds, bucketLimits }) {
  const rateLimited = {};
  // What each scope of each bucket has spent: a list of {at, cost} for a window, {level, at} for a
  // leaky bucket.
  const spent = new Map();

  // Where a request leaves one bucket's scope: whether it fits, how long until it would, and the
  // scope's standing both once the request is counted and as it stands without it. `commit` counts it.
  function standingOf(spec, key, cost, t) {
    const windowMs = spec.windowSeconds * 1000;
    const fitsEver = cost <= Math.min(spec.limit, spec.maxCost ?? Infinity);
    if (spec.algorithm === "leaky") {
      const rate = spec.limit / windowMs;
      const prior = spent.get(key) ?? { level: 0, at: t };
      const level = Math.max(0, prior.level - (t - prior.at) * rate);
      const fits = fitsEver && level + cost <= spec.limit;
      const standingAt = (held) => ({ limit: spec.limit, remaining: Math.max(0, Math.floor(spec.limit - held)), resetMs: Math.ceil(t + held / rate), cost });
      return {
        fits,
        retryMs: fitsEver && !fits ? (level + cost - spec.limit) / rate : null,
        counted: standingAt(level + cost),
        uncounted: standingAt(level),
        commit: () => spent.set(key, { level: level + cost, at: t }),
      };
    }
    const entries = (spent.get(key) ?? []).filter((e) => e.at > t - windowMs);
    const used = entries.reduce((sum, e) => sum + e.cost, 0);
    const fits = fitsEver && used + cost <= spec.limit;
    let retryMs = null;
    if (fitsEver && !fits) {
      // The wait until enough of the oldest spending leaves the window to make room.
      let freed = 0;
      for (const e of entries) {
        freed += e.cost;
        if (used - freed + cost <= spec.limit) {
          retryMs = e.at + windowMs - t;
          break;
        }
      }
    }
    const withRequest = [...entries, { at: t, cost }];
    const standingAt = (kept) => ({
      limit: spec.limit,
      remaining: Math.max(0, spec.limit - kept.reduce((sum, e) => sum + e.cost, 0)),
      resetMs: kept.length ? kept[kept.length - 1].at + windowMs : t,
      cost,
    });
    return { fits, retryMs, counted: standingAt(withRequest), uncounted: standingAt(entries), commit: () => spent.set(key, withRequest) };
  }

  function withHeaders(res, extra) {
    const headers = new Headers(res.headers);
    for (const [k, v] of Object.entries(extra)) headers.set(k, String(v));
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
  }

  function guard(service, policy) {
    const refusedBy = new WeakMap();
    const rateLimitedBucket = (req) => refusedBy.get(req) ?? null;
    if (!enabled) return { ...service, rateLimitedBucket };
    // The buckets as this run keeps them: each one's own, with a test's window and limit in place of its own.
    const buckets = Object.fromEntries(
      Object.entries(policy.buckets).map(([bucket, spec]) => [bucket, { ...spec, windowSeconds: windowSeconds ?? spec.windowSeconds, limit: bucketLimits[bucketName(service.name, bucket)] ?? spec.limit }]),
    );
    async function route(req, url) {
      const draws = await policy.bucketOf(req, url, service);
      if (!draws.length) return service.route(req, url);
      const t = now();
      const assessed = draws.map((d) => ({ ...d, ...standingOf(buckets[d.bucket], `${d.bucket}\u0000${d.scope}`, d.cost, t) }));
      const refused = assessed.filter((a) => !a.fits);
      const standing = Object.fromEntries(assessed.map((a) => [a.bucket, refused.length ? a.uncounted : a.counted]));
      const headers = policy.headers ? policy.headers(standing) : {};
      if (refused.length) {
        // A request that never fits is refused as such; otherwise the longest wait is the one to name.
        const never = refused.find((a) => a.retryMs === null);
        const by = never ?? refused.reduce((a, b) => (b.retryMs > a.retryMs ? b : a));
        const key = bucketName(service.name, by.bucket);
        rateLimited[key] = (rateLimited[key] ?? 0) + 1;
        refusedBy.set(req, key);
        const retryAfter = never ? null : Math.max(1, Math.ceil(by.retryMs / 1000));
        return withHeaders(policy.refuse(req, retryAfter, by.bucket, by.cost), retryAfter === null ? {} : { ...headers, "Retry-After": retryAfter });
      }
      for (const a of assessed) a.commit();
      return withHeaders(await service.route(req, url), headers);
    }
    return { ...service, route, rateLimitedBucket };
  }

  return { guard, rateLimited };
}
