// Faults: a service that misbehaves the way the real one does on a bad day, so a downloader's
// handling of refusals and failures is exercised rather than assumed. A seed opts in with a `faults`
// object; without one nothing here applies.
//
//   {
//     "after_requests": 50,            // the first N requests to the service are never faulted
//     "rate_limit_episode_every": 600, // every Nth request opens an episode ...
//     "rate_limit_episode_seconds": 90,// ... during which every request is refused as rate limited,
//                                      //     Retry-After naming the seconds left in it
//     "transient_error_every": 250,    // every Nth request is answered with the service's own
//                                      //     transient server error
//     "unreadable_every": 700          // every Nth request is answered with an HTML error page, 503
//   }
//
// Every period counts the service's requests in order, so the pattern is fixed for a given request
// sequence and the same for every trial; 0 or a missing period turns that fault off. A faulted
// request reaches no service: it draws on no rate limit bucket and changes no store. Like the real
// thing, an episode's refusals name how long to wait and a transient failure passes on retry.
//
// What a refusal and a transient error look like is each service's (its module's `faults` export):
//   rateLimited(req, url, retryAfterSeconds) -> Response   the vendor's rate limit refusal
//   transientError(req, url) -> Response                   the vendor's transient server error
// The unreadable answer is the same for every service: an HTML page from an edge that could not
// reach the API, which no JSON parser reads.
//
// createFaults({config, now}): `now()` is the launcher's clock in epoch ms. It returns
// guard(service, shapes), the service with its route faulted (unchanged with no config), and faults,
// the count of faulted requests by "<service>:<kind>".
const PERIOD_KEYS = { rate_limit_episode: "rate_limit_episode_every", transient_error: "transient_error_every", unreadable: "unreadable_every" };
const KNOWN_KEYS = new Set(["after_requests", "rate_limit_episode_seconds", ...Object.values(PERIOD_KEYS)]);

// Why a seed's `faults` cannot be served, or null when it can. Mirrored by the harness's parse_faults.
export function validateFaults(config) {
  if (config === undefined) return null;
  if (typeof config !== "object" || config === null || Array.isArray(config)) return "faults must be an object";
  for (const key of Object.keys(config)) if (!KNOWN_KEYS.has(key)) return `faults has an unknown key ${JSON.stringify(key)}; it takes ${[...KNOWN_KEYS].sort().join(", ")}`;
  for (const [key, value] of Object.entries(config)) if (!Number.isInteger(value) || value < 0) return `faults.${key} must be a whole number of 0 or more, got ${JSON.stringify(value)}`;
  if ((config.rate_limit_episode_every ?? 0) > 0 && !(config.rate_limit_episode_seconds > 0)) return "faults.rate_limit_episode_seconds must be positive when rate_limit_episode_every is";
  return null;
}

const HTML_ERROR = "<!DOCTYPE html>\n<html><head><title>Server Error</title></head><body><h1>Server Error</h1><p>Something went wrong. Please try again.</p></body></html>\n";
const unreadable = () => new Response(HTML_ERROR, { status: 503, headers: { "content-type": "text/html; charset=utf-8" } });

export function createFaults({ config, now }) {
  const faults = {};
  function guard(service, shapes) {
    const faultOf = new WeakMap();
    const faulted = (req) => faultOf.get(req) ?? null;
    if (!config) return { ...service, faulted };
    let count = 0;
    let episodeEndsMs = null;
    const period = (kind) => config[PERIOD_KEYS[kind]] ?? 0;
    const due = (kind) => period(kind) > 0 && count % period(kind) === 0;
    function fault(req, kind, response) {
      const key = `${service.name}:${kind}`;
      faults[key] = (faults[key] ?? 0) + 1;
      faultOf.set(req, key);
      return response;
    }
    async function route(req, url) {
      count += 1;
      if (count <= (config.after_requests ?? 0)) return service.route(req, url);
      const t = now();
      if (episodeEndsMs !== null && t < episodeEndsMs)
        return fault(req, "rate_limit_episode", shapes.rateLimited(req, url, Math.max(1, Math.ceil((episodeEndsMs - t) / 1000))));
      episodeEndsMs = null;
      if (due("rate_limit_episode")) {
        episodeEndsMs = t + config.rate_limit_episode_seconds * 1000;
        return fault(req, "rate_limit_episode", shapes.rateLimited(req, url, config.rate_limit_episode_seconds));
      }
      if (due("transient_error")) return fault(req, "transient_error", shapes.transientError(req, url));
      if (due("unreadable")) return fault(req, "unreadable", unreadable());
      return service.route(req, url);
    }
    return { ...service, route, faulted };
  }
  return { guard, faults };
}
