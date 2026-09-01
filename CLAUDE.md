# dectl

Data engineering control CLI for driving AWS pipelines (Glue, Lambda, Step Functions, S3,
Jenkins deploys) from a single config file. Installed via `uv tool install`; the entry point is
`dectl.main:app`. Universal rules live in `~/.claude/CLAUDE.md` — this file only covers what
is specific to dectl.

## The core idea: the CLI surface is assembled from config at import time

There is no static command tree. `src/dectl/main.py` calls `load_config()` **at module import**
and then builds the Typer app dynamically: it loops over the pipelines in
`$XDG_CONFIG_HOME/dectl/config.yaml` and, for each one, attaches only the resource sub-apps
that the pipeline actually defines. A pipeline with just `buckets` gets an `s3` command and
nothing else; add `glue_jobs` and a `glue` command appears. Running `dectl` with no config still works
(commands like `config init` are always present) because `load_config()` returns `None` rather
than raising.

This means the command you see depends entirely on the user's config. When adding a resource
type, you touch two places: the resource's own `make_*_app` module, and the pipeline loop in
`main.py` that conditionally wires it in (plus the `resources` summary line and
`pipeline_view.render_pipeline`).

## Grammar: `PIPELINE RESOURCE ALIAS VERB` (verb last)

The command shape is `dectl PIPELINE RESOURCE ALIAS VERB [OPTIONS]` — the **verb is last** so a
deploy → run → logs loop on one resource changes only the trailing word. One rule governs the
whole surface: **aliased = one thing, unaliased = the set/pipeline.** Instance verbs take an
alias (`glue JOB run`, `s3 BUCKET mount`); set verbs take none (`s3 export`, `release`, `list`,
`monitor`). Scope is structural: the argument's presence selects it, never a flag.
`dectl reference` is the static-grammar command a config-assembled tree needs, and `--json` on
reads goes through
`output.emit_json` — bare-print, so pipes stay clean.

## Shortcuts: one stroke for a whole path

