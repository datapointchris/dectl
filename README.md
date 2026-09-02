# dectl

Data engineering control CLI for managing AWS pipelines.

## Installing

```bash
uv tool install git+https://github.com/datapointchris/dectl.git
```

Releases are cut from `main` by python-semantic-release, so the default branch is
always the latest release. Pin a specific version with `@v0.6.1` if you need to.

`dectl update` installs the latest GitHub release over the running one, and
`dectl update --check` reports whether one exists without installing it. Once a
day, a command that finds a newer release prints a one-line notice to stderr;
`NO_AUTO_UPDATE=1` (or `DECTL_NO_AUTO_UPDATE=1`) turns that off.

## Command grammar

Commands follow the shape `dectl PIPELINE RESOURCE ALIAS VERB [OPTIONS]` — **the verb comes
last**, so a deploy → run → logs loop on one resource changes only the final word. Pipelines
and their resources (`glue`, `lambda`, `sfn`, `s3`, `iceberg`, `release`) are built from your config.
Every level is self-documenting — run any partial command or add `--help` to see what's next,
including the live list of aliases. `dectl reference` prints the whole grammar independent of
your config.

**One rule: aliased vs. set.** Acting on one thing takes an alias (`glue JOB run`,
`s3 BUCKET mount`); acting on the whole set takes none (`s3 export`, `release`, `list`,
`monitor`).

```bash
dectl                              # top-level help: pipelines + global commands
dectl reference                    # the full command grammar, config-independent
dectl salesdata                    # what the salesdata pipeline manages
dectl salesdata list               # alias -> real AWS name mapping (add --json for scripts)
dectl salesdata glue               # the glue jobs; then pick one, then a verb
dectl salesdata glue source-copy   # this job's verbs (deploy/run/logs/runs) + what it maps to
```

Every read command takes `--json` for a stable, machine-readable shape on stdout (`list`,
`config show`, `search`, the per-resource `runs`, `lambda ... run`, `release status`, and every
`iceberg` verb).

## Environments

Resource names in config carry an `{env}` placeholder, so one config drives dev / staging / prod:

```yaml
lambdas:
  do-thing:
    name: salesdata-{env}-ds-do-thing-lambda
```

```bash
dectl salesdata list                 # dev (the default)
dectl --env prod salesdata list      # -> salesdata-prod-ds-do-thing-lambda
DECTL_ENV=staging dectl salesdata s3 export
```

Since `--env` is only known at runtime, `{env}` is substituted when a command runs, not when
the tree is built. `--help` and the alias info panels show the raw `{env}` token.

Resolution priority is `--env` > `DECTL_ENV` > `defaults.environment` in config > `dev`. This
assumes one AWS account across environments — only the names change.

To see which environment you're pointed at (and why), run `dectl env`, or just `dectl` — the
active env is printed as a banner above the help:

```bash
dectl env
# environment: prod  (from DECTL_ENV)
```

## Lambda dev loop vs release

Aliases are the short config keys, not the full AWS names. The verb is last, so the
deploy → run → logs loop only changes the trailing word.

```bash
# Fast local loop — updates $LATEST, test it directly
dectl salesdata lambda error-notifier deploy
dectl salesdata lambda error-notifier run --payload-file event.json
dectl salesdata lambda error-notifier run --json          # response as machine-readable JSON
dectl salesdata lambda error-notifier logs                # add --follow to tail
echo '{"key": "value"}' | dectl salesdata lambda error-notifier run --payload-file -

# Make it live behind the alias (S3 triggers / durable functions see it):
# updates $LATEST, publishes an immutable version, moves the configured live alias
dectl salesdata lambda error-notifier deploy --publish

# Canonical release through Jenkins + Terraform
dectl salesdata release                                   # add --plan for terraform plan only
```

`--publish` requires a `live_alias` on the function in config; without it, deploy only ever
touches `$LATEST` and alias-triggered functions keep running old code. `run` always targets
`$LATEST`, so it exercises the code from `deploy` even before you publish.

