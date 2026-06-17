# CHANGELOG


## v0.2.0 (2026-06-17)

### Bug Fixes

- Interleave output and error streams in Glue log tailing
  ([`0b7c45b`](https://github.com/datapointchris/dectl/commit/0b7c45b77a4d1ca04646d130790bf9bb4a46e5c2))

Previously output stream was tailed with follow=True, blocking until it stopped before ever showing
  the error stream. Now both streams are polled in the same loop so errors appear immediately
  alongside output.

### Documentation

- Add CHANGELOG from v0.1.0 release
  ([`9d86953`](https://github.com/datapointchris/dectl/commit/9d8695387366b5117aa7f347bd130c6638926e11))

Ports the changelog that previously existed only on the orphaned main history. Commit link updated
  to 33ce5cf (the initial commit in this canonical history) since main's df445a9 root will be
  discarded.

### Features

- Add glue option for Connections
  ([`1ad588e`](https://github.com/datapointchris/dectl/commit/1ad588e096f853d37ae8e72d863b2a51614ef47f))

- Add httpx dependency for Jenkins API integration
  ([`893242b`](https://github.com/datapointchris/dectl/commit/893242b64d6307eaa63dcc57ad60477899110381))

### Refactoring

- Restructure pipelines to support multiple resource types
  ([`c6c6093`](https://github.com/datapointchris/dectl/commit/c6c6093e0d8ffae4938593fc747c93e1613b6c79))

Pipelines can now have both glue_jobs and lambdas (and Jenkins deploy) registered as sub-commands.
  Removes the single-type constraint. Adds per-pipeline list command and dectl update for
  reinstalling from source.


## v0.1.0 (2026-06-02)

### Features

- Initial dectl project
  ([`33ce5cf`](https://github.com/datapointchris/dectl/commit/33ce5cfd8de4394698014cc1477d002f65656196))

Config-driven CLI for managing AWS data engineering pipelines. Consolidates repeated justfile
  operations (Glue deploy/run/logs, Lambda deploy/invoke/logs) into a single tool with YAML config
  at ~/.config/dectl/config.yaml.