`main.py` ends with `attach(app, teaching=True, expanding=True)` — the whole of dectl's use of
[clisteno](https://github.com/datapointchris/pyclisteno). Each level of the tree gets the shortest
prefix of its own name that its siblings do not share, and they run together into one token, so
`dectl exgsr` is `dectl example-pipeline glue source-copy run`. Help rows show the sequence beside
each command, and `~/.cache/clisteno/dectl.tsv` is what the zsh hint reads.

Nothing here is dectl's to maintain: prefixes are computed from the live tree, so a new pipeline or
alias gets one for free and a removed one has its sequence retired rather than reissued. The two
things worth knowing are that **the call has to be the last line of the module** — the global
commands are registered below the pipeline loop and a walk that ran earlier would miss them — and
that a sequence which is also a real command name is withheld, so `dectl env` reaches `env`.

## The `make_*_app` factory pattern

Every resource type is a module in `src/dectl/commands/` exposing a factory:

```python
def make_<resource>_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
```

The factory returns the resource-level app (`glue`) and, for each configured alias, attaches a
**per-alias sub-app** (`make_<resource>_<thing>_app`) whose commands are the verbs. This is the
same config-assembled pattern as the pipeline loop, one level down: the alias is a tree node,
not an argument. Each verb command **closes over that alias's config** and resolves `{env}` at
call time (`render_env_model` / `substitute_env`) — never at import, since `--env` is not known
until the command runs. Because the alias is a sub-app, an unknown alias is a Typer
"no such command" (usage) error, not a `resolve_*` failure; the valid aliases are discoverable
via no-args/`--help` at the resource level, and each alias sub-app's `help=` doubles as an info
panel (its resolved name, bucket, scripts). `glue.py` and `lambda_.py` are the reference
implementations, `stepfunctions.py` and `s3.py` the newest.

`iceberg.py` is the one resource whose verbs are all reads. Its factory is the same two-level
shape, and the domain behind it lives in `src/dectl/iceberg.py` — the same split as
`durable.py` beside `lambda_.py`.

Aliases matter: users reference the short config keys (`raw`, `source-copy`), never the real
AWS names. `dectl PIPELINE list` prints the alias → AWS-name mapping.

`s3` is the one resource with a set-level verb: per-bucket `mount`/`unmount`/`uri` are alias
sub-apps, but `export` (spans every bucket, no alias) is a command on the `s3` app itself.
`lambda`/`sfn` `run` take payloads through `payloads.read_payload`: a payload comes from
`--payload-file F` or from stdin as `-`, never from an inline string argument.

`monitor` is the exception to the factory pattern: it is a **pipeline-level** command (like
`list`, registered on `pipeline_app` directly), not a resource sub-app, because it spans
several resources. It reads the pipeline's explicit `monitor` config block and tails every
selected resource's CloudWatch log group as one interleaved, time-ordered stream via
`logs.tail_log_groups`.

## Config

`src/dectl/config.py` — Pydantic models plus `load_config()` / `init_config()`. Config lives at
`$XDG_CONFIG_HOME/dectl/config.yaml`, resolved by `pyclisteno.paths.config_home()` so the
variable is honoured rather than `~/.config` being hardcoded. `buckets` is an
`alias -> real-bucket-name` mapping (same alias→name shape as `glue_jobs` and `lambdas`), not
a fixed set of roles. A lambda's
`live_alias` (renamed from `alias` to avoid colliding with the CLI "alias" = the config key) is
the AWS Lambda alias that `deploy --publish` repoints; its `durable` flag selects the durable
verb set and doubles as the invocation qualifier's source. `step_functions` maps
alias → `{name, log_group}` (the log group is optional, needed only for `monitor`).
`iceberg_tables` maps alias → `{database, table}`, the Glue Data Catalog pair that owns the
table; both fields carry `{env}`. `monitor` is
its own block listing which lambdas / step machines to tail, kept separate so the monitored view
is defined in one scannable place. When you change a model, update `TEMPLATE_CONFIG` in the same
file so `config init` stays valid — a test asserts `TEMPLATE_CONFIG` round-trips through the models.

A pipeline's optional `repo` is the directory its relative paths resolve against, which is what
lets a deploy run from any working directory. **Every config value naming a file or directory
goes through `resolve_in_repo(pipeline, value)`, never `Path(value)`** — missing it reintroduces
the dependency on where dectl was invoked, silently. `pipeline_root` is the fallback half: no
`repo` means `Path.cwd()`. Both substitute `{env}`, so a path is resolved the same way every
other name is.

**`declared_paths` is the one enumeration of which config fields hold paths.** A new
path-bearing field is added there and `config validate`, `config show` and `list --json` all see
it at once. A field added anywhere else leaves `validate` reporting nothing wrong, which is what
a correct config also reports — a failure that reads as success. `path_fault` is the shared
answer to *why* a path is unusable, and it separates absent from present-with-the-wrong-type
because those have opposite fixes.

`expand_home` is the guarded `~` expansion. `Path.expanduser()` raises `RuntimeError` for a
`~user` naming nobody, pydantic does not wrap that, and `CONFIG_LOAD_ERRORS` does not catch it —
so an unguarded call escapes `main.py`'s import-time try/except and takes down the `config`
commands that exist to repair the file. Every path field validates through it.

A glue `scripts` entry must be relative and must not climb out of the repo, because its S3 key
is built from the config string rather than from the resolved path. A lambda `source_dir` has no
such restriction: it is zipped and uploaded as bytes with no key derived from it.

`deploy` runs everything that can refuse before it writes anything — `resolve_scripts`, then
`plan_glue_job_update`, then the upload, then `apply_glue_job_update`. `build_job_update` exits
on a capacity Glue would reject and the confirmation can be declined, and either one landing
after an upload leaves `ScriptLocation` pointing at code the user was just told not to deploy.

`TEMPLATE_CONFIG` is the single source for the example config: `config init` writes it, `config
example` prints it (syntax-highlighted on a TTY, plain when piped so `config example > config.yaml`
stays clean), and it exercises every option so it doubles as side-by-side reference while editing.
Its `repo` line stays commented — uncommented it would name a directory no machine has, and
`config validate` checks declared repo paths, so `config init` would write a config that fails.

All models inherit `StrictModel` (`extra='forbid'`), so an unknown key — a typo like
`step_function:` — is a loud error rather than a silently dropped field. Because forbidding
extras widens what counts as "invalid," `main.py` wraps its import-time `load_config()` in
try/except: a present-but-invalid config falls back to `cfg = None` (so the always-present
`config` commands stay reachable to diagnose and fix it) and keeps the exception in
`CONFIG_ERROR`. Only a *missing* config yields `None` from `load_config()` directly; an invalid
one raises, and `CONFIG_LOAD_ERRORS` is the tuple every caller catches.

`report_config_error` is the single renderer for that failure, and every site that notices the
missing pipelines calls it: `list`, `search`, `config show`, `config validate`, bare `dectl`, and
`DectlGroup.get_command` when the name typed was a pipeline. It prints the file and which of the
two failures it hit, then one entry per problem carrying the location, the message, and the
rejected value — `describe_config_error` keeps that value because it is what identifies the
offending key when the location alone is ambiguous. Every line goes to stderr, and the detail
prints with markup disabled because a rejected list renders as `['a']`, which rich would eat as
a style tag.

`config edit` resolves the editor via `$VISUAL` → `$EDITOR` (no hardcoded fallback — the env var
carries the user's intent, including any `--wait`), `shlex.split`s it so args survive, resolves the
binary with `shutil.which` (full path, B607-clean), and runs it in the foreground. It seeds from the
template first if no config exists.

### Environments

Resource names carry an `{env}` placeholder (e.g. `salesdata-{env}-ds-thing`). `env.py` holds the
env resolved for the invocation and substitutes the token — one config drives every environment
by swapping a single token, rather than deriving the env out of inconsistent names. The active
env is resolved once in `main.py`'s root callback (`--env` option with `envvar='DECTL_ENV'`,
default from `defaults.environment`), giving the priority chain **`--env` > `DECTL_ENV` > config
`environment` > `dev`**. Substitution happens at **runtime, at the `resolve_*` chokepoints** (via
`render_env_model`, or `substitute_env` for bare strings) and in the display loops — never at
import, because the flag is not known until the command runs. This assumes one AWS account across
environments (names change, the session does not).

The active env is surfaced so you always know which one you are pointed at: bare `dectl` prints an
`environment: <name> (from <source>)` banner above the help, and `dectl env` prints it on demand.
The source label is exact — `main.py` reads Click's `ctx.get_parameter_source('env')` to tell flag
from `DECTL_ENV` from config/default. (This is why the top-level app drops `no_args_is_help`: that
flag would short-circuit to help before the callback could print the banner, so the callback prints
help itself.) `--help` still shows Click's static `[default: dev]` regardless of the resolved env,
which is why the banner and `dectl env` exist.

Because substitution is a literal `{env}` replacement, a config that hardcodes its environment
(`salesdata-dev-ds-thing`) ignores `--env`/`DECTL_ENV` entirely and acts on the wrong environment
while succeeding — an invisible failure. `warn_if_environment_had_no_effect` closes that hole: when
the env was set *explicitly* (source `--env` or `DECTL_ENV`, never a config default) and the
resource carries no `{env}` token at all, it warns once per invocation. Every path that resolves
names calls it — `render_env_model`, `s3`'s bare-string `resolved_bucket`/`export`, `monitor`, and
both `pipeline_view` renderers. The warning goes to **stderr** (`output.warn`), so `--json` output
and an eval'd `s3 export` stay clean.

## Cross-cutting modules

| Module | Responsibility |
| --- | --- |
| `session.py` | Builds the boto3 `Session` from config (region + optional profile). Every command that touches AWS goes through `make_session`. |
| `invoke.py` | The Lambda Invoke domain: how long to wait for one, the client to send it through, and which failures may safely be sent again. Kept out of `session.py` for the reason `durable.py` is kept out of `logs.py` — it is the Lambda API, not the boto3 session. |
| `output.py` | The two `rich` consoles and the `error`/`success`/`info`/`warn` helpers, plus `emit_json` (bare-print JSON for `--json`) and `format_duration`. Use these, not bare `print`, for anything human-facing. `error` and `warn` write to `stderr_console`; `success` and `info` write to stdout. |
| `pipeline_view.py` | Shared pipeline rendering — `render_pipeline` (human) and `pipeline_to_dict` (the stable `--json` shape). Used by both `main.py` (`list`) and `config_cmd.py` (`show`); lives outside both to avoid the `main` ↔ `config_cmd` import cycle. |
| `payloads.py` | `read_payload` — resolves a `--payload-file` path or `-` (stdin) to a JSON string for `lambda`/`sfn` `run`. |
| `logs.py` | CloudWatch log tailing (Glue, Lambda, and the multi-group `monitor` stream) plus Step Functions execution-history rendering, including structured-JSON pretty-printing. `LogGroupCursor` is the shared primitive all three tailers poll through. |
| `durable.py` | The Lambda durable-functions domain: qualifier resolution, execution lookup by name/ARN, and execution-history rendering. Kept out of `logs.py` because it is the Lambda API, not CloudWatch. |
| `iceberg.py` | The Iceberg table domain: locating the metadata file through Glue, parsing it, resolving a snapshot by id/tail/ref, walking lineage, and the diff. Talks to Glue and S3 only. |

## Gotchas

- **Glue `UpdateJob` replaces the whole job definition** — it does not patch. `build_job_update`
  reconstructs the update from the existing definition and overrides only what dectl manages, so
  fields set outside dectl survive a deploy. See the comments there before touching it.
- **`glue deploy` is two writes with different owners** — the script upload is always yours, but
  the job *definition* (role, connections, capacity, arguments) is Terraform's once a pipeline is
  established. dectl can still write it, because that is the whole point before Terraform exists:
  set arguments and deploy from the shell instead of commit → Jenkins → console. So `deploy` diffs
  its computed update against the live definition, skips `UpdateJob` entirely when nothing differs
  (the steady state — a pure code push, no drift surface), and otherwise renders the field-level
  diff and confirms. `--plan` shows it and exits without uploading; `--yes` skips the prompt for
  the pre-Terraform loop. `job_definition_changes` also reports keys dectl *drops*, since a
  detached connection is invisible in a diff that only walks the new definition.
- **`connections` in config is authoritative, not additive** — it used to union with whatever the
  job already had, which meant a stale entry could never be removed and a connection renamed in
  Terraform got silently reattached under its old name on every deploy. `None` (key absent) means
  dectl does not manage connections; `[]` detaches all.
- **Lambda `$LATEST` vs. published alias** — `deploy` without `--publish` only moves `$LATEST`;
  alias-following triggers keep running the old published version until you `--publish` (which
  moves the configured `live_alias`). `run` always targets `$LATEST`.
- **`Invoke` is the one call dectl makes that AWS cannot take back, so `invoke.py` owns it and
  `make_session` is never what sends it** — the module's docstring and constants carry the whole
  mechanism, including which failures may be sent again and why the two attempt-count keys are not
  interchangeable. Two consequences are worth knowing before reading it: a botocore default reached
  a 205-second function and ran it five times over, and the function's own `MaximumRetryAttempts`
  cannot reach that, because those are the client's HTTP retries and Lambda never sees them.
- **A wait is chosen per invocation, not per function** — a synchronous run waits on the function, a
  durable one on the whole execution, and an `--async` one only on Lambda taking the event.
  `invoke_read_timeout` is where the three are told apart, and the third is why the wait is resolved
  after `--async` is read rather than before.
- **`s3 export` / `s3 <alias> uri` must stay eval-safe** — a CLI cannot mutate its parent shell,
  so `export` prints `export name='s3://…'` lines for `eval "$(...)"` and `uri` prints a bare
  `s3://…`. Both use bare `print()`, not the rich console, so no markup or ANSI escapes leak into
  command substitution.
- **`s3 <alias> mount` is Linux-only** — it shells out to `mount-s3` (Mountpoint for Amazon S3),
  which is FUSE-based and unavailable on macOS. The command detects a non-Linux OS and refuses
  with a pointer to `export`. Binaries are resolved with `shutil.which` (full path) so bandit's
  partial-path check (B607) stays clean without a `nosec`.
- **Step Functions has two log sources** — `sfn <alias> logs` uses the `GetExecutionHistory` API
  (typed state transitions, no setup, Standard workflows only). `monitor` instead uses the state
  machine's CloudWatch **log group**, because it needs one uniform source it can merge with the
  Lambda groups — which is why a monitored state machine must have `log_group` set (Express
  workflows only log to CloudWatch and have no history API at all).
- **Log tailing follows by time, not by stream** — every tailer polls whole log groups through
  `LogGroupCursor` (moving `startTime`, boundary dedup by `eventId`), never a named stream.
  Lambda writes each execution environment to a new stream; Glue creates its *error* stream only
  when something first writes to stderr. `tail_glue_run` isolates a run with
  `logStreamNamePrefix=<run id>` on the two shared Glue groups. It used to wait for the streams
  to exist via `describe_log_streams` — sequentially, output then error — which cost up to two
  minutes of silence before the first line whenever a job never wrote to stderr, and pinned the
  stream list so a traceback landing in a later-created error stream was never shown at all.
- **`glue run --follow` stops on its own and exits non-zero on failure** — `GlueRunWatcher.finished`
  is handed to the tailer as a predicate (it only holds a logs client and cannot ask Glue
  anything), and the watcher keeps the last state so the caller can set the exit code without a
  second `get_job_run`. A few drain passes run past the terminal state, because CloudWatch
  ingestion lags the run's end and the tail would otherwise cut off mid-traceback.
- **A durable Lambda's unit of work is the execution, not the invocation** — so `durable: true`
  *swaps* `run`/`logs` for the execution-scoped set (`executions`, `history`, `logs EXECUTION`)
  rather than adding to them; see `add_durable_verbs`. `history` and `logs` read *different*
  sources: `history` is the checkpoint log via `GetDurableExecutionHistory`, while `logs` is
  CloudWatch filtered on the execution ARN that the SDK logger stamps onto every record. Note the
  two Error shapes — `GetDurableExecution` returns `Error` unwrapped, history events wrap it in a
  `Payload`/`Truncated` envelope.
- **The handle is the name's tail, never a row number** — a UUID-keyed resource needs a short
  handle of its own, and AWS assigns none, so `resolve_execution` accepts any
  unique suffix of `DurableExecutionName`, tried after both exact-name paths and erroring with the
  candidates when several match. A row number was built first and reverted: a position is only
  valid for the exact query that produced it, so `--status`, `--qualifier` and `--all-versions`
  each made the same digit name a different execution, silently. The table folds the name rather
  than truncating it, because an ellipsis mid-UUID hides the tail you would retype.
- **`render_event` takes `hide_keys` with no default, and the reason is the failure mode** — a
  suppressed field leaves a record that still reads as complete, so a wrong default is invisible.
  Glue, `monitor` and the non-durable `logs` pass `frozenset()`; only the durable `logs` suppresses,
  and only `DURABLE_OPERATION_ID_KEYS`. `requestId` is deliberately not in it — one execution spans
  many invocations, so it varies across a scoped tail. `tail_lambda_logs` adds `EXECUTION_ARN_KEY`
  itself when its filter pattern says the tail is scoped, because that is the fact that decides it
  and a caller recomputing it can get it wrong invisibly. Whether folding was wanted at all is
  `fold_scope_fields`, a parameter rather than an empty `hide_keys`, because emptiness already
  means "this caller has nothing to suppress" for glue and the non-durable `logs`.
- **`operation_tag` returns the keys it *rendered*, never one it merely read** — the claim is
  consulted before `hide_keys`, so a key claimed without being shown is one no flag can bring
  back. That was live for `attempt: 1`: folded out of the tag deliberately, claimed anyway, and
  unreachable even with `--context`. Only a retry is folded.
- **`SUFFIX_SEARCH_LIMIT` is both the tail search window and the cap on `executions --limit`** —
  one number on purpose. A tail is typed off a listing, so a row the table can print and the
  resolver cannot reach is an ambiguity that resolves silently to whichever candidate fell inside.
- **Invoking and listing need *different* qualifiers, which is why there are two functions** —
  `invoke_qualifier` returns the alias (Lambda rejects an unqualified invoke of a durable
  function, and an alias is the right thing to send since it resolves to whatever is live).
  `listing_qualifier` must return a *version*: Lambda resolves the alias to a version number when
  the execution starts, so the alias never appears in the execution ARN and
  `ListDurableExecutionsByFunction` refuses it with "cannot filter durable executions by alias".
  The API reference claims `Qualifier` takes "the function version or alias" — for that operation
  it is wrong. Collapsing these back into one function reintroduces the bug.
- **Resolving the alias scopes the listing to one deploy** — `deploy --publish` moves the alias,
  and earlier runs stay under the old version. `sweep_executions` is the escape hatch (one list
  call per version, merged newest-first); `executions --all-versions` uses it, and
  `resolve_execution` falls back to it automatically so a name from the console resolves whatever
  version ran it. It is bounded by `VERSION_SWEEP_DEPTH` and returns the versions it scanned, so
  callers report the span rather than presenting a bounded search as exhaustive.

- **`cfg is None` covers two states that need opposite answers** — no config file, and a config
  file that failed to load. The first is `config init`, the second is `config edit`, and telling
  a reader to init a config that already exists sends them to a command that refuses. Nothing
  reads `cfg` directly to refuse: `require_config` is the guard, it consults `CONFIG_ERROR`
  first, and it returns the config so the caller has no reason to reach for the global.
- **A config that does not load takes the whole pipeline tree with it** — every pipeline is a
  subcommand assembled from config, so Click's answer to `dectl my-pipeline ...` is "No such
  command", which sends the reader after a typo in something they have run for months.
  `DectlGroup.get_command` answers with the config failure instead, and only while `cfg is None`.
  With a config loaded, an unknown name really is one and the usage error is correct.
- **stdout is data and stderr is everything else, and `error()` is where that is enforced** —
  every refusal in the tool goes through it, and every read verb takes `--json`, so a message
  printed to stdout reaches a caller as a parse error instead. `success` and `info` stay on
  stdout because they are the answer. A new refusal path calls `error`, never `console.print`.
- **A gzipped metadata file is named `<name>.gz.metadata.json`** — the codec extension goes
  *before* `.metadata.json`, which is what Java's `TableMetadataParser` builds and the only form
  pyiceberg decompresses. `.metadata.json.gz` is the legacy spelling Java still reads and no
  current writer produces. `GZIP_SUFFIXES` carries both, because the catalog decides which one it
  points at. Recognising only the trailing form reports a healthy table as corrupt and sends the
  reader after a metadata file that is fine.
- **Every Iceberg summary counter is a string** — the spec says so, so `total-records` arrives as
  `'2000'`. Adding two of them concatenates and subtracting two raises, and neither failure shows
  up until a real table. Every read goes through `summary_int`, and the test fixtures write
  strings for the same reason: an integer fixture would let a reader that never coerces pass.
- **Glue holds a pointer, not the state** — `table_type` and `metadata_location` on the catalog
  table are the whole of what it knows about an Iceberg table. A table missing either is not an
  Iceberg table, and saying that beats a `KeyError` on a parameter name, because pointing dectl
  at a plain Glue table is the first thing anyone does.
- **The metadata file is the floor** — snapshots, the snapshot log, refs and schemas are all in
  it. Everything below it (the manifest list, and the manifests naming data files) is Avro, which
  is why `files` reports per-snapshot summary counters rather than a row per data file. Reading
  Avro would mean pyiceberg, which cannot open an `s3://` table without pyarrow or s3fs — either
  one more than doubles the install, and pyiceberg's own import would become the most expensive
  in the tool, paid on every invocation including the ones that never touch a table.
- **The lineage and the log answer different questions** — `snapshot-log` records every change of
  the current pointer, while parent pointers record descent. A rollback appends a log entry naming
  an older snapshot and changes nobody's parent, so the skipped commits stay in the log and leave
  the lineage. `history` shows the log with an `ancestor` column, and that column is the only
  place a rollback is visible.
- **A `diff` that could not reach its base reports no commits at all** — an ancestry walk that
  runs out has collected the target's whole lineage, and presenting that as "what ran between
  them" would be a confident answer to a question with none. `linear` is false and the path is
  empty.
- **`compact`, `expire`, `orphans` and `rollback` are deliberately not here.** They are writes,
  and the mechanism is a real fork. pyiceberg reaches `expire` and `rollback` through
  `maintenance` and `manage_snapshots`, and as of 0.11.1 has no compaction or orphan removal at
  all; Athena reaches all four through `OPTIMIZE ... REWRITE DATA USING BIN_PACK` and `VACUUM`,
  and needs a workgroup, an output location and a per-query cost. Neither covers all four for
  free, so it is a dependency-versus-config decision rather than a detail. Do not add a stub for
  one — a verb in the help that is not built is worse than an absent one.

## Tests

`pytest` (`uv run pytest`). Unit tests mock boto3; command tests drive the real Typer apps via
`typer.testing.CliRunner` against a factory-built app. Live AWS integration tests are marked
`integration` and skipped unless `--run-integration` is passed — they create and delete real
resources.

**A fake enforces the service's constraints rather than replaying responses.** The constraints
this repo's fakes encode: `FakeCloudWatchLogs` applies `startTime`, `endTime`,
`logStreamNamePrefix` and `filterPattern` for real and *raises* on a `filter_log_events` with no
`startTime`; `test_durable.py`'s Lambda fake raises on a `Qualifier` that is an alias.
`test_iceberg.py`'s Glue fake raises `EntityNotFoundException` for an unknown table and can
serve a table carrying neither Iceberg parameter, and its S3 fake hands back a stream rather
than bytes and gzips the names *Iceberg* gzips. `test_lambda.py`'s session hands out a client
bound to the botocore `Config` it was built with, and that client raises on an `Invoke` whose
config still permits a retry. When you learn a new constraint from the real API, encode it in
the fake.

**botocore's retry loop sits below the client object, so no fake can reach it.** A duplicate
invoke is issued without the code under test being called twice, which makes every fake-level
assertion here a statement about the config rather than about the behaviour. `test_invoke.py`
closes that gap against a socket that accepts connections and answers none, which is what a
still-running function looks like to botocore: it counts the requests that actually arrive. Every
other assertion in the repo rests on `total_max_attempts: 1` meaning exactly one request, and
those two tests are the only place that is established — in requests, which no client object can
show. Neither needs AWS, and both run in about a second.

**`FakeS3.ICEBERG_GZIP_SUFFIXES` is read off the writers** — Java's `TableMetadataParser` and
pyiceberg's `serializers.py` — rather than off `iceberg.GZIP_SUFFIXES`. That is what makes it
cover a spelling the reader can miss. A fake restating the implementation's rule is that rule
written twice, and it leaves something worse than a permissive fake: a green test named for the
property, exercising it, and passing on the one input the code happens to get right. The tell is
greppable, the same expression on both sides of the boundary.

*Proposed, not settled.* That this generalises to every fake here is an open standards proposal
and Chris has not ruled on it. It describes the code above, so follow it in this file's
fakes. Do not carry it elsewhere as approved policy, and do not cite it as a rule.

**Some failures are invisible to any fake.** The two Glue tailing bugs both produced *correct*
output, just minutes late: one waited on a log stream that a fake creates instantly, the other
scanned a shared group from the start of its retention, which is fast when the fake holds three
events. Latency against real data volume is not simulable, so `test_glue_run_integration.py`
runs a real Python Shell job and asserts against a wall-clock budget. That module is the only
one that costs money (a 1/16-DPU run, a fraction of a cent); the deploy integration test
deliberately never starts a run.
