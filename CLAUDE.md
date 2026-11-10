# dectl

Data engineering control CLI for driving AWS pipelines (Glue, Lambda, Step Functions, S3,
Jenkins deploys) from one config directory. Installed via `uv tool install`; the entry point is
`dectl.main:app`.

## The core idea: the CLI surface is assembled from config as a command asks for it

There is no static command tree. `src/dectl/main.py` reads `config.yaml` alone **at module
import**, because the root options' defaults come from it. The root `DectlGroup` holds the global
commands. When a command names a pipeline, `DectlGroup.get_command` reads `config.yaml` and
`$XDG_CONFIG_HOME/dectl/pipelines/<name>.yaml` alone, and attaches a `PipelineGroup` if the
pipeline configures a resource. Root help and TAB completion list every pipeline, so
`list_commands` reads every file and attaches a group for each. A `PipelineGroup` carries only its
name, its help line and its panel until a command under it is asked for. Then `make_pipeline_app`
builds its tree, attaching only the resource sub-apps the pipeline defines. So a run reads and
builds the one pipeline it names, and its start does not grow with the number of pipeline files.
A pipeline with just `buckets` gets an `s3` command and nothing else; add `glue_jobs` and a `glue`
command appears. Running `dectl` with no config still works (commands like `config init` are
always present) because `load_config()` returns `None` rather than raising.

This means the command you see depends entirely on the user's config. Each resource kind is one
`RESOURCE_KINDS` entry in `src/dectl/resource_kinds.py`. It names the kind's word, its
`PipelineConfig` collection, its `make_*_app` factory, its help panel and its two `list` sections.
`make_pipeline_app`, `pipeline_summary`, `pipeline_has_commands`, `reference`, `resource_types`
and both renderers iterate it. So a new kind costs its command module, one entry, and its lines in
`main.py`'s `REFERENCE_*` tables, which are static grammar.
`test_every_pipeline_field_is_a_resource_kind_or_named_otherwise` fails on a collection with no
entry, and the reference test fails on a kind `reference` never names.

## Grammar: `PIPELINE RESOURCE ALIAS VERB` (verb last)

The command shape is `dectl PIPELINE RESOURCE ALIAS VERB [OPTIONS]` — the **verb is last** so a
deploy → run → logs loop on one resource changes only the trailing word. One rule governs the
whole surface: **aliased = one thing, unaliased = the set/pipeline.** Instance verbs take an
alias (`glue JOB run`, `s3 BUCKET mount`); set verbs take none (`s3 export`, `jenkins status`,
`list`, `monitor`). Scope is structural: the argument's presence selects it, never a flag.
Whether a command writes is structural too: a write's rehearsal is a `plan` verb beside it,
never a flag on the write, and no namespace acts bare. `glue` and `sfn` `plan` write nothing.
A resource is named for the system it drives, so the Jenkins job is `jenkins`, never a word
for what its builds do. `dectl reference` is the static-grammar command a config-assembled tree
needs, and `--json` on reads goes through `output.emit_json`, which prints bare so a pipe receives only the JSON.

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
  the `s3` app itself, beside the per-bucket alias sub-apps. `S3_SET_VERBS` in `config.py` names
  it, so a bucket alias of the same name is refused at load, as `JENKINS_READ_VERBS` refuses an
  action named `status` or `logs`. A test holds each set equal to its app's commands.
- `lambda`/`sfn` `run` take payloads through `payloads.read_payload`: from `--payload-file F` or
  from stdin as `-`, never from an inline string argument.
- `sfn` `plan` and `deploy` exist only for a machine naming a `definition_file`: config decides
  whether the verbs are there, as `durable` does for the lambda verbs. Both run one `prepare`, so
  `plan` refuses what `deploy` refuses. Rendering a definition lives in `src/dectl/asl.py`.
- `jenkins` has no aliases, since a pipeline has one job. Its writes are the keys of the job's
  `actions`, each posting `parameters` with its own on top, beside `status` and `logs`. dectl
  names no action itself: what `ACTION=dry_run` does is the Jenkinsfile's, which dectl never
  sees, so a verb dectl named would be a guess at it. Every action queues a build, whatever it
  is called, and polls the queue item the POST's `Location` names until Jenkins numbers the
  build. That number is what `--follow` streams and what `status` and `logs` take. A queued
  build is not `lastBuild` until it starts, so they read `lastBuild` only when given no number.
- `monitor` is a **pipeline-level** command registered on `pipeline_app`, because it spans
  several resources. It tails every resource its `monitor` config block selects as one
  time-ordered stream via `logs.tail_log_groups`.

## Config