## Durable functions

A Lambda durable function's unit of work is the *execution*, not the invocation: one execution
checkpoints its way across many invocations and can suspend for up to a year between them. So a
function flagged `durable: true` in config swaps `run` and `logs` for an execution-scoped set —
the invocation-shaped views answer nothing useful, since `Invocations` counts replays and a
single log stream holds fragments of whichever executions that environment happened to serve.

```yaml
lambdas:
  order-workflow:
    name: salesdata-{env}-order-workflow
    source_dir: modules/lambda/order_workflow/code
    live_alias: live
    durable: true
```

```bash
dectl salesdata lambda order-workflow executions                 # which ones succeeded or failed
dectl salesdata lambda order-workflow executions --status FAILED
dectl salesdata lambda order-workflow history                    # steps/waits/retries of the latest
dectl salesdata lambda order-workflow history order-123 --follow
dectl salesdata lambda order-workflow logs                       # logger output for the latest
dectl salesdata lambda order-workflow logs order-123             # ...or for one named execution
dectl salesdata lambda order-workflow logs cfd01dc3a122          # ...or a unique tail of its name
dectl salesdata lambda order-workflow run --async --name order-123
```

`history` and `logs` are complements, and match the two console tabs: `history` is the
checkpoint log Lambda replays from — which step failed, what it returned, how long a wait
suspended for — while `logs` is what the function's own logger printed while doing it. The SDK
logger stamps the execution ARN onto every record, and `logs` filters the group on it, so a log
group carrying dozens of interleaved executions reads back as just the one. `--all` opts back
out to the raw group.

**Both take a unique tail of the name.** Without `run --name` an execution is named by a UUID,
which is not something anyone retypes, so `history` and `logs` accept any suffix long enough to
be unique — `logs cfd01dc3a122`, or fewer characters when fewer will do. The whole name is tried
first, so an execution whose name ends another's still wins itself, and a tail matching several
is an error listing them rather than a guess at which was meant. The tail rather than the head
because a UUID front-loads its timestamp, so a prefix carries almost no entropy.

The search covers the same number of executions per version that `executions --limit` is capped
at, so anything the table can print is something a tail can reach. An execution older than that
has to be named in full, or by its ARN.

**The opaque operation ids are folded away.** `logs` prints the operation as a tag beside the
level — `wait_for_files`, or `wait_for_files.3` on a retry — and hides the two ids that identify
it to the service, along with the execution ARN the tail is already filtered on. Everything else
stays, including `requestId`, which says which invocation of the execution spoke, and `attempt`
on a first try. `--context` restores the lot. Fields your own `extra=` put there are never
dropped.

`run` is qualified with the configured `live_alias` (falling back to `$LATEST`) — Lambda rejects
an unqualified invoke of a durable function outright, since an execution is pinned to the
version it starts on. Synchronous invocation waits for the whole execution and is capped at 15
minutes, so anything longer needs `run --async`; `--name` makes the start idempotent and gives
you a handle to pass to `history` and `logs`.

**Executions are keyed by version, not by alias.** Lambda resolves an alias to a version number
when the execution starts, so the alias name never appears in a durable execution ARN and
listing by alias fails outright (`cannot filter durable executions by alias`). dectl resolves
the alias to the version it currently points at and shows the resolution:

```bash
dectl salesdata lambda order-workflow executions
# order-workflow durable executions (live → version 7)

dectl salesdata lambda order-workflow executions --qualifier 6   # a specific version
dectl salesdata lambda order-workflow executions --all-versions  # merged across recent deploys
```

The consequence worth knowing: `deploy --publish` moves the alias to a new version, so runs from
before the deploy stay under the old one and drop out of the default list. `--all-versions`
merges the most recent published versions (and `$LATEST`), newest execution first, naming the
versions it scanned. `history` and `logs` already fall back to that sweep when a name isn't
found under the live version, so a name copied from the console resolves regardless of which
deploy ran it.

