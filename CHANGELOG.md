# CHANGELOG


## v2.1.2 (2026-07-30)

### Bug Fixes

- Bound the Glue log scan to the run's own start time
  ([`4b8aa9f`](https://github.com/datapointchris/dectl/commit/4b8aa9fbc41cd82c9d58af383f9691c022d570be))

Tailing a Glue run printed nothing for several minutes, then everything at once. Filtering the two
  shared groups by stream-name prefix isolates the run's streams but does not bound the scan:
  filter_log_events pages forward from startTime, and unset that is the start of the group's
  retention. Both Glue groups hold every Python Shell run in the account, so the tailer was paging
  through months of unrelated logs before reaching this run's, and it drained every page before
  rendering a line.

The run's StartedOn is the natural lower bound, so run_log_start reads it and the tailer now
  requires it. A missing log group is also reported rather than silently swallowed, since "printed
  nothing" and "nowhere to print" are different diagnoses.

Verified against a real Python Shell run: both groups readable in under a second, where before the
  same assertions would have waited minutes.


## v2.1.1 (2026-07-30)

### Bug Fixes

- Resolve the alias to a version before listing executions
  ([`049b837`](https://github.com/datapointchris/dectl/commit/049b8376e37b9811f0ed2c7b1ddb78a2ecb85b09))

Listing durable executions with the live alias as the qualifier fails with "cannot filter durable
  executions by alias". Lambda resolves an alias to a version number when an execution starts, so
  the alias name never appears in a durable execution ARN and there is nothing for the list API to
  match. The API reference says Qualifier takes "the function version or alias"; for
  ListDurableExecutionsByFunction that is wrong.

Invoking and listing therefore need different qualifiers, so qualifier_for splits: invoke_qualifier
  still sends the alias, which is correct and what triggers do, while listing_qualifier resolves it
  through GetAlias.

Resolving pins the listing to whichever version the alias points at now, so deploy --publish would
  silently drop every run from before it. The resolved version is shown alongside the alias, each
  execution's version is a column, and --all-versions merges recent published versions newest-first,
  naming the ones it scanned. Name lookups for history and logs fall back to that sweep
  automatically, since which version ran a given execution is not something you can be expected to
  know.


## v2.1.0 (2026-07-30)

### Bug Fixes

- Stream Glue run logs the moment they exist
  ([`a4af837`](https://github.com/datapointchris/dectl/commit/a4af837eaacc30f967f79210ac71ad70855d520b))

Tailing a Glue run waited for its CloudWatch streams to be created before reading anything:
  describe_log_streams on a 5-second poll for the output stream, then sequentially for the error
  stream. A job that never writes to stderr has no error stream to find, so that second wait burned
  its full 120-second timeout in silence while the output the console was already showing went
  unprinted.

Pinning the stream list at start-up also meant a traceback landing in an error stream created later
  in the run was never displayed at all.

Both groups are now filtered by stream-name prefix (the run id) instead, so an empty group costs one
  empty response rather than blocking the other, and a stream appearing mid-run is picked up on the
  next poll. All three tailers share one LogGroupCursor, which also gains the scoping and
  time-bounding a per-execution tail needs.

Following now ends when the run reaches a terminal state, draining a few passes past it since
  CloudWatch ingestion lags the run's end, and exits non-zero when the run did not succeed.

### Features

- Add durable execution verbs for durable Lambda functions
  ([`bf27d16`](https://github.com/datapointchris/dectl/commit/bf27d16d61037d88a313f5b54edef8564afa4fe5))

A durable function's unit of work is the execution, not the invocation: one execution checkpoints
  across many invocations and can suspend for up to a year between them. None of the
  invocation-shaped views answer "did this run succeed" — Invocations counts replays, and a log
  stream holds interleaved fragments of whichever executions that environment served.

A lambda flagged durable: true in config therefore swaps run and logs for an execution-scoped set:

executions which ones succeeded or failed, with elapsed time history [EXECUTION] its steps, waits,
  retries and failures logs [EXECUTION] its own logger output, filtered by execution ARN

history and logs are complements and mirror the two console tabs: history is the checkpoint log
  Lambda replays from, logs is what the function printed while doing it. The SDK logger stamps the
  execution ARN onto every record, which is what both the console and this filter on.

run is now qualified with the live alias, falling back to $LATEST — Lambda rejects an unqualified
  invoke of a durable function, since an execution is pinned to the version it starts on. It also
  gains --async, lifting the 15-minute synchronous cap, and --name for an idempotent start that
  doubles as the handle for history and logs.


## v2.0.0 (2026-07-30)

### Features

- Diff and confirm glue job definition changes
  ([`b2b2129`](https://github.com/datapointchris/dectl/commit/b2b2129d29cd92e0c9090d6f27a6f98b266c3079))

dectl and Terraform both write the Glue job definition. Terraform owns it once a pipeline is
  established, but dectl keeps write access because that is the point before Terraform exists: set
  arguments and deploy from the shell instead of commit -> Jenkins -> console. Nothing marked the
  seam, so every deploy silently reasserted a config Terraform may have moved past.

deploy now diffs its computed update against the live definition, skips UpdateJob entirely when
  nothing differs, and otherwise renders a field-level table and confirms. --plan shows it and
  exits; --yes skips the prompt for the pre-Terraform loop. Removals are reported too, since a
  detached connection is invisible in a diff that only walks the new definition.

BREAKING CHANGE: connections is authoritative rather than additive. The union could only add, so a
  stale entry silently reattached a Terraform-renamed connection on every deploy and could not be
  removed from config. Absent means unmanaged, [] detaches all.

Also adds max_capacity, rejected on worker-based Spark jobs since UpdateJob will not accept it
  alongside WorkerType.

### Breaking Changes

- Connections is authoritative rather than additive. The union could only add, so a stale entry
  silently reattached a Terraform-renamed connection on every deploy and could not be removed from
  config. Absent means unmanaged, [] detaches all.


## v1.1.1 (2026-07-30)

### Bug Fixes

- Label only the Glue error stream, align expansions
  ([`c279f08`](https://github.com/datapointchris/dectl/commit/c279f086d7fe42712d78fc16121f1a44a32e52ee))

Every line carried out or err, but out is a constant on a well-configured job (one stdout handler)
  so it spent width carrying no information. Worse, render_event prefixed only the header line,
  leaving expanded JSON fields and tracebacks orphaned at column 0 four columns left of the line
  they belong to.

stdout is now bare and err marks the exceptional stream — a traceback, the warnings module, a
  library writing direct. Continuation lines indent by the prefix's visible width (markup tags
  stripped) so a prefixed record reads as one block. monitor's per-resource prefixes are untouched:
  there many sources interleave, so every line needs its source.


## v1.1.0 (2026-07-28)

### Chores

- Add .planning to gitignore
  ([`3000d25`](https://github.com/datapointchris/dectl/commit/3000d2505487dbaef674e7e08c1682b29320a0e9))

### Continuous Integration

- Add generated validate.yml and gate release on it
  ([`2a01156`](https://github.com/datapointchris/dectl/commit/2a01156faa3654d69546c68482034c0282138abb))

Release triggered on push to main with no validation at all, so it published whatever was on main.
  Adds the forge-generated CI block (ruff check, ruff format, mypy, pytest) and makes release depend
  on it.

Verified locally before wiring the gate: all four checks pass.

- Regenerate validate.yml at toolchain 6
  ([`ceda126`](https://github.com/datapointchris/dectl/commit/ceda126e16966811f411ceb2705e4e43c911b82d))

Stamp only — the python block is unchanged. Toolchain 6 adds the pinned release-binary mechanism and
  the shell CI block.

### Features

- Warn when an explicit --env substitutes nothing
  ([`f640444`](https://github.com/datapointchris/dectl/commit/f640444d33c4b82067a528b86f141d4257150c9d))

Substitution is a literal {env} replacement, so a config that hardcodes its environment
  (salesdata-dev-ds-etl) ignores --env/DECTL_ENV entirely and acts on the wrong environment while
  still succeeding. Nothing surfaced that, so the command looked like it worked.

warn_if_environment_had_no_effect fires once per invocation when the env came from an explicit
  source (--env or DECTL_ENV, never a config default, since a config without placeholders is a
  legitimate single-env setup) and the resource carries no {env} token at all. Wired into every path
  that resolves names: render_env_model, s3's bare-string resolved_bucket and export, monitor, and
  both pipeline_view renderers.

The warning goes to stderr via the new output.warn, so --json output piped to jq and an eval'd `s3
  export` stay clean.


## v1.0.0 (2026-07-23)

### Chores

- **pre-commit**: Restrict hooks to pre-commit stage
  ([`1643fd3`](https://github.com/datapointchris/dectl/commit/1643fd33cda73e9b2889d63d030021786fb2fb90))

Add default_stages: [pre-commit] so hooks without an explicit stages: run only at the pre-commit
  stage. Without it, unrestricted hooks (ruff, codespell, bandit, etc.) also ran at the
  prepare-commit-msg and commit-msg stages, firing multiple times per commit.

### Documentation

- Fix install command and document release model
  ([`b3efb92`](https://github.com/datapointchris/dectl/commit/b3efb9291bb5c4a1b8ba6689785a937e39371c99))

The documented 'uv tool install ...@latest' failed because releases are semver tags with no moving
  'latest' ref. Point install at the default branch (always the latest release under
  python-semantic-release) and document version pinning and the local 'dectl update' path.

### Features

- Add --json to read commands and unify pipeline rendering
  ([`129a009`](https://github.com/datapointchris/dectl/commit/129a00953709fd607a85a9d8ff639bedcf8c6c6b))

Add an emit_json output helper (bare print, no rich markup, so piped output stays clean for jq) and
  a stable JSON shape for pipeline listings. Wire --json onto 'dectl list', 'PIPELINE list', 'config
  show', and 'search'. Extract the duplicated pipeline-printing from main.py and config_cmd.py into
  a shared pipeline_view module (importable by both without the main<->config_cmd import cycle).

- Verb-last CLI grammar and unified verbs
  ([`2d0fde0`](https://github.com/datapointchris/dectl/commit/2d0fde091892e610a689febec2f74163c4b3204d))

Restructure the command surface to PIPELINE RESOURCE ALIAS VERB, with the verb last so a deploy ->
  run -> logs loop on one resource changes only the trailing word. The alias becomes a
  config-assembled sub-app and each verb closes over its resolved config; {env} is substituted at
  call time. One rule governs the surface: aliased acts on one thing, unaliased on the set.

- Unify verbs: lambda invoke -> run, sfn start -> run, sfn watch -> logs, sfn list -> runs; add glue
  runs. deploy always means "update artifact". - --follow defaults off everywhere (was forced-on for
  glue/lambda logs, glue run, sfn watch); streaming is now an explicit opt-in. - lambda/sfn run take
  payloads via --payload-file PATH or - (stdin). - s3: per-bucket mount/unmount/uri sub-apps; export
  stays set-level with --prefix; new uri prints a bare s3:// for command substitution. - Pipeline
  Jenkins deploy -> release, with --plan (was --dry-run). - Config: lambda alias -> live_alias (no
  back-compat; extra=forbid rejects the old key loudly). buckets docs say alias, not shortname. -
  dectl reference prints the full grammar, config-independent. - Progressive discovery: no-args help
  at every level, each alias node doubles as an info panel, examples-first, instance/set help
  panels.

No confirmation prompts or --yes: dectl is used at the operator's own peril.

BREAKING CHANGE: command grammar is now PIPELINE RESOURCE ALIAS VERB (verb last). Renamed verbs
  (invoke/start/watch/sfn list -> run/run/logs/runs), pipeline deploy -> release (--dry-run ->
  --plan), --follow now defaults to false, inline positional JSON payloads replaced by
  --payload-file, and the lambda config field alias -> live_alias. No deprecation aliases.

### Breaking Changes

- Command grammar is now PIPELINE RESOURCE ALIAS VERB (verb last). Renamed verbs
  (invoke/start/watch/sfn list -> run/run/logs/runs), pipeline deploy -> release (--dry-run ->
  --plan), --follow now defaults to false, inline positional JSON payloads replaced by
  --payload-file, and the lambda config field alias -> live_alias. No deprecation aliases.


## v0.6.1 (2026-07-15)

### Bug Fixes

- **s3**: Mount buckets under ~/buckets instead of cache dir
  ([`531d6ae`](https://github.com/datapointchris/dectl/commit/531d6ae16e94036d2334c622b816a7593be3a245))

The old ~/.cache/dectl/mounts/PIPELINE/SHORTNAME path was buried and impractical to cd into. Mount
  at ~/buckets/PIPELINE/SHORTNAME instead, keeping the pipeline segment so buckets sharing a
  shortname across pipelines don't collide.


## v0.6.0 (2026-07-15)

### Features

- **config**: Add example, edit, validate, path
  ([`12d3652`](https://github.com/datapointchris/dectl/commit/12d3652a02697b3a26d801287a55facec6ea2d2b))

Add four config-management commands to the always-present config app:

- example: print the full template config (one of every option) to stdout, syntax-highlighted on a
  TTY and plain when piped, for side-by-side reference while editing the real config in another
  pane. - edit: open the config in $VISUAL then $EDITOR (no hardcoded editor), shlex-split so args
  survive, binary resolved via shutil.which; seeds from the template if none exists. Foreground, so
  terminal editors block and GUI editors follow their own --wait semantics. - validate: parse the
  file and report the exact failing config path. - path: bare-print the config path for shell
  substitution.

Viewing is done in-process via rich.syntax.Syntax rather than shelling out to a pager, so there is
  no PATH lookup or $PAGER parsing and it works everywhere.

Models now inherit StrictModel (extra='forbid'), so an unknown key is a loud validate error instead
  of a silently dropped field. Because that widens what counts as invalid, main.py wraps its
  import-time load_config() so a present-but-invalid config falls back to no config (keeping the
  config commands reachable to fix it) and the root banner prints the reason, rather than crashing
  on import.


## v0.5.0 (2026-07-14)

### Bug Fixes

- **lambda**: Follow all log streams when tailing
  ([`9bc4549`](https://github.com/datapointchris/dectl/commit/9bc454937ddd3955eb4da618ef4f32b466081584))

Lambda writes each execution environment to its own CloudWatch log stream, so an invocation that
  cold-starts after the warm environment is reaped (~5-15 min idle) lands in a new stream.
  tail_lambda_logs pinned to the single newest stream at startup and followed only that one, so
  later runs were invisible until the command was killed and restarted.

Follow the whole log group with filter_log_events instead, advancing a moving startTime and deduping
  the inclusive boundary events by eventId. Glue tailing is unchanged: its run-id-named streams are
  created up front, so pinning is correct there.

### Continuous Integration

- Skip generated CHANGELOG in markdownlint
  ([`92c7827`](https://github.com/datapointchris/dectl/commit/92c782705805f4575841ce4071b69a8b39ea3ae1))

CHANGELOG.md is generated by semantic-release, which owns its format: per-version duplicate section
  headings (MD024) and blank-line spacing (MD012). Style-linting it produced an unfixable failure
  plus a blank-line auto-fix that reverted on every release. Exclude it from the markdownlint hook.

### Features

- Add multi-environment {env} substitution
  ([`8abf620`](https://github.com/datapointchris/dectl/commit/8abf620da24ec24913a7383b3f75b255f85a5df6))

Resource names in config carry an {env} placeholder (e.g. salesdata-{env}-ds-thing) that is
  substituted at runtime, so one config drives dev/staging/prod by swapping a single token instead
  of duplicating every name per environment. No derivation from inconsistent names; the substitution
  point is marked explicitly.

- --env option (envvar DECTL_ENV, default from defaults.environment) gives the priority chain --env
  > DECTL_ENV > config > dev - env.py holds the active env and substitutes {env} generically across
  every string field of a config model; applied at the resolve_* chokepoints and in the display
  loops, at runtime (never at import, since the flag is not known until the command runs) - assumes
  one AWS account across environments: names change, the session does not - the active env is
  surfaced so you know which one you are targeting: a banner on the bare dectl landing and a dectl
  env command, both showing the resolved env and its source (via Click's get_parameter_source) -
  drop top-level no_args_is_help so the callback can print the banner before help

- Add step functions and pipeline monitor
  ([`a521be7`](https://github.com/datapointchris/dectl/commit/a521be7e578cf77f0070bcfee32a94e31cfbf4c9))

Step Functions resource (sfn), mirroring the glue/lambda factory pattern: - start (with --follow),
  watch, and list executions - watch tails the GetExecutionHistory API, rendering typed state
  transitions (no CloudWatch logging setup required); a generic renderer pulls state name and
  error/cause from whichever *EventDetails field is present - config: step_functions maps alias ->
  {name, optional log_group}; the ARN is built from account_id/region like the other resources

Pipeline-level monitor command: - tails several resources at once as one timestamp-ordered stream
  with a color-coded per-resource prefix, so a multi-lambda / step function pipeline's
  cross-resource sequence reads top to bottom - what it watches is defined explicitly in a
  per-pipeline monitor config block, so the command takes no arguments and requires no recall of
  resource names - logs.tail_log_groups generalizes the glue multi-stream tailer, reusing the
  moving-startTime + eventId boundary dedup from the lambda tail fix - a monitored state machine
  must have log_group set (Express workflows only log to CloudWatch); monitor warns and skips when
  it is missing

- **s3**: Add bucket export and mount commands
  ([`c44ce55`](https://github.com/datapointchris/dectl/commit/c44ce556096368c8923cf622e2fd2e35b9b3e287))

Buckets become a shortname -> bucket-name mapping (matching the glue/lambda alias shape) instead of
  a fixed raw/curated/error schema, so a pipeline can declare any buckets it wants. Old configs
  still parse: raw/curated/error are now just ordinary shortnames.

The new s3 resource exposes: - export: prints eval-able 'export pipeline_shortname=s3://bucket'
  lines (lowercase) for use with the aws CLI, since a CLI cannot mutate its parent shell's
  environment. - mount/unmount: wrap mount-s3 (Mountpoint for Amazon S3) to expose a bucket as a
  local directory. Linux-only; refuses cleanly on macOS since FUSE is unavailable there.


## v0.4.1 (2026-07-09)

### Bug Fixes

- **glue**: Preserve existing job definition on deploy
  ([`92b9ea8`](https://github.com/datapointchris/dectl/commit/92b9ea8bc7d20818e31b5e5e0ad9f88401d6f752))

update_glue_job built a minimal JobUpdate from scratch, which broke deploys in three ways, since
  Glue's UpdateJob replaces the whole definition rather than patching it:

- Empty Connections: get_job omits the Connections key for jobs with none, so the old default
  produced {'Connections': []}, which UpdateJob rejects with "empty connections list is not allowed
  when Connections is specified". - Field reset: omitted fields (Timeout, GlueVersion, WorkerType,
  MaxRetries, ExecutionProperty, ...) and default arguments set outside dectl silently reverted to
  defaults on every deploy. - Capacity conflict: Spark jobs report a derived MaxCapacity alongside
  WorkerType/NumberOfWorkers, and echoing both back is rejected with "do not set Max Capacity if
  using Worker Type and Number of Workers".

Now start from the existing definition, strip the read-only keys UpdateJob rejects (Name, CreatedOn,
  LastModifiedOn, ProfileName, AllocatedCapacity), drop MaxCapacity when the worker-based model is
  in use, merge connections and default arguments additively, and override only Role and
  ScriptLocation.

Add unit tests for the payload logic plus an opt-in live AWS round-trip test (--run-integration)
  that creates a throwaway role and job of each type, deploys, asserts the definition survives, and
  tears everything down.

- **lambda**: Surface function errors from invoke
  ([`f76540f`](https://github.com/datapointchris/dectl/commit/f76540fc32e146958df0f41362c205f35b4c2154))

A handled or unhandled exception in a Lambda still returns HTTP 200 with an error payload;
  FunctionError is the only signal it failed. invoke printed that payload as though it were a
  successful result and exited zero. Check FunctionError, label the output as an error, and exit
  non-zero so failures are visible and scriptable.


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
