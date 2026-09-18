# dectl

Data engineering control CLI for driving AWS pipelines (Glue, Lambda, Step Functions, S3,
Jenkins deploys) from a single config file. Installed via `uv tool install`; the entry point is
`dectl.main:app`.

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
**per-alias sub-app** (`make_<resource>_<thing>_app`) whose commands are the verbs. The alias is
a tree node, not an argument. Each verb command **closes over that alias's config** and resolves
`{env}` at call time (`render_env_model` / `substitute_env`), never at import, since `--env` is
not known until the command runs. An unknown alias is therefore a Typer "no such command" usage
error, not a `resolve_*` failure. Each alias sub-app's `help=` doubles as an info panel (its
resolved name, bucket, scripts). `glue.py` and `lambda_.py` are the reference implementations.

Aliases matter: users reference the short config keys (`raw`, `source-copy`), never the real AWS
names. `dectl PIPELINE list` prints the alias → AWS-name mapping.

- `iceberg` is the one resource whose verbs are all reads. Its domain lives in
  `src/dectl/iceberg.py`, the same split as `durable.py` beside `lambda_.py`.
- `s3` is the one resource with a set-level verb: `export` spans every bucket and is a command on
  the `s3` app itself, beside the per-bucket alias sub-apps.
- `lambda`/`sfn` `run` take payloads through `payloads.read_payload`: from `--payload-file F` or
  from stdin as `-`, never from an inline string argument.
- `monitor` is a **pipeline-level** command registered on `pipeline_app`, because it spans
  several resources. It tails every resource its `monitor` config block selects as one
  time-ordered stream via `logs.tail_log_groups`.

## Config

`src/dectl/config.py` holds the Pydantic models plus `load_config()` / `init_config()`. Config
lives at `$XDG_CONFIG_HOME/dectl/config.yaml`, resolved by `pyclisteno.paths.config_home()`, so
the variable is honored rather than `~/.config` being hardcoded. `buckets`, `glue_jobs` and
`lambdas` are all `alias -> real-name` mappings. A lambda's `live_alias` (not `alias`, which would
collide with the CLI's "alias" = the config key) is the AWS Lambda alias that `deploy --publish`
repoints. Its `durable` flag selects the durable verb set and is the invocation qualifier's
source. `step_functions` maps alias → `{name, log_group}`; the log group is needed only for
`monitor`. `iceberg_tables` maps alias → `{database, table}`, both carrying `{env}`. `monitor` is
its own block listing which lambdas and state machines to tail.

**`TEMPLATE_CONFIG` is the single source for the example config.** `config init` writes it and
`config example` prints it (highlighted on a TTY, plain when piped). It exercises every option, and
a test asserts it round-trips through the models, so update it with any model change. Its
`resolve_paths_from` line stays commented: uncommented, it names a directory no machine has, and
`config init` would write a config that fails `config validate`.

**All models inherit `StrictModel` (`extra='forbid'`)**, so a typo like `step_function:` is a loud
error. `main.py` wraps its import-time `load_config()` so an invalid config falls back to
`cfg = None`, keeping the `config` commands reachable, and stores the exception in
`CONFIG_ERROR`. Only a *missing* config yields `None` directly; `CONFIG_LOAD_ERRORS` is the tuple
every caller catches. `report_config_error` is the single renderer for that failure. It writes to
stderr, keeps the rejected value because it identifies an ambiguous location, and disables markup
because a rejected list renders as `['a']`, which rich would eat as a style tag.

`config edit` resolves the editor via `$VISUAL` → `$EDITOR` with no hardcoded fallback,
`shlex.split`s it, resolves the binary with `shutil.which`, and seeds the template first if no
config exists.

### Paths

`src/dectl/values.py` answers whether a value the config names outside the program can be what it
has to be; `config.py` walks the models and builds the records. Read `values.py`'s module
docstring before changing either. What a new field or resource has to meet:

- **A resource declares its path, key and bucket fields on the model**: `PATH_FIELDS` (field →
  `PathKind`), `KEY_FIELDS` (the parts of a glue S3 key) and `BUCKET_FIELDS`. Every checker reads
  them. `test_every_string_field_is_classified` makes a new string field a decision: it is in one
  of the three, or in `UNCHECKED_FIELDS` with the reason.
- **Every enumeration of a declaration reads `declaring_members`**, the one walk over the pipeline
  and each resource. A consumer that traverses for itself reaches a narrower set, and a value
  nothing enumerates reports no fault. `aws_names_only` is the deliberate exception.
