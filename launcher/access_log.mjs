// The access log: one line per request that reaches a fake, and the count of every call a fake does
// not support. Every line carries the phase of the trial it was made in: "agent" until the driver
// marks another (setPhase), so the requests the verification flows cause are told apart from the
// ones the agent's app made while the agent worked. See launcher.mjs for the service interface this
// wraps, and rate_limits.mjs for the rateLimitedBucket(req) its guard adds to every service.
import crypto from "node:crypto";
import { appendFileSync, writeFileSync } from "node:fs";

const BODY_LIMIT = 8192;

// What a call the fake does not support is counted under: nothing for one it does. A fake that
// answers "no such method" for a Slack method, or answers a public Linear field, argument or filter
// key empty, has a gap where the real service has data, and the count is how the gap is seen. A
// Linear query the public schema refuses is the caller's mistake, as it would be against Linear.
export function createAccessLog(path, services) {
  if (path) writeFileSync(path, "");
  const lines = [];
  const unsupportedCalls = {};
  let phase = "agent";
  // Per phase, in the order the phases were first marked: its requests by service, its calls the
  // fakes do not support by what was asked, and its rate limit refusals by "<service>:<bucket>".
  // Without a prototype, so a phase named like one of Object's own keys ("constructor") gets counts
  // of its own.
  const phases = Object.create(null);
  const phaseCounts = (name) => (phases[name] ??= { requests: Object.fromEntries(services.map((s) => [s.name, 0])), unsupported_calls: {}, rate_limited: {} });
  phaseCounts(phase);
  function setPhase(name) {
    const previous = phase;
    phase = name;
    phaseCounts(name);
    return previous;
  }
  // Slack takes a token as a query or form parameter too, so the logged URL and body are masked of
  // every token the seeds name.
  function withoutTokens(text) {
    let masked = text;
    for (const service of services) for (const token of service.tokenUsers.keys()) masked = masked.split(token).join("[redacted]");
    return masked;
  }
  function logged(service) {
    return async (req) => {
      const url = new URL(req.url);
      const began = Date.now();
      // The phase the request began in, as its ts is when it began.
      const requestPhase = phase;
      const bodyText = req.method === "GET" ? "" : await req.clone().text();
      // The shim names the host a request was for. A service's other hosts (file downloads, uploads)
      // are not served, and a request for one is answered here rather than by the real host. A
      // request that names none is for the service's first host.
      const host = (req.headers.get("x-forwarded-host") ?? [...service.hosts][0]).toLowerCase();
      const served = service.hosts.has(host);
      const res = served ? await service.route(req, url) : service.notFound();
      const text = await res.clone().text();
      let json = null;
      try {
        json = JSON.parse(text);
      } catch {}
      const token = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
      const unsupported = served ? service.unsupportedKeys(url, json, req) : [`${service.name}:host:${host}`];
      const inPhase = phaseCounts(requestPhase);
      inPhase.requests[service.name] += 1;
      for (const key of unsupported) {
        unsupportedCalls[key] = (unsupportedCalls[key] ?? 0) + 1;
        inPhase.unsupported_calls[key] = (inPhase.unsupported_calls[key] ?? 0) + 1;
      }
      const rateLimited = service.rateLimitedBucket(req);
      if (rateLimited) inPhase.rate_limited[rateLimited] = (inPhase.rate_limited[rateLimited] ?? 0) + 1;
      const outcome = service.outcome(json);
      const rec = {
        ts: new Date(began).toISOString(),
        seq: lines.length + 1,
        phase: requestPhase,
        service: service.name,
        method: req.method,
        url: withoutTokens(`https://${host}${url.pathname}${url.search}`),
        body: withoutTokens(bodyText.length > BODY_LIMIT ? bodyText.slice(0, BODY_LIMIT) + `...[${bodyText.length} bytes]` : bodyText),
        content_type: req.headers.get("content-type"),
        // The credential itself never reaches the log: a digest tells two tokens apart.
        token_user: token ? service.tokenUsers.get(token) ?? null : null,
        token_sha256_8: token ? crypto.createHash("sha256").update(token).digest("hex").slice(0, 8) : null,
        user_agent: req.headers.get("user-agent"),
        caller: req.headers.get("x-eval-caller"),
        status: res.status,
        ok: outcome.ok,
        error: outcome.error,
        // The first of a request's unsupported keys; the counts carry every one.
        unsupported: unsupported[0] ?? null,
        // "<service>:<bucket>" of the rate limit that refused the request, else null.
        rate_limited: rateLimited,
        result_count: outcome.result_count,
        response_bytes: Buffer.byteLength(text),
        duration_ms: Date.now() - began,
      };
      lines.push(rec);
      if (path) appendFileSync(path, JSON.stringify(rec) + "\n");
      return res;
    };
  }
  return {
    logged,
    lines,
    unsupportedCalls,
    requestCount: (name) => lines.filter((rec) => rec.service === name).length,
    setPhase,
    phase: () => phase,
    phases,
  };
}