`src/dectl/config.py` holds the Pydantic models plus `load_config()` / `init_config()`. Config
lives in `$XDG_CONFIG_HOME/dectl/`, resolved by `config_home()` in the same module, so the variable
is honored rather than `~/.config` being hardcoded. `config.yaml` holds `defaults`, `jenkins` and
`variables`. Each pipeline is `pipelines/<name>.yaml`, and the file stem is the pipeline's name.

**The files are joined at the loader, and nothing after it knows there are several.**
`read_config_files` joins them into the one mapping `DectlConfig` validates, so everything after
loading sees each pipeline under `pipelines.<stem>`. Its `PipelineScope` names which pipeline
files it joins: `('sales',)` joins one, `()` none, and `None` every file. A pipeline loads alone
because nothing in it reads a sibling: no validator compares two pipelines, and its variables
come from `config.yaml` and its own block. A named pipeline costs one file lookup. Only a miss
lists the directory, for the entries sharing its stem, so `sales.yml` is refused when `sales` is
typed. It refuses a `pipelines` key in
`config.yaml`. In `pipelines/` it refuses what looks like a misnamed pipeline: a `.yml`, `.yaml` in
another case, or a stem in `GLOBAL_COMMAND_NAMES`, which the root's own command would hide. It
skips everything else, since one editor backup refused would take every pipeline down with it.
`config edit NAME` runs the same `pipeline_name_fault` before it writes a file. The refusals are
`ValidationError`s located at `('pipelines',)`, so the one renderer reports them. Locations are
rendered by `variables.file_and_key`, which turns `('pipelines', name, ...)` into
`pipelines/<name>.yaml <keys>` and anything else into `config.yaml <keys>`. The fault rows and
variable origins use it too. Every path is derived from `CONFIG_PATH.parent` at the call, because
tests point `CONFIG_PATH` at a temporary directory in both `config` and `config_cmd`.
`tests/conftest.py`'s `write_config_files` writes a one-document test config the same way.

`buckets`, `glue_jobs` and
`lambdas` are all `alias -> real-name` mappings. A lambda's `live_alias` (not `alias`, which would
collide with the CLI's "alias" = the config key) is the AWS Lambda alias that `deploy --publish`
repoints. Its `durable` flag selects the durable verb set and is the invocation qualifier's
source. `step_functions` maps alias → `{name, log_group, definition_file,
definition_substitutions, live_alias}`; the log group is needed only for `monitor`, and the other
three only for `deploy`. `iceberg_tables` maps alias → `{database, table}`, both carrying
`{env}`. `monitor` is its own block listing which lambdas and state machines to tail. A
pipeline's `jenkins` names a `job_path` (`folder/job`, which `job_url_path` expands), the
`parameters` every build posts, and `actions`. An action cannot be named `status` or `logs`,
because it would share that command's name and only one of the two would survive.

**`variables` is substituted before the models see the file, and `{env}` is not.**
`variables.expand_variables` runs inside `DectlConfig`'s `wrap` validator. It replaces `{name}` in
every string of the raw mapping: the block in `config.yaml` reaches every file, a pipeline's block
that pipeline alone. So every field downstream, the path checks included, sees a substituted value
and needs no change for a new field to take variables. `{env}` passes through to the `resolve_*`
chokepoints. A pipeline variable redefining one from `config.yaml`, a variable named `env` or a name
`{name}` cannot reach, an undefined `{name}`, an unquoted `{name}` YAML read as a mapping, and a
non-string value are each a `ValidationError` carrying the key's own location. When any of those
fire, the validator runs the schema anyway, and a key it refuses is reported alone. A `variables`
block under the wrong key, or a pipeline file still wrapped in its name, defines nothing, so every
reference reads as undefined and the `{{name}}` hint sends the reader the wrong way. A read of one
pipeline holds no sibling's `variables`, so an undefined `{name}` names the blocks it searched.
Only a read of every file can say a sibling defines it, which `config validate` does. An anchor or merge
is YAML's, resolved before the pass sees the mapping, and reaches only within one file. The pass
builds new mappings rather than editing the one it was handed, because YAML gives an anchored block
to each alias and merge of it as one object. `{{name}}` is the escape for a literal `{name}` another
program fills. `${NAME}` is skipped by the token's lookbehind, which keeps the Jenkins credentials
and Terraform placeholders out of reach.
`aws_names_only` drops `variables`, because a `{env}` held there names nothing and would silence
the env-effect guard.

The pass records what the file wrote for every string it changed, and hands each pipeline its
share as `_writings`. `written_for` reads it, so a fault row names the line to edit and the
variables in it, not only a value no line of the file holds. Any new code that builds an
`UnusableValue` passes `written_for(...)` as its `written` argument.