## Step Functions

```bash
dectl salesdata sfn ingest run                                   # start an execution, print its ARN
dectl salesdata sfn ingest run --payload-file input.json --follow  # start with input and tail history
dectl salesdata sfn ingest logs                                  # show the most recent execution's history
dectl salesdata sfn ingest runs                                  # recent executions with status (--json)
```

`logs` renders the execution history — the typed state transitions
(`TaskStateEntered`, `LambdaFunctionScheduled`, `ExecutionSucceeded`, …) — using
the `GetExecutionHistory` API, so no CloudWatch logging setup is required.

## Monitor a whole pipeline

`monitor` tails several resources at once as a single time-ordered stream, so you
can watch a multi-Lambda / Step Functions pipeline behave end to end without
opening each log by hand. What it watches is defined explicitly in config:

```yaml
pipelines:
  salesdata:
    monitor:
      lambdas: [ingest, transform, load]
      step_functions: [flow]      # needs log_group set on the state machine
```

```bash
dectl salesdata monitor    # interleaved, colored, one line per event, prefixed by resource
```

Each monitored line is prefixed and colored by its source, and the combined stream
is ordered by timestamp so you see the actual cross-resource sequence. Step
Functions is included only when the state machine has a `log_group` configured
(CloudWatch logging enabled); monitor tells you when one is missing.

## S3 buckets

Buckets are declared in config as an `alias -> real bucket name` mapping. Per-bucket verbs
(`mount`/`unmount`/`uri`) take the alias; the set-level `export` spans every bucket.

```bash
# Load every bucket URI into your shell as $pipeline_alias (lowercase), then use
# them with the aws CLI. A CLI can't set the parent shell's env, so you eval it:
eval "$(dectl salesdata s3 export)"           # --prefix overrides the variable name prefix
aws s3 cp "$salesdata_raw/incoming/file.txt" .

# One bucket's bare s3:// URI, for command substitution:
aws s3 ls "$(dectl salesdata s3 raw uri)"

# Mount a bucket as a local directory (Linux only — uses mount-s3 / FUSE):
dectl salesdata s3 raw mount     # -> ~/buckets/salesdata/raw
dectl salesdata s3 raw unmount
```

`s3 <alias> mount` refuses on macOS and points you at `export` instead.

## Iceberg tables

Tables are declared as an alias over the Glue Data Catalog pair that owns them:

```yaml
iceberg_tables:
  events:
    database: salesdata-{env}-catalog
    table: events
```

The verbs read the table's own metadata file. Glue holds a pointer to it, so a read is one
`GetTable` and one `GetObject` — no query engine, no workgroup, and nothing to switch on.

```bash
dectl salesdata iceberg events snapshots     # every commit: operation, rows added and removed
dectl salesdata iceberg events history       # each change of the current snapshot, rollbacks included
dectl salesdata iceberg events branches      # branches and tags, and what they point at
dectl salesdata iceberg events files         # file counts and average file size, commit by commit
dectl salesdata iceberg events diff          # what the last commit changed
```

Every one of those but `diff` is a list and takes `--limit`/`-n`, showing ten rows by default.
`--limit 0` means all of them, on all four.

`diff` is the one to reach for when a number moved. Give it the snapshot the number was last
right at and it names the commits that ran since, alongside the record, file and byte deltas:

```bash
dectl salesdata iceberg events diff 4471          # from that snapshot to now
dectl salesdata iceberg events diff 4471 9108     # between two snapshots
```

A snapshot is named by its full 19-digit id, by any unique tail of one, or by a branch or tag.
An ambiguous tail is an error listing the candidates rather than a guess.

Two limits are worth knowing. `files` reports the file layout from each snapshot's own summary
counters — file counts, total bytes and average size — and there is no row-per-data-file view,
because the manifests naming individual files are Avro and dectl does not read them. And a
snapshot that has been expired is gone from the metadata file, so nothing can reach it.

