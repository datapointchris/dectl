# CHANGELOG


## v0.4.0 (2026-07-09)

### Features

- **logs**: Pretty-print structured json log events
  ([`1178791`](https://github.com/datapointchris/dectl/commit/1178791a297d5726228a688d2bed8e1d12b3c030))

Structured loggers (durable functions, python-json-logger) emit each event as one dense JSON line
  with any traceback collapsed into a single \n-escaped field, which is unreadable when tailed
  verbatim. render_event detects when the whole message is a JSON object, lifts timestamp/level/
  message into a colored header, lists remaining fields, and re-expands traceback fields into
  syntax-highlighted frames via rich Syntax. Values are escaped so log text containing brackets is
  not parsed as Rich markup. Non-JSON lines print verbatim -- no partial parsing of half-structured
  input. Wired into both the Glue and Lambda tail loops.

- **logs**: Tag glue events with source stream
  ([`b4f2212`](https://github.com/datapointchris/dectl/commit/b4f22122ab51d274651621122a4065a257cfc73b))

Both the output and error log groups are tailed, but only the error group was tagged, and its
  [ERROR] label conflated the error stream with error-level logs -- misleading on Glue where INFO
  records land in the error group. Tag every event with its source stream (out/err) via a testable
  stream_prefix helper, so a line duplicated across both groups by a propagating logger in the job
  is obvious at a glance.


## v0.3.0 (2026-07-08)

### Documentation

- Rewrite README around command grammar and dev loop
  ([`3e464a0`](https://github.com/datapointchris/dectl/commit/3e464a0660544c5fadf9e25905e6438bd8dd8a00))

Replace stale usage examples (that did not match the actual command tree) with the PIPELINE RESOURCE
  ACTION grammar, the lambda dev-loop vs --publish release distinction, and config guidance.

### Features

- **cli**: Make help config-aware at every level
  ([`7811ec5`](https://github.com/datapointchris/dectl/commit/7811ec562b1c418dd01823fc602008723f4104bc))

The command tree is generated from config, but help never showed which pipelines, resources, or
  aliases existed, forcing reliance on shell history. Surface the config in help everywhere:

- root help teaches the PIPELINE RESOURCE ACTION [ALIAS] grammar and groups commands into
  Global/Pipelines panels with resource summaries - no_args_is_help on every group, so partial
  commands show a menu instead of erroring - inject available job/function aliases into app help,
  argument help, and per-command example epilogs

Also fixes two bugs found during review: the update command pointed at ~/code/dectl (source lives at
  ~/tools/dectl), and removes unreadable [dim] Rich markup from config show and the Jenkins log
  tailer.

- **lambda**: Publish and promote alias on deploy
  ([`3cd21ba`](https://github.com/datapointchris/dectl/commit/3cd21ba510d409a609ff546a3f0b746a173b7ecd))

deploy previously only updated $LATEST, so functions invoked through an alias (S3 triggers, durable
  functions) kept running the last published version. Add --publish to update $LATEST, wait for it
  to settle, publish an immutable version, and repoint the function's configured alias to it.

Add an optional alias field to LambdaConfig; when set, --publish moves it.


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