Variables let one key's value refer to another's. A config that needs that is usually better
generated by a program than written by hand. dectl supports it anyway, because its config is
written by hand, and an account id, a company prefix and a bucket recur inside many of its names.

**`TEMPLATE_CONFIG` and `TEMPLATE_PIPELINES` are the single source for the example config.**
`template_files` maps each to its path. `config init` writes them and `config example` prints
each under a `# <path>` line (highlighted on a TTY, plain when piped). Together they hold one of
every option and every form a value takes, and name each alias for what it shows. A test asserts
they round-trip through the models, so update them with any model change. Its
`resolve_paths_from` line stays commented: uncommented, it names a directory no machine has, and
`config init` would write a config that fails `config validate`.

**All models inherit `StrictModel` (`extra='forbid'`)**, so a typo like `step_function:` is a loud
error. `loaded_config(scope)` returns the load's exception rather than raising it, so a config
that does not load keeps the `config` commands reachable. It reads each scope once per process.
`main.py` reads `()` at import for the root options' defaults, and `search` and `env --json`
need nothing more. A command naming a pipeline reads that pipeline's scope. Root help, `list`,
`reference`, `config show` and `config validate` share the read of every file. `load_config`
reads the files on every call. Only a *missing* config yields `None` directly;
`CONFIG_LOAD_ERRORS` is the tuple every caller catches. `report_config_error` is the single renderer for that failure. It writes to
stderr, keeps the rejected value because it identifies an ambiguous location, and disables markup
because a rejected list renders as `['a']`, which rich would eat as a style tag.

`config edit` resolves the editor via `$VISUAL` → `$EDITOR` with no hardcoded fallback,
`shlex.split`s it, and resolves the binary with `shutil.which`, before writing anything. Bare, it
opens `config.yaml`, seeding the template first if none exists. `config edit NAME` opens
`pipelines/NAME.yaml`, writing `PIPELINE_STUB` first if it is absent.

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
  `--json` and the printed row all come from the declaration. `resolved_paths` keys each path by
  the kind's word, then alias. `render_pipeline` prints every row's paths itself, and each
  `--json` section reads its own word's share as `paths`. `s3`'s section carries no `paths` key,
  because a bucket names no file.
- **A new `ConfigFault` needs one `FAULT_REPORTING` row**: its wording, whether it is about
  spelling, and its form line. `recovery_lines` derives from the rows, in row order.
  `test_every_fault_is_reported_in_full` fails on a missing row, or a spelling row with no form.
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

`deploy` runs everything that can refuse before it writes anything: `resolve_scripts`, then
`confirm_glue_job_update`, then the upload, then `apply_glue_job_update`. A refusal after the
upload leaves `ScriptLocation` pointing at code the user was just told not to deploy. A config naming
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
help itself.) `--help` shows the config's `environment` as the default, read when the tree is
built. A config naming `staging` shows `[default: staging]` even under `DECTL_ENV=prod`. That is
why the banner and `dectl env` exist.

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

**Each kind of external effect has one door.** Every AWS client comes from `make_session` in
`session.py`, except Lambda's `Invoke`, which `invoke.py` owns. A client-level setting (retry
mode, timeout, user agent) goes in `default_client_config` there. `make_session` sets it as the
botocore session's default, which botocore merges under each client's own `Config`, so no call
site sets one. Every program dectl starts on this
machine, `mount-s3`, `umount` and the config editor, goes through `external_command.run`. Call
sites reach it as `external_command.run` rather than importing the function, so a test patches
`dectl.external_command.run` once. Jenkins is reached through `jenkins_request` in
`commands/jenkins.py`. Its caller passes the status it expects, and a transport failure or any
other status raises one `JenkinsError`, so no call site checks a status.

**A door imports its client when it opens, never at the top of a module.** boto3, `botocore.config`,
`httpx2` and pyselfupdate are imported inside `make_session`, `make_invoke_client`,
`jenkins_request` and the update paths, and elsewhere only under `TYPE_CHECKING`. Every command
imports the modules that hold the doors, and most never open one, so a top-level import makes
`reference`, `config`, `--help` and each TAB completion pay for it. `botocore.exceptions` is the
exception: it is a few milliseconds, and the root group catches its errors.

## Gotchas

**Glue**

- **`UpdateJob` replaces the whole job definition**; it does not patch. `build_job_update`
  rebuilds the update from the existing definition and overrides only what dectl manages, so
  fields set outside dectl survive a deploy. Read its comments before touching it.