- **Adding a path field to an existing resource is the whole edit.** Checking, resolution,
  `--json` and the printed row all come from the declaration. **A new resource *kind* costs six
  edits**: the `--json` section, its `paths` key, the human section, its `print_paths` call,
  `resource_types`, and the pipeline loop in `main.py`. `s3` has neither a `print_paths` call nor
  a `paths` key today. `test_every_resolved_path_reaches_both_renderers` catches a kind with
  declared paths and no display.
- **A new `ConfigFault` needs a `FAULT_WORDING` entry, a `SPELLING_FAULTS` decision, and a line in
  `recovery_lines`.** `test_every_fault_has_a_sentence` pins the first.
- **`join_uri` joins three strings and all three are guarded.** `key_fault` covers the prefix and
  the script, and `bucket_fault` covers the bucket. A guard over some operands of a composed value
  and not the rest is the defect this surface keeps producing.
- **An `s3://` URI is constructed in `values.py` and nowhere else.** `rg -n "f's3://" src/`
  returns exactly `bucket_uri` and `s3_uri`. A third row is a second rendering of one location,
  and it drifts from the object the deploy wrote. `iceberg.py`'s `parse_s3_uri` only reads one.
- **Every value naming a file or directory goes through `resolve_from_root(pipeline, value)`,
  never `Path(value)`**, or it silently depends on where dectl was invoked.
- **A fault about how a value is written, or what this machine holds, belongs to `config validate`
  and the deploy, never a field validator.** `key_fault`'s docstring says why.
- **A deploy door scopes `pipeline_value_faults` by argument, never by filtering its result.**
  Filtering drops the row naming the absent root.
- **Nothing in the path domain or the walk exits the process**, or `--json` writes zero bytes.
  `render_unusable_values` returns the lines; `refuse_unusable_values` is the one exit.

**Each `join_uri` operand is substituted exactly once, and which caller does it varies.**
`rg -n 'join_key\(|join_uri\(' src/` names the composers. `script_uri` and the upload are handed a
rendered job. `configured_uris` is handed a raw one, because the help panel is built before
`--env` is parsed. `script_uris` renders its own operands, because a renderer must not reach
`render_env_model`. Mixing conventions names an object nothing wrote, but only when the
environment's own name carries the token.
`test_every_site_composing_a_script_destination_substitutes_exactly_once` drives all four.

`deploy` runs everything that can refuse before it writes anything: `resolve_scripts`, then
`plan_glue_job_update`, then the upload, then `apply_glue_job_update`. A refusal after the upload
leaves `ScriptLocation` pointing at code the user was just told not to deploy. A config naming
both sizing models, or half the worker pair, is refused by `GlueJobConfig` itself. That is a fact
about the config alone, so it is a field validator rather than one of `values.py`'s faults.

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

## Module boundaries

Each module's docstring says what it holds. Three splits are deliberate: `invoke.py` is the Lambda
Invoke API, kept out of the boto3 session in `session.py`; `durable.py` is the Lambda API, kept
out of the CloudWatch code in `logs.py`; and `values.py` knows nothing of pipelines.
`pipeline_view.py` sits outside `main.py` and `config_cmd.py` to avoid an import cycle.

## Gotchas

**Glue**

- **`UpdateJob` replaces the whole job definition**; it does not patch. `build_job_update`
  rebuilds the update from the existing definition and overrides only what dectl manages, so
  fields set outside dectl survive a deploy. Read its comments before touching it.
- **`build_job_update` must not write into the definition it was handed.** That dict is the
  before-image `job_definition_changes` diffs against, and the copy is shallow. Writing through a
  nested dict such as `Command` makes the diff compare it with itself and skip `UpdateJob`.
- **`JobUpdate.derived` tells the diff that a key the API forced out is not a change.** Its
  docstring is the one copy of the mechanism. The `sizing_named` half of
  `test_a_worker_sized_job_converges_after_one_deploy` catches an inverted read; its
  `sizing_unmanaged` twin passes either way.
- **`glue deploy` is two writes with different owners.** The script upload is always yours. The
  job *definition* is Terraform's once a pipeline is established, but dectl can still write it
  before then. So `deploy` diffs its computed update against the live definition, skips
  `UpdateJob` when nothing differs, and otherwise renders the field-level diff and confirms.
  `--plan` shows it and exits without uploading; `--yes` skips the prompt. The diff also reports
  keys dectl *drops*.
- **`DEFINITION_FIELDS` and `NESTED_DEFINITION_FIELDS` are the declared surface, not everything a
  deploy writes.** `rg -n "job_update\['" src/dectl/commands/glue.py` returns the four written by
  hand (`Role`, `Command`, `Connections`, `DefaultArguments`). An omitted declared key means
  unmanaged, and the live value survives.
