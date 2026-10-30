// Google's JSON error shape, as every Google API answers a request it refuses: the code, a message,
// one `errors` entry naming its domain and reason (with a message of its own where Google gives one
// a shorter message), and the status the code stands for.
export function googleError(code, message, reason, status, { domain = "global", entryMessage = message, ...extra } = {}) {
  return Response.json({ error: { code, message, errors: [{ message: entryMessage, domain, reason, ...extra }], status } }, { status: code });
}

export const notFound = () => googleError(404, "Requested entity was not found.", "notFound", "NOT_FOUND");

const AUTH_HINT = "Expected OAuth 2 access token, login cookie or other valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project.";
// A request with no credential, and one whose credential Google does not know.
export const credentialsMissing = () =>
  googleError(401, `Request is missing required authentication credential. ${AUTH_HINT}`, "required", "UNAUTHENTICATED", { entryMessage: "Login Required.", location: "Authorization", locationType: "header" });
export const credentialsInvalid = () =>
  googleError(401, `Request had invalid authentication credentials. ${AUTH_HINT}`, "authError", "UNAUTHENTICATED", { entryMessage: "Invalid Credentials", location: "Authorization", locationType: "header" });

export const internalError = () => googleError(500, "Internal error encountered.", "backendError", "INTERNAL");

// What the package underneath answers a path none of its routes takes, so a product can tell that
// from the package's own not-found answer for a record that is not there. The header never reaches
// a caller: a product answers such a request itself.
const UNROUTED_HEADER = "x-unrouted";
export const unrouted = () => new Response(null, { status: 404, headers: { [UNROUTED_HEADER]: "1" } });
export const isUnrouted = (res) => res.headers.has(UNROUTED_HEADER);