- **`build_job_update` must not write into the definition it was handed.** That dict is the
  before-image `job_definition_changes` diffs against, and the copy is shallow. Writing through a
  nested dict such as `Command` makes the diff compare it with itself and skip `UpdateJob`.
- **`JobUpdate.derived` tells the diff that a key the API forced out is not a change.** Its
  docstring is the one copy of the mechanism. `test_a_worker_sized_job_converges_after_one_deploy`
  catches an inverted read.
- **`glue deploy` is two writes with different owners.** The script upload is always yours. The
  job *definition* is Terraform's once a pipeline is established, but dectl can still write it
  before then. So `deploy` diffs its computed update against the live definition, skips
  `UpdateJob` when nothing differs, and otherwise renders the field-level diff and confirms.
  `plan` shows it through `diff_glue_job_update` and uploads nothing; `deploy --yes` skips the
  prompt. The diff also reports keys dectl *drops*.
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
- **A durable Lambda's unit of work is the execution**, so `durable: true` *swaps* the ordinary
  `run`/`logs` for durable ones and adds `executions` and `history` (`add_durable_verbs`). Its
  `run` starts an execution and prints the ARN, and `logs` takes an EXECUTION. `history` reads the
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
- **`sfn deploy` sends the rendered text, never a re-serialization of it.** The AWS provider
  stores the live definition verbatim on refresh and compares it with `templatefile`'s output
  unnormalized. Identical bytes are what make Terraform's next plan over the same file report no
  change, and anything else shows as a whitespace diff on every plan. Whether to write is decided
  on the bytes, and the parsed JSON decides only whether the diff is semantic.
- **`render_definition` substitutes a bare `${name}` and nothing else.** A directive, an
  expression or a strip marker is a `TemplateFault` rather than an approximation, because a
  near-miss renders a definition Terraform would not. `TEMPLATE_SEQUENCE` tries the escapes first
  at each position, which is how Terraform scans a run of `$`.
- **A number or boolean substitution is held as Terraform's text for it.** `template_text` runs
  as the model loads, so `2.0` renders as `2` and `true` as `true`, as `templatefile` writes them.
  Python's `str` writes `2.0` and `True`, which are different definition strings.
- **`{env}` is substituted in `definition_substitutions`, never in the file.** `substitute_env`
  replaces the literal `{env}`, so run over the template it would turn a Terraform `${env}` into
  `$dev`. The reverse holds for a suggestion: a live value names one environment, so
  `suggested_entry` writes it back as `{env}` through `generalize_env` before printing it.
- **`UpdateStateMachine` patches; it is not Glue's `UpdateJob`.** Sending `definition` alone
  leaves the role, logging and tracing as they are, so there is no `build_job_update` here. There
  is no confirm prompt either: the definition is the code being iterated on, so a prompt would fire
  on every deploy, and the diff is printed every time instead.
- **`deploy --publish` publishes the current revision, changed or not.**
  `PublishStateMachineVersion` is idempotent per revision, and the `revisionId` it is handed makes
  Step Functions refuse a revision someone else wrote since dectl read or updated it. `run` starts
  the unqualified machine, which is the latest revision, so it never needs `--publish`.
- **Log tailing follows by time, not by stream.** Every tailer polls whole log groups through
  `LogGroupCursor`. Lambda writes each execution environment to a new stream, and Glue creates its
  error stream only on first stderr write. `tail_glue_run` isolates a run with
  `logStreamNamePrefix=<run id>`. Waiting for streams via `describe_log_streams` costs minutes of
  silence and misses a later-created error stream.

**Config and output**

- **A `None` config covers two states needing opposite answers**: no config (`config init`) and
  a config that failed to load (`config edit`). `require_config(scope)` is the guard; it consults
  the load's failure first and returns the config.
- **A pipeline file that does not load takes only its own pipeline's commands with it.** A command
  naming a pipeline reads `config.yaml` and that file alone, so a broken or misnamed sibling
  leaves it working. `DectlGroup.get_command` answers the name with that read's failure instead
  of "No such command". A broken `config.yaml` takes every pipeline. Root help, `list`,
  `reference`, `config show` and `config validate` read every file, so any file that does not
  load stops them.
- **Typer's "Did you mean" reads only the attached commands**, which hold no pipeline a command
  has not named. `DectlGroup.resolve_command` refuses an unknown name itself and suggests from the
  directory listing too, so a typo reads no pipeline file.
- **stdout is data and stderr is everything else.** Every refusal calls `output.error`, never
  `console.print`. `success` and `info` stay on stdout because they are the answer.