- **`connections` is authoritative, not additive.** A union makes a stale entry immortal and
  reattaches a connection renamed in Terraform. `None` (key absent) means unmanaged; `[]` detaches all.
- **`glue run --follow` stops on its own and exits non-zero on failure.** `GlueRunWatcher.finished`
  is the tailer's predicate, and a few drain passes run past the terminal state because CloudWatch
  ingestion lags the run's end.

**Lambda**

- **`$LATEST` vs. published alias.** `deploy` without `--publish` only moves `$LATEST`, so
  alias-following triggers keep running the old version until `--publish` moves `live_alias`.
  `run` always targets `$LATEST`.
- **`Invoke` is the one call AWS cannot take back, so `invoke.py` owns it and `make_session` never
  sends it.** botocore's default retries re-send a long invoke, and the function's own
  `MaximumRetryAttempts` cannot stop them, because Lambda never sees client HTTP retries. The
  module docstring carries the rest.
- **A wait is chosen per invocation, not per function.** A synchronous run waits on the function,
  a durable one on the whole execution, and `--async` only on Lambda taking the event.
  `invoke_read_timeout` tells them apart, which is why it resolves after `--async` is read.
- **A durable Lambda's unit of work is the execution**, so `durable: true` *swaps* `run`/`logs`
  for `executions`, `history` and `logs EXECUTION` (`add_durable_verbs`). `history` reads the
  checkpoint log via `GetDurableExecutionHistory`; `logs` reads CloudWatch filtered on the
  execution ARN. `GetDurableExecution` returns `Error` unwrapped, while history events wrap it in
  a `Payload`/`Truncated` envelope.