## Other commands

```bash
dectl salesdata glue source-copy run   # start a Glue job (add --follow to tail it)
dectl reference                        # the full command grammar, config-independent
dectl search my-bucket                 # find AWS resources by keyword (--json to script it)
dectl config show                      # inspect the loaded config (--json for the shape)
```

## Config

Config lives at `$XDG_CONFIG_HOME/dectl/config.yaml`, which is
`~/.config/dectl/config.yaml` when the variable is unset. `dectl config path` prints the
resolved path.

```bash
dectl config init        # write a starter config (fails if one already exists)
dectl config example     # print a full example of every option, for side-by-side reference
dectl config edit        # open it in $VISUAL / $EDITOR (seeds one from the template if missing)
dectl config validate    # check it parses, matches the schema, and its paths are usable
                         # add --json for {outcome, unusable_paths}
dectl config show        # resolved pipelines with alias -> AWS name mapping
dectl config path        # print the config file path
```

Unknown keys are rejected, so `config validate` catches a typo like `step_function:`
instead of silently ignoring it. `config example` prints to stdout (highlighted in a
terminal, plain when redirected), so you can display the full reference in one pane
while editing the real config in another.

### Running from anywhere: `resolve_paths_from`

Every read talks to AWS and needs no local file, so a read runs from any directory.
Deploys are the exception: a Glue job's `scripts` and a Lambda's `source_dir` are paths,
and without `resolve_paths_from` they resolve from the directory you happen to be standing
in.

Give a pipeline a `resolve_paths_from` and they resolve from that instead:

```yaml
pipelines:
  salesdata:
    resolve_paths_from: ~/code/salesdata
    lambdas:
      notifier:
        name: salesdata-{env}-notifier
        source_dir: modules/lambda/notifier/code
```

`dectl salesdata lambda notifier deploy` now zips `~/code/salesdata/modules/lambda/notifier/code`
from wherever you run it. With several pipelines each naming their own directory, one
config drives every checkout's deploys and you never change directory to do it.

The value must be absolute or start with `~`. A relative one would resolve against the
working directory, which is the dependency the key removes, so it fails validation. A
`{env}` token in it is substituted like any other name.

A `source_dir` may be absolute and sit outside that directory — it is zipped and uploaded
as bytes, so nothing else depends on where it came from. A glue `scripts` entry may not:
its S3 key is built from the string you write, and S3 stores that string as given. So a
script and its `script_prefix`, the two halves of that key, are each written as plain
relative segments — `jobs/copy.py`. Anything else names an object nothing will look for,
and `config validate` and the deploy both refuse it. The config still loads either way,
which is what keeps the rest of the CLI available to tell you.

A glue job that declares no scripts is refused for the same reason: a Glue job is defined
by a `ScriptLocation`, and there is none to build. A lambda `source_dir` that exists and
holds nothing is refused too — it zips to a valid empty archive, which replaces the
function's code with nothing.

Omit `resolve_paths_from` and nothing changes: paths resolve from the working directory,
and a deploy has to run from the checkout.

`config validate` checks a pipeline that names `resolve_paths_from` all the way down — the
directory, every script, every `source_dir` — and says of each one whether it is absent or
present with the wrong type, since those have opposite fixes. For a pipeline that names
none it still checks how the glue keys are written, which is answerable on any machine; only
whether a file is present needs a directory to look in.

An absent directory is reported on its own. Every path beneath it would carry the same one
cause, and ten lines each naming a file name that cause in none of them.

`config show` prints the resolved directory for every pipeline, with `~` expanded and
`{env}` substituted, and marks the ones falling back to the working directory. It prints
the resolved path of every script and `source_dir` beneath it, so the file a deploy would
send is on the page rather than inferred. `config show --json` and `list --json` carry the
same under `resolve_paths_from`, `script_paths` and `source_path`. `validate --json` emits
an object with `outcome` and `unusable_paths`, so a caller can tell a clean config from one
that could not be read at all.
