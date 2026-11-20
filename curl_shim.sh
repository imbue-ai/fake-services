#!/bin/bash
# A curl that sends requests for the faked hosts to the fakes. A program whose HTTP client is a curl it
# is told of (latchkey's gateway reads `LATCHKEY_CURL`) is pointed at this file; it runs this for
# every request, and the request reaches the fake or the real host as the routes say.
#
# Two files beside this script say what it does, both read on every request:
#   curl_router   one line, the path of the curl the program would have run itself
#   curl_routes   one faked host a line, as `<shell pattern> <port>`; absent when nothing is faked
# A request for a faked host is pointed at the fake's loopback port, with the host it was for in an
# `X-Forwarded-Host` header, which is how the launcher tells a service's API hosts from its others.
# Every other argument and every other URL is passed on untouched, and with no routes file this
# script is the router under another name. Either way the request is made by the named curl.
#
# A host is matched whatever its case, over http or https, with or without its default port. The
# value of an option that takes one (a body, a header) is never read as a URL.
set -euo pipefail

here="$(dirname "$0")"
router=""
if [ -r "$here/curl_router" ]; then
  read -r router < "$here/curl_router" || true
fi
# A box whose app has no router runs the system curl, and so does this.
if [ -z "$router" ] || [ ! -x "$router" ]; then
  router="curl"
fi
routes="$here/curl_routes"
if [ ! -r "$routes" ]; then
  exec "$router" "$@"
fi

patterns=()
ports=()
while read -r pattern port; do
  if [ -n "$pattern" ] && [ -n "$port" ]; then
    patterns+=("$pattern")
    ports+=("$port")
  fi
done < "$routes"

args=()
forwarded_host=""
is_option_value=""
for arg in "$@"; do
  if [ -n "$is_option_value" ]; then
    is_option_value=""
  elif [[ "$arg" == -d || "$arg" == --data || "$arg" == --data-* || "$arg" == -H || "$arg" == --header \
    || "$arg" == -F || "$arg" == --form || "$arg" == --form-string || "$arg" == -e || "$arg" == --referer \
    || "$arg" == -A || "$arg" == --user-agent || "$arg" == -b || "$arg" == --cookie || "$arg" == --json ]]; then
    is_option_value="yes"
  else
    lowered="$(printf '%s' "$arg" | tr '[:upper:]' '[:lower:]')"
    if [[ "$lowered" == https://* || "$lowered" == http://* ]]; then
      rest="${arg#*://}"
      authority="${rest%%[/?#]*}"
      host="$(printf '%s' "$authority" | tr '[:upper:]' '[:lower:]')"
      if [[ "$lowered" == https://* ]]; then
        host="${host%:443}"
      else
        host="${host%:80}"
      fi
      for index in "${!patterns[@]}"; do
        # Unquoted on purpose: the entry is a pattern.
        # shellcheck disable=SC2053
        if [[ "$host" == ${patterns[$index]} ]]; then
          arg="http://127.0.0.1:${ports[$index]}${rest#"$authority"}"
          forwarded_host="$host"
          break
        fi
      done
    fi
  fi
  args+=("$arg")
done
if [ -n "$forwarded_host" ]; then
  args+=(-H "X-Forwarded-Host: $forwarded_host")
fi
exec "$router" "${args[@]}"