- **An AWS or Jenkins error no command catches ends on its own sentence, never a traceback.**
  `DectlGroup.invoke` catches `ClientError`, `BotoCoreError` and `JenkinsError`, prints the
  message and exits 1.
  A command that knows what a refusal means catches it first and says that instead, as
  `locate_metadata` does for a missing table.

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
  and the mechanism is a real fork. pyiceberg reaches `rollback` through `manage_snapshots`, and
  its `expire` only drops snapshots from the metadata, deleting no file. It has no compaction or
  orphan removal. Athena compacts through `OPTIMIZE ... REWRITE DATA USING BIN_PACK`, and its
  `VACUUM` expires snapshots and removes orphans in one statement that cannot run either alone.
  It has no rollback, and it needs a workgroup, an output location and a per-query cost. No
  engine covers all four, so it is a dependency-versus-config decision rather than a detail. Do
  not add a stub for one — a verb in the help that is not built is worse than an absent one.

## Tests

`uv run pytest`. Unit tests mock boto3, and command tests drive the real Typer apps through
`typer.testing.CliRunner`. Live AWS tests are marked `integration` and run only with
`--run-integration`, because they create and delete real resources.

**`tests/layered_pipelines_config/` is a config in the shape a real multi-pipeline one takes.** It
configures every resource type. The variables in `config.yaml` fan out to two pipeline files
through each one's prefix, and `finance.yaml` is written out like `sales.yaml` under its own
pipeline and dataset. The scripts and Lambda sources it names sit beside it, so its relative paths
resolve. `test_config.py` loads it through `load_config` for the load-time faults and refusals.
`test_main.py` starts the installed `dectl` against it in a fresh process, because `main.py`
reads `config.yaml` at import for the root options' defaults.
`test_stepfunctions_integration.py` deploys its `flow.asl.json.tftpl` with `finance`'s
substitutions, so live Step Functions validates the JSONata. A config shape that has to work goes
in these files rather than in a test's inline dict. The template is named `.tftpl`, Terraform's
extension for a `templatefile()` template, so the JSON hook does not read it before it is rendered.

**Anything that writes to AWS gets a live test.** A failure here is read once, from output, with
no debugger and no second attempt, and a fake holds only the service rules somebody already knew.
The exemptions are real cost and setup too extensive to maintain, never "the fake covers it".

**Every resource under test is created and deleted by the test**, never a real pipeline's job. A
role is the exception, as a precondition like credentials. `DECTL_IT_ROLE_ARN` names the Glue one
and `DECTL_IT_STATES_ROLE_ARN` the Step Functions one, and the fixtures in `conftest.py` create
their own when either is unset:

```bash
# Least privilege: glue:*, logs:*, s3:*, states:*, and iam:PassRole on those two roles
DECTL_IT_ROLE_ARN=<arn> DECTL_IT_STATES_ROLE_ARN=<arn> DECTL_IT_REGION=<region> uv run pytest --run-integration

# No setup, broader credentials: the fixture creates and deletes a role, needing iam:CreateRole
DECTL_IT_AWS_PROFILE=<profile> DECTL_IT_REGION=<region> uv run pytest --run-integration
```

Prefer the first. `iam:CreateRole` is an escalation path, and a fresh role costs about two minutes
per module because IAM is eventually consistent.

**What live Glue does that no fake would have told you.** Version 2.0 is retired and `CreateJob`
refuses it outright. A job asking for `MaxCapacity` alongside version 3.0 or 4.0 comes back
carrying `WorkerType` as well, already worker-sized. A DPU-only Spark job is still creatable by
omitting `GlueVersion` entirely, which is the only way to build a DPU-to-worker migration fixture.

**What live Step Functions does that no fake would have told you.** A machine never updated
carries no `revisionId` at all. `DescribeStateMachine` returns the definition byte for byte as it
was sent. `DescribeStateMachineAlias` lags `UpdateStateMachineAlias` by seconds, returning the
old routing when read at once, so a test reading an alias back polls for it.
`PublishStateMachineVersion` on a machine never updated since version 1 returns version 1 rather
than publishing another.

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

**What live Lambda does that no fake would have told you.** Lambda delivers an invocation's logs
seconds after `Invoke` returns, creating the log group if it is gone. So a function whose role may
write logs recreates a group the test deleted. A test's role for a durable function needs only
`lambda:CheckpointDurableExecution` and `lambda:GetDurableExecutionState`. A durable execution's
`FunctionArn` names the version it ran.

**Latency is invisible to any fake.** `test_glue_run_integration.py` runs a real Python Shell job
against a wall-clock budget, and it is the only module that costs money. The deploy integration
test never starts a run.