- **Invoking and listing need different qualifiers.** `invoke_qualifier` returns the alias, since
  Lambda rejects an unqualified invoke of a durable function. `listing_qualifier` must return a
  *version*: `ListDurableExecutionsByFunction` refuses an alias ("cannot filter durable executions
  by alias"), whatever the API reference says. Merging the two functions reintroduces the bug.
- **Resolving the alias scopes the listing to one deploy.** `sweep_executions` lists every version
  up to `VERSION_SWEEP_DEPTH` and returns the versions it scanned, so callers report the span.
  `executions --all-versions` uses it, and `resolve_execution` falls back to it.
- **An execution's handle is a unique suffix of its name, never a row number.** A row number
  names a different execution under every `--status`, `--qualifier` or `--all-versions`. The
  table folds the name rather than truncating it, since the tail is what gets retyped.
  `SUFFIX_SEARCH_LIMIT` is both the search window and the cap on `executions --limit`, so any
  printed row is resolvable.
- **`render_event` takes `hide_keys` with no default**, because a suppressed field leaves a record
  that still reads as complete. Only the durable `logs` suppresses. `operation_tag` returns only
  the keys it *rendered*, so no claimed key becomes unreachable.

**S3 and Step Functions**

- **`s3 export` and `s3 <alias> uri` must stay eval-safe.** `export` prints `export name='s3://…'`
  lines for `eval "$(...)"` and `uri` prints a bare `s3://…`. Both use bare `print()`, so no
  markup or ANSI escapes leak into command substitution.
- **`s3 <alias> mount` is Linux-only.** It shells out to `mount-s3`, which is FUSE-based, and
  refuses elsewhere with a pointer to `export`.
- **Step Functions has two log sources.** `sfn <alias> logs` uses `GetExecutionHistory` (typed
  transitions, Standard workflows only). `monitor` uses the state machine's CloudWatch log group so
  it can merge with the Lambda groups, which is why a monitored state machine needs `log_group`.
  Express workflows have no history API at all.
- **Log tailing follows by time, not by stream.** Every tailer polls whole log groups through
  `LogGroupCursor`. Lambda writes each execution environment to a new stream, and Glue creates its
  error stream only on first stderr write. `tail_glue_run` isolates a run with
  `logStreamNamePrefix=<run id>`. Waiting for streams via `describe_log_streams` costs minutes of
  silence and misses a later-created error stream.

**Config and output**

- **`cfg is None` covers two states needing opposite answers**: no config (`config init`) and a
  config that failed to load (`config edit`). `require_config` is the guard; it consults
  `CONFIG_ERROR` first and returns the config.
- **A config that does not load takes the whole pipeline tree with it.** `DectlGroup.get_command`
  answers a pipeline name with the config failure instead of "No such command", only while
  `cfg is None`.
- **stdout is data and stderr is everything else.** Every refusal calls `output.error`, never
  `console.print`. `success` and `info` stay on stdout because they are the answer.

**Iceberg**

- **A gzipped metadata file is named `<name>.gz.metadata.json`.** Java's `TableMetadataParser`
  writes that form, and it is the only one pyiceberg decompresses. `.metadata.json.gz` is the
  legacy spelling Java still reads. `GZIP_SUFFIXES` carries both, because the catalog decides which
  one it points at.
- **Every Iceberg summary counter is a string**, so `total-records` arrives as `'2000'`. Every read
  goes through `summary_int`, and the fixtures write strings so a non-coercing reader fails.
- **Glue holds a pointer, not the state.** `table_type` and `metadata_location` are all it knows. A
  table missing either is not an Iceberg table, and dectl says so rather than raising a `KeyError`.
- **The metadata file is the floor.** Everything below it is Avro, so `files` reports per-snapshot
  summary counters. Reading Avro would mean pyiceberg plus pyarrow or s3fs, more than doubling the
  install and slowing every invocation.
- **The lineage and the log answer different questions.** A rollback appends a `snapshot-log`
  entry and changes no parent pointer. `history`'s `ancestor` column is the only place it shows.
- **A `diff` that could not reach its base reports no commits at all**: `linear` is false and the
  path is empty.
- **`compact`, `expire`, `orphans` and `rollback` are deliberately not here.** They are writes,
  and the mechanism is a real fork. pyiceberg reaches `expire` and `rollback` through
  `maintenance` and `manage_snapshots`, and as of 0.11.1 has no compaction or orphan removal at
  all; Athena reaches all four through `OPTIMIZE ... REWRITE DATA USING BIN_PACK` and `VACUUM`,
  and needs a workgroup, an output location and a per-query cost. Neither covers all four for
  free, so it is a dependency-versus-config decision rather than a detail. Do not add a stub for
  one — a verb in the help that is not built is worse than an absent one.

## Tests

`uv run pytest`. Unit tests mock boto3, and command tests drive the real Typer apps through
`typer.testing.CliRunner`. Live AWS tests are marked `integration` and run only with
`--run-integration`, because they create and delete real resources.

**Anything that writes to AWS gets a live test.** A failure here is read once, from output, with
no debugger and no second attempt, and a fake holds only the service rules somebody already knew.
The exemptions are real cost and setup too extensive to maintain, never "the fake covers it".

**Every resource under test is created and deleted by the test**, never a real pipeline's job. A
role is the exception, as a precondition like credentials. `DECTL_IT_ROLE_ARN` names one, and the
fixture in `conftest.py` creates its own when it is unset:

```bash
# Least privilege: glue:*, logs:*, s3:*, and iam:PassRole on that one role
DECTL_IT_ROLE_ARN=<arn> DECTL_IT_REGION=<region> uv run pytest --run-integration

# No setup, broader credentials: the fixture creates and deletes a role, needing iam:CreateRole
DECTL_IT_AWS_PROFILE=<profile> DECTL_IT_REGION=<region> uv run pytest --run-integration
```

Prefer the first. `iam:CreateRole` is an escalation path, and a fresh role costs about two minutes
per module because IAM is eventually consistent.

**What live Glue does that no fake would have told you.** Version 2.0 is retired and `CreateJob`
refuses it outright. A job asking for `MaxCapacity` alongside version 3.0 or 4.0 comes back
carrying `WorkerType` as well, already worker-sized. A DPU-only Spark job is still creatable by
omitting `GlueVersion` entirely, which is the only way to build a DPU-to-worker migration fixture.

**A fake enforces the service's constraints rather than replaying responses.** `FakeCloudWatchLogs`
applies `startTime`, `endTime`, `logStreamNamePrefix` and `filterPattern`, and raises on a
`filter_log_events` with no `startTime`. The Lambda fakes raise on an alias `Qualifier` and on an
`Invoke` whose client config still permits a retry. When you learn a new constraint from the real
API, encode it in the fake. `FakeS3.ICEBERG_GZIP_SUFFIXES` is read off the writers (Java's
`TableMetadataParser`, pyiceberg's `serializers.py`), not off `iceberg.GZIP_SUFFIXES`, so it
covers a spelling the reader can miss.

**botocore's retry loop sits below the client object, so no fake reaches it.** `test_invoke.py`
counts the requests arriving at a socket that accepts connections and never answers. It is the
only place `total_max_attempts: 1` is shown to mean one request, and it needs no AWS.

**Latency is invisible to any fake.** `test_glue_run_integration.py` runs a real Python Shell job
against a wall-clock budget, and it is the only module that costs money. The deploy integration
test never starts a run.
