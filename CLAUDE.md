# dectl

Data engineering control CLI for driving AWS pipelines (Glue, Lambda, Step Functions, S3,
Jenkins deploys) from a single config file. Installed via `uv tool install`; the entry point is
`dectl.main:app`. Universal rules live in `~/.claude/CLAUDE.md` — this file only covers what
is specific to dectl.

## The core idea: the CLI surface is assembled from config at import time

There is no static command tree. `src/dectl/main.py` calls `load_config()` **at module import**
and then builds the Typer app dynamically: it loops over the pipelines in
`~/.config/dectl/config.yaml` and, for each one, attaches only the resource sub-apps that the
pipeline actually defines. A pipeline with just `buckets` gets an `s3` command and nothing
else; add `glue_jobs` and a `glue` command appears. Running `dectl` with no config still works
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
`monitor`). `dectl reference` prints the full static grammar independent of config; every read
command takes `--json` (via `output.emit_json`, bare-print so pipes stay clean).

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

Aliases matter: users reference the short config keys (`raw`, `source-copy`), never the real
AWS names. `dectl PIPELINE list` prints the alias → AWS-name mapping.

`s3` is the one resource with a set-level verb: per-bucket `mount`/`unmount`/`uri` are alias
sub-apps, but `export` (spans every bucket, no alias) is a command on the `s3` app itself.
`lambda`/`sfn` `run` take payloads via `payloads.read_payload` (a `--payload-file` path, or `-`
for stdin), not an inline positional.

`monitor` is the exception to the factory pattern: it is a **pipeline-level** command (like
`list`, registered on `pipeline_app` directly), not a resource sub-app, because it spans
several resources. It reads the pipeline's explicit `monitor` config block and tails every
selected resource's CloudWatch log group as one interleaved, time-ordered stream via
`logs.tail_log_groups`.

## Config

`src/dectl/config.py` — Pydantic models plus `load_config()` / `init_config()`. Config lives at
`~/.config/dectl/config.yaml`. `buckets` is an `alias -> real-bucket-name` mapping (same
alias→name shape as `glue_jobs` and `lambdas`), not a fixed set of roles. A lambda's
`live_alias` (renamed from `alias` to avoid colliding with the CLI "alias" = the config key) is
the AWS Lambda alias that `deploy --publish` repoints; its `durable` flag selects the durable
verb set and doubles as the invocation qualifier's source. `step_functions` maps
alias → `{name, log_group}` (the log group is optional, needed only for `monitor`). `monitor` is
its own block listing which lambdas / step machines to tail, kept separate so the monitored view
is defined in one scannable place. When you change a model, update `TEMPLATE_CONFIG` in the same
file so `config init` stays valid — a test asserts `TEMPLATE_CONFIG` round-trips through the models.

`TEMPLATE_CONFIG` is the single source for the example config: `config init` writes it, `config
example` prints it (syntax-highlighted on a TTY, plain when piped so `config example > config.yaml`
stays clean), and it exercises every option so it doubles as side-by-side reference while editing.

All models inherit `StrictModel` (`extra='forbid'`), so an unknown key — a typo like
`step_function:` — is a loud error surfaced by `config validate` rather than a silently dropped
field. Because forbidding extras widens what counts as "invalid," `main.py` wraps its import-time
`load_config()` in try/except: a present-but-invalid config falls back to `cfg = None` (so the
always-present `config` commands stay reachable to diagnose and fix it) and the root callback's
bare-invocation banner prints the reason. Only a *missing* config yields `None` from
`load_config()` directly; an invalid one still raises, which is what `config validate` catches to
report the exact failing path.

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
| `output.py` | The `rich` console and the `error`/`success`/`info` helpers, plus `emit_json` (bare-print JSON for `--json`). Use these, not bare `print`, for anything human-facing. |
| `pipeline_view.py` | Shared pipeline rendering — `render_pipeline` (human) and `pipeline_to_dict` (the stable `--json` shape). Used by both `main.py` (`list`) and `config_cmd.py` (`show`); lives outside both to avoid the `main` ↔ `config_cmd` import cycle. |
| `payloads.py` | `read_payload` — resolves a `--payload-file` path or `-` (stdin) to a JSON string for `lambda`/`sfn` `run`. |
| `logs.py` | CloudWatch log tailing (Glue, Lambda, and the multi-group `monitor` stream) plus Step Functions execution-history rendering, including structured-JSON pretty-printing. `LogGroupCursor` is the shared primitive all three tailers poll through. |
| `durable.py` | The Lambda durable-functions domain: qualifier resolution, execution lookup by name/ARN, and execution-history rendering. Kept out of `logs.py` because it is the Lambda API, not CloudWatch. |

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

## Tests

`pytest` (`uv run pytest`). Unit tests mock boto3; command tests drive the real Typer apps via
`typer.testing.CliRunner` against a factory-built app. Live AWS integration tests are marked
`integration` and skipped unless `--run-integration` is passed — they create and delete real
resources.
