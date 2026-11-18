# External services launcher: third-party material

What the eval box image bakes under `/opt/eval_external_services/` for the fake Slack, Linear and
Google a trial can run (`resources/external_services/launcher/`, uploaded per trial as a `launcher/`
directory under it), and what `just minds-evals-external-services-install` installs for the
launcher's tests.

- `package.json`, `package-lock.json`: the npm packages the launcher imports, pinned by the lockfile:
  `@emulators/core`, `@emulators/slack`, `@emulators/linear` and `@emulators/google` (Apache-2.0),
  all at one version since each fake runs on the core it was built against, and `graphql`.
- `linear_schema.graphql.gz`: Linear's public GraphQL schema, which the launcher validates every query
  against, so a query fails or passes as it would against the real API and introspection shows
  Linear's own types. It is `packages/sdk/src/schema.graphql` of
  https://github.com/linear/linear at commit `87d638c0b151575505d36822839324d2287f2e06`, gzipped
  unchanged; `LINEAR_SCHEMA_LICENSE` is that repository's MIT licence. To move to a newer schema:

      gh api 'repos/linear/linear/contents/packages/sdk/src/schema.graphql?ref=<commit>' \
          -H 'Accept: application/vnd.github.raw' | gzip -9 > linear_schema.graphql.gz

  then record the commit here and run the launcher's tests, which check the fields the fake serves
  are still the schema's.
