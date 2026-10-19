# dectl

A command line for the loop between editing a data pipeline's code and watching it run on AWS. One
config file names each pipeline's Glue jobs, Lambda functions, Step Functions state machines, S3
buckets, Iceberg tables and Jenkins job under short aliases. dectl deploys code to them, runs them,
and reads their logs and history.

Terraform creates the resources and owns their definitions. dectl never creates one. It writes the
part that changes on every iteration: a Glue script, a Lambda's code, a state machine's definition.
Where a deploy touches a field Terraform also sets, dectl shows the diff and asks first. The Jenkins
job stays the canonical deploy, and dectl queues its builds without deciding what they do.

## Install

```bash
uv tool install --index https://pypi.ichrisbirch.com/simple/ dectl
dectl --version     # the release, and the commit it was built from
```

Every release is published to that package index, and nothing else is. Name it on the install.
uv records it beside the tool, which is where `dectl update` and `uv tool upgrade dectl` read it
back. Without it uv asks PyPI, which does not carry dectl. An install from a git branch or a local
checkout carries no release version, so `dectl update` refuses it.

`dectl update` installs the latest release over the running one. `dectl update --check` only
reports whether there is one. Once a day, on a terminal, a command that finds a later release prints
one line to stderr. Setting `NO_AUTO_UPDATE` or `DECTL_NO_AUTO_UPDATE` to anything turns that off.

## Starting from scratch

```bash
dectl reference                  # the whole grammar; needs no config and reaches no AWS
dectl config init                # an annotated starter exercising every option; refuses to overwrite
dectl config edit                # your region, profile and pipelines, {env} where the environment goes
dectl config validate            # schema, local paths and bucket names, offline; exits 3 on a fault
dectl env                        # which environment {env} becomes, and what chose it
dectl example-pipeline list      # each alias beside the AWS name it resolves to, still offline
dectl search source-copy         # the first call to AWS: proves the credentials, region and names
```

The examples in this file use the starter config's names: the pipeline `example-pipeline`, its Glue
job `source-copy`, and the rest. Rename them in the file and the commands follow, because dectl
builds its command tree from the config every time it starts.

`config init` writes `$XDG_CONFIG_HOME/dectl/config.yaml`, which is `~/.config/dectl/config.yaml`
when the variable is unset. `dectl config path` prints the resolved path. `config init` refuses when
a file is already there. `config edit` opens `$VISUAL`, then `$EDITOR`, and writes the starter first
when there is no file. `dectl config example` prints the same starter to stdout, for reading in one
pane while editing in another.

dectl holds no credentials of its own. AWS access comes from the profile `defaults.aws_profile`
names, or from boto3's default chain when it is blank. The Jenkins `user` and `token` can be written
as `${NAME}`, which reads them from that environment variable. A command needing an unset one
refuses and names it.

`search` is also how to find the names to put in the config. It matches a substring across S3,
Lambda functions and layers, Glue, Step Functions, IAM and Secrets Manager, in the configured
region.

**When a command is missing, run `dectl config validate`.** A config that does not load drops every
pipeline and leaves the `config` commands working. Typing a pipeline name then prints the load error
rather than "No such command". Unknown keys are errors, so a typo like `step_function:` fails here
instead of being ignored.

## The grammar

```text
dectl PIPELINE RESOURCE ALIAS VERB [OPTIONS]
```

The verb comes last. A deploy, run and logs loop on one resource changes only the final word.

A pipeline gets a resource command only for what its config declares: `glue`, `lambda`, `sfn`,
`s3`, `iceberg` or `jenkins`. A pipeline with only `buckets` has `s3` and nothing else. An alias is
the short key the config gives a resource, never its AWS name. `dectl PIPELINE list` prints the
mapping. `dectl reference` prints the whole grammar whatever the config holds.

One rule decides scope. Acting on one thing takes an alias: `glue JOB run`, `s3 BUCKET mount`.
Acting on the whole set takes none: `s3 export`, `jenkins status`, `list`, `monitor`. The argument's
presence selects the scope, never a flag.

Whether a command writes is structural too. A write's rehearsal is a `plan` verb beside it, never a
flag on the write. `plan` refuses everything the write would refuse, so a clean plan is a deploy
that will run. A partial command prints its help, with the aliases the config gives that level, and
acts on nothing.

Every command also answers to a shortcut. Each level of the tree gets the shortest prefix of its
name that its siblings do not share, and the prefixes run together into one token. `dectl exgsr` is
`dectl example-pipeline glue source-copy run`. Help shows each command's sequence in parentheses.

stdout carries the answer and stderr carries everything else. A command that answers with a record
takes `--json`. A log tail does not, and neither does the eval-ready output of `s3`. `--no-input`
makes a command that would prompt refuse instead, naming the flag that answers the prompt.

## Which surface answers which question

| Ask | Surface | Reads |
| --- | --- | --- |
| What does this pipeline manage, under which AWS names | `PIPELINE list`, `config show` | the config, for the active environment |
| Which environment am I pointed at | `env` | `--env`, `DECTL_ENV`, the config |
| What is that resource really called | `search` | S3, Lambda, Glue, Step Functions, IAM, Secrets Manager |
| What would a deploy change | `glue ALIAS plan`, `sfn ALIAS plan` | the live definition, against the config and local files |
| Push this code and try it | `ALIAS deploy`, then `ALIAS run` | the local files the config names |
| Why did that run fail | `ALIAS logs`, `ALIAS runs`, a durable function's `history` | CloudWatch Logs and each service's run records |
| What is the whole pipeline doing | `PIPELINE monitor` | the log groups the `monitor` block selects |
| Ship through the canonical path | `jenkins ACTION`, `jenkins status` | the pipeline's Jenkins job |
| Give the aws CLI a bucket | `s3 export`, `s3 ALIAS uri` | the config |
| When did this table's numbers move | `iceberg ALIAS diff`, `iceberg ALIAS snapshots` | the Glue catalog entry and the table's metadata file |
| Can this config be used | `config validate` | the config, and the files it names |

## `--env` — one config drives every environment by swapping one token

```yaml
lambdas:
  my-function:
    name: my-{env}-lambda-function
```

```bash
dectl example-pipeline list                  # my-dev-lambda-function, from the config's environment
dectl --env prod example-pipeline list       # my-prod-lambda-function
DECTL_ENV=staging dectl example-pipeline s3 export
DECTL_ENV=staging dectl env                  # environment: staging  (from DECTL_ENV)
```

The priority is `--env`, then `DECTL_ENV`, then `defaults.environment` in the config, then `dev`.
dectl substitutes `{env}` when a command runs, never when it builds the tree, because `--env` is not
known until then. So `--help` and the alias panels show the raw `{env}` token. `--help` shows the
config's environment as the default even when `DECTL_ENV` overrides it. Bare `dectl` prints the
active environment and its source above the help, and `dectl env` prints it alone.

The scheme assumes one AWS account across environments. Only the names change.

A name that hardcodes its environment ignores `--env`. A deploy with `--env prod` against a name
spelled `my-dev-function` acts on dev and succeeds. So when `--env` or `DECTL_ENV` was set and the
resource carries no `{env}`, dectl warns once on stderr that the environment changed nothing.

## `glue` — a deploy pushes the script and confirms any change to the definition

```bash
dectl example-pipeline glue conform plan          # the definition diff; uploads and writes nothing
dectl example-pipeline glue conform deploy        # upload the scripts, show the diff, confirm
dectl example-pipeline glue conform run --follow  # start a run and tail its output and error logs
dectl example-pipeline glue conform runs          # recent runs, with --json to script them
```

A deploy is two writes with different owners. The script upload is always yours. The job definition
is Terraform's once a pipeline is established. dectl keeps write access to it for the stretch before
Terraform exists, when changing a worker type should not take a commit, a build and an apply.
Because Terraform may own the definition, `deploy` diffs its computed definition against the live
one and confirms before applying it. `deploy --yes` skips the prompt.

`deploy` runs every check that can refuse before it writes anything. A refusal after the upload
would leave the job pointing at code you were just told not to deploy.

`run --follow` stops when the run ends, and exits non-zero when the run failed.

### dectl manages what the config names, preserves the rest, and creates nothing

`deploy` reads the live definition first, so the job has to exist. `UpdateJob` replaces the whole
definition rather than patching it, so a field dectl did not carry over would reset to its default
on every deploy. dectl starts from the live definition and overrides only what the config names.
Omit a key and the live value survives. That is how a Terraform-owned field outlives a deploy run to
change something else.

When nothing the config names differs, dectl sends no `UpdateJob`. That is the steady state: a
deploy is a pure code push with no drift surface.

| Config key | What it sets |
| --- | --- |
| `role` | the IAM role the job runs as |
| `scripts` | `ScriptLocation` from the first entry; the rest become `--extra-py-files` |
| `connections` | authoritative: a name dropped from the list is detached |
| `arguments` | merged onto the job's existing default arguments |
| `max_capacity` | DPU sizing: every Python shell job, and a Spark job that predates workers |
| `worker_type`, `number_of_workers` | worker sizing, Spark only |
| `glue_version` | the Glue runtime |
| `python_version` | the Python inside `Command` |
| `timeout_minutes` | `Timeout`, which Glue counts in minutes |
| `max_retries` | reruns after a failure |
| `max_concurrent_runs` | `ExecutionProperty.MaxConcurrentRuns` |
| `execution_class` | `STANDARD` or `FLEX` |

`role` and `scripts` are required, and every other key is optional. Omit one and dectl writes
nothing for it, so whatever Terraform or a console edit last set survives. Leaving `connections` out
leaves the job's connections alone, and `[]` detaches them all. The diff also reports keys a deploy
drops.

`list` prints what each job's config decides, so the split is answerable without a deploy:

```bash
dectl example-pipeline list --json | jq '.glue.conform.managed'
```

`max_retries: 0` is the one worth setting by hand while iterating. A job at three retries takes
three runs to report a failure, which is three tracebacks to read instead of one.
`execution_class: FLEX` runs on spare capacity for less money and starts when it starts. That suits
a loop and not a schedule.

### A job sizes by DPU or by workers, never both

A Python shell job takes `max_capacity`, which is `0.0625` (1 GB) or `1` (16 GB). A Spark job takes
`worker_type` with `number_of_workers`, unless it is old enough to be sized by DPU. `UpdateJob`
rejects a definition carrying both.

dectl refuses these before it uploads anything:

- a config naming both models
- one half of the worker pair
- `max_capacity` against a job Glue already sizes by workers
- workers against a Python shell job

Glue refuses each of them itself, but from `UpdateJob`. That runs after the scripts are uploaded,
and its message names neither the job nor the config key.

Migrating a DPU-sized Spark job to workers works, and the diff shows the DPU value leaving as the
workers arrive. The reverse is refused. No Glue runtime takes `MaxCapacity` on a Spark job and
leaves `WorkerType` off, so no update returns a job to DPU sizing. A Spark job's derived
`MaxCapacity` never shows in the diff, and `JobUpdate` in `src/dectl/commands/glue.py` says why.

## `lambda` — `deploy` moves `$LATEST`, and only `--publish` moves what triggers run

```bash
dectl example-pipeline lambda my-function deploy                         # zip source_dir, update $LATEST
dectl example-pipeline lambda my-function run --payload-file event.json  # invoke $LATEST, print the response
echo '{"key": "value"}' | dectl example-pipeline lambda my-function run --payload-file -
dectl example-pipeline lambda my-function logs --follow                  # tail its CloudWatch logs
dectl example-pipeline lambda my-function deploy --publish               # publish a version, move live_alias
```

`run` always targets `$LATEST`, so it exercises a deploy before anything is published. Anything that
invokes through an alias, such as an S3 trigger, keeps running the old version. `deploy --publish`
publishes a version and moves the function's `live_alias` to it. With no `live_alias` configured,
`--publish` still publishes a version and moves nothing.

A payload comes from a file, or from stdin as `-`, never from an inline argument.

`run` waits for the function's own timeout plus a margin. A call that does not answer within it is
reported, not retried. `Invoke` is the one Lambda call that cannot be taken back, so dectl sends it
with the SDK's retries off. botocore would otherwise re-send a long invoke. The function's own retry
setting cannot stop that, because Lambda never sees a client's HTTP retries.

### Durable functions — the unit of work is the execution, not the invocation

A durable function checkpoints one execution across many invocations, and can suspend for up to a
year between them. The invocation-shaped views answer nothing there. `Invocations` counts replays,
and one log stream holds fragments of whichever executions that environment served. So
`durable: true` replaces `run` and `logs` with execution-scoped versions, and adds `executions` and
`history`.

```bash
dectl example-pipeline lambda my-workflow run --async --name order-123   # start one, named, and return
dectl example-pipeline lambda my-workflow executions --status FAILED     # which ones failed
dectl example-pipeline lambda my-workflow history order-123 --follow     # its steps, waits and retries
dectl example-pipeline lambda my-workflow logs cfd01dc3a122              # its logger output, by a name tail
```

`history` and `logs` are complements, and match the console's two tabs. `history` is the checkpoint
log Lambda replays from: which step failed, what it returned, how long a wait suspended. `logs` is
what the function's own logger printed meanwhile. The SDK logger stamps the execution ARN on every
record, and `logs` filters on it. A log group carrying dozens of interleaved executions reads back
as one. `logs --all` tails the raw group instead.

`logs` folds away the fields that repeat on every line of one execution. The operation shows as a
tag beside the level, such as `wait_for_files`, or `wait_for_files.3` on a retry. `--context`
restores every field. Fields your own `extra=` added are never dropped.

**An execution is named by any unique tail of its name.** Without `run --name`, Lambda names an
execution with a UUID, which nobody retypes. `history` and `logs` take any suffix long enough to be
unique. The whole name is tried first, so a name that ends another's still finds itself. A tail
matching several is an error listing them. The tail rather than the head, because a UUID's prefix
carries the timestamp and little else.

The tail search covers the same window `executions --limit` is capped at, so any row the table
prints is reachable. An older execution has to be named in full, or by its ARN.

`run` is qualified with `live_alias`, falling back to `$LATEST`. Lambda rejects an unqualified
invoke of a durable function, because an execution is pinned to the version it starts on. A
synchronous run waits for the whole execution and is capped at 15 minutes, so anything longer needs
`--async`. `--name` makes the start idempotent and gives a handle for `history` and `logs`.

**Executions are listed by version, not by alias.** Lambda resolves an alias to a version when the
execution starts, and listing by alias fails outright. dectl resolves `live_alias` to the version it
points at, and the listing's title shows the resolution, such as `live → version 7`. So
`deploy --publish` moves the listing to the version it published, and earlier runs drop out of it.
`executions --all-versions` merges recent published versions and `$LATEST`, latest first, naming the
versions it scanned. `history` and `logs` fall back to that sweep when a name is not under the live
version. A name copied from the console resolves whichever deploy ran it.

## `sfn` — a definition pushed byte for byte as Terraform would render it

```bash
dectl example-pipeline sfn my-flow run --payload-file input.json --follow  # start, then tail its history
dectl example-pipeline sfn my-flow logs                                    # the latest execution's history
dectl example-pipeline sfn my-flow runs                                    # recent executions and status
dectl example-pipeline sfn my-flow plan                                    # diff against the live definition
dectl example-pipeline sfn my-flow deploy --publish                        # replace it, move live_alias
```

`logs` reads `GetExecutionHistory`, the typed state transitions, so it needs no CloudWatch logging.
That API covers Standard workflows only, and Express workflows have no history API at all.
`monitor` reads the state machine's CloudWatch log group instead, so it can interleave with
Lambda's.

`plan` and `deploy` exist only for a machine whose config names a `definition_file`. That is an
`.asl.json` file Terraform renders with `templatefile()`. Each `${name}` in it needs an entry under
`definition_substitutions`:

```yaml
step_functions:
  my-flow:
    name: my-{env}-state-machine
    definition_file: modules/step_functions/my_flow.asl.json
    definition_substitutions:
      process_function_arn: arn:aws:lambda:us-east-2:123456789012:function:my-{env}-lambda-function
      copy_timeout_seconds: 3600
    live_alias: live            # optional; what deploy --publish moves
```

`deploy` renders the file as Terraform would, byte for byte, then validates it with Step Functions.
It replaces only the definition. The role, logging and tracing stay as Terraform set them. A
rendered definition identical to the live one is not written. Identical bytes are what make
Terraform's next plan over the same file report no change. Commit the same file and that holds.
Leave it uncommitted and the next `terraform apply` puts the committed definition back.

There is no confirm prompt, unlike Glue. The definition is the code being iterated on, so a prompt
would fire on every deploy. The diff prints every time instead.

A placeholder with no entry is refused with its line, and with the value the live definition holds
there. That value is printed as a ready config entry, with `{env}` in place of the active
environment's name.

- **Only `${name}` is substituted.** `$${` and `%%{` escape as Terraform escapes them. A `%{ if }`
  directive or an expression such as `${jsonencode(x)}` is refused rather than guessed at.
- **JSONata needs no escaping.** `{% ... %}` opens with a brace, and neither Terraform nor dectl
  reads that as a placeholder. A `${name}` inside an expression is substituted like any other:
  `"{% ${load_enabled} and $count($orders) >= ${min_orders} %}"`. The one JSONata form that does
  collide is a `$` followed by `{`, as in `$orders.${ customer: $sum(total) }`, which the file
  writes as `$orders.$${ customer: $sum(total) }`.
- **A substitution's value can be a whole JSONata expression.** It lands inside a JSON string in
  the definition, so its string literals take single quotes, and the YAML value is double-quoted
  because it starts with `{`. A config variable inside it is substituted like any other:
  `landing_key: "{% '{landing}' & $states.input.date & '.json' %}"`. JSONata's own braces always
  hold more than a name, as `{'stage': 'loaded'}` does, so no variable matches them.
- **A number or boolean renders as Terraform renders it.** `copy_timeout_seconds: 3600` fills an
  unquoted `"TimeoutSeconds": ${copy_timeout_seconds}`, and `2.0` renders as `2`.
- **`{env}` belongs to the values, never the file.** A template's own `${env}` is a Terraform
  variable like any other, and needs an entry such as `env: "{env}"`.
- **`run` starts the latest definition, and an alias does not follow it.** Anything starting
  executions through an alias keeps the old version until `deploy --publish`. `--publish` publishes
  the current revision whether or not it changed. An execution started within a few seconds of a
  deploy may still run the previous definition.

## `jenkins` — dectl queues the build, and the Jenkinsfile decides what it does

```bash
dectl example-pipeline jenkins plan            # queue a build posting ACTION=dry_run, as the starter has it
dectl example-pipeline jenkins apply --follow  # queue one posting ACTION=deploy and stream its console
dectl example-pipeline jenkins status          # the most recent build's result
dectl example-pipeline jenkins logs --follow   # its console output
```

`jenkins` has no aliases, since a pipeline has one job. Its writes are the keys of the job's
`actions` in the config. Each action posts the job's `parameters` with its own on top. dectl names
no action itself. What `ACTION=dry_run` does belongs to the Jenkinsfile, which dectl never sees, so
a verb dectl named would be a guess at it. An action cannot be called `status` or `logs`, because it
would collide with that read.

The command appears only when the config has both `jenkins` blocks. The top-level one names the
server and its credentials. The pipeline's own names the job.

## `monitor` — several resources tailed as one stream, in time order

```yaml
pipelines:
  example-pipeline:
    monitor:
      lambdas: [my-function]
      step_functions: [my-flow]     # needs log_group set on the state machine
```

```bash
dectl example-pipeline monitor     # one line per event, prefixed and colored by its source
```

`monitor` is a pipeline-level command because it spans resources. It tails what the `monitor` block
selects, so a pipeline of several functions reads end to end without opening each log. A state
machine joins only when it has a `log_group`, which means CloudWatch logging is on. `monitor` warns
about each one missing.

Every tailer follows whole log groups by time, never stream by stream. Lambda writes each execution
environment to its own stream, and Glue creates its error stream only on the first write to stderr.
Waiting for streams to appear costs minutes of silence and misses a stream created later.

## `s3` — bucket names for the aws CLI, not another copy of it

```bash
eval "$(dectl example-pipeline s3 export)"          # $example_pipeline_raw and the rest, in this shell
aws s3 cp "$example_pipeline_raw/incoming/file.txt" .
aws s3 ls "$(dectl example-pipeline s3 raw uri)"     # one bucket's bare s3:// URI
dectl example-pipeline s3 raw mount                  # at ~/buckets/example-pipeline/raw, Linux only
dectl example-pipeline s3 raw unmount
```

A CLI cannot set its parent shell's environment, so `export` prints the assignments for `eval`.
Each bucket becomes a lowercase variable named for the pipeline and the alias, with hyphens turned
to underscores. `--prefix` replaces the pipeline name. `export` and `uri` print no markup and no
color codes, so command substitution gets exactly the value.

`mount` shells out to `mount-s3`, Mountpoint for Amazon S3, which is FUSE-based and Linux-only. On
any other system it refuses and points at `export`.

## `iceberg` — table history read from its metadata file, with no query engine

```yaml
iceberg_tables:
  events:
    database: my-{env}-catalog
    table: events
```

```bash
dectl example-pipeline iceberg events snapshots     # every commit: operation, rows added and removed
dectl example-pipeline iceberg events history       # each change of the current snapshot, rollbacks included
dectl example-pipeline iceberg events files         # file counts and average size, commit by commit
dectl example-pipeline iceberg events diff 4471     # what ran since that snapshot, and every counter moved
```

Glue holds a pointer to the table's metadata file, so a read is one `GetTable` and one `GetObject`.
There is no query engine, no workgroup and nothing to switch on. A table missing that pointer is not
an Iceberg table, and dectl says so.

`diff` is the one to reach for when a number moved. With no argument it compares the current
snapshot with its parent. Given the snapshot the number was last right at, it lists the commits
since, beside the record, file and byte deltas. Given two, it compares those. A snapshot is named by
its full id, any unique tail of one, or a branch or tag. An ambiguous tail is an error listing the
candidates.

The list verbs take `--limit`, a row count. Leave it off for every row. The whole answer is one
metadata file already read, so the flag trims the display rather than the fetch.

`files` reports each snapshot's own summary counters. There is no row-per-data-file view, because
the manifests naming individual files are Avro and dectl does not read them. Reading them would
more than double the install. An expired snapshot is gone from the metadata file, so nothing can
reach it.

dectl only reads tables. Compaction, snapshot expiry, orphan removal and rollback are not here.

## `config` — one file builds the command tree, and a typo in it is an error

```bash
dectl config path        # the resolved config path, whether or not a file is there
dectl config show        # every pipeline resolved: names, managed fields and paths
dectl config validate    # exit 0 when clean, 3 otherwise; --json for outcome and unusable_values
```

Every model rejects unknown keys. `config validate` checks that the file parses, matches the schema,
and names values that can be used. Bucket names are checked on any machine. `script_bucket` and
every entry under `buckets` must be a name S3 accepts, as the active environment resolves it. A
refused name is printed beside every arm of S3's naming rule, reserved prefixes and suffixes
included. How each Glue script key is written is checked on any machine too. Whether a file is
present needs a directory to look in, which `resolve_paths_from` supplies.

`validate --json` emits `outcome` and `unusable_values`. The outcome separates a clean config from
one that could not be read at all.

### `variables` — a value written once and used as `{name}` everywhere

```yaml
variables:
  account: "123456789012"
  lambda_arn_prefix: arn:aws:lambda:us-east-2:{account}:function
pipelines:
  sales:
    variables:
      raw_bucket: "acme-{env}-raw"
    buckets:
      raw: "{raw_bucket}"
    step_functions:
      flow:
        definition_substitutions:
          process_arn: "{lambda_arn_prefix}:acme-{env}-process"
```

The top-level block reaches every string in the file. A pipeline's block reaches that pipeline
alone. Variables are substituted when the config loads. `{env}` passes through untouched and is
substituted when a command runs. A variable sees the ones defined above it, so one can be built from
another. `config show` and `list` print the substituted values.

Each of these is refused when the config loads, naming the key:

- a variable named `env`, or named with anything but letters, digits, `_` and `-`;
- a pipeline variable redefining a top-level one;
- a `{name}` that names no variable, so a typo never reaches AWS as a literal name;
- a `{name}` defined only in another pipeline's block. The message names that block, and offers
  moving the variable to the top level only when its value uses no pipeline variable;
- an unquoted `{name}`, which YAML reads as a mapping;
- a value that is not a string, including a block meant for reuse, which is an anchor's job.

A value another program fills, such as a Glue argument `s3://b/out/{date}/`, writes the braces
doubled: `s3://b/out/{{date}}/` reaches the job as `s3://b/out/{date}/`. `${NAME}` is never a
variable, so a Terraform placeholder or an environment-variable reference passes through. Neither
is a brace holding anything but a name, such as a JSON payload.

A fault found in a substituted value, such as a bucket name S3 refuses, prints the value as the
file wrote it beside the variables it used, since the character to fix may sit in a variable.

Quote a value that starts with `{`. YAML reads a leading brace as an inline mapping, so
`raw: {raw_bucket}` parses as a mapping and `raw: {company}-raw` does not parse. A brace later in
the value needs no quotes. Quote an account id too, because YAML reads an unquoted `0123` as octal.

YAML anchors repeat a whole block, and variables fill what differs in each copy. YAML copies the
block as the file wrote it, before any variable is filled, so a copy resolves against the pipeline
it lands in:

```yaml
variables:
  company: acme
  account: "123456789012"
pipelines:
  sales: &pipeline
    variables:
      prefix: "{company}-{env}-sales"
      landing: &landing s3://{prefix}-raw/incoming/
    buckets:
      raw: "{prefix}-raw"
    glue_jobs:
      load: &load
        name: "{prefix}-load"
        role: arn:aws:iam::{account}:role/{prefix}-glue
      backfill:
        <<: *load
        name: "{prefix}-backfill"
  finance:
    <<: *pipeline
    variables:
      prefix: "{company}-{env}-finance"
      landing: *landing
```

`finance` resolves to `acme-dev-finance-raw` and `acme-dev-finance-backfill`. A `<<:` merge is one
level deep, so `finance`'s `variables` block replaces the copied one whole. It restates every
variable, and `*landing` repeats a shared value. An anchor is written where its block is first
used, since the config has no key for holding anchors alone. Write it after the colon and before
the quotes, as `&landing` is above.

### `resolve_paths_from` — deploys that reach the same files from any directory

Every read talks to AWS and needs no local file, so reads run from any directory. Deploys are the
exception. A Glue job's `scripts`, a Lambda's `source_dir` and a state machine's `definition_file`
are paths. With no `resolve_paths_from`, they resolve from the directory you are standing in.

```yaml
pipelines:
  example-pipeline:
    resolve_paths_from: ~/code/example-pipeline
    lambdas:
      my-function:
        name: my-{env}-lambda-function
        source_dir: modules/lambda/my_function/code
```

`dectl example-pipeline lambda my-function deploy` then zips
`~/code/example-pipeline/modules/lambda/my_function/code` from anywhere. With several pipelines each
naming its own directory, one config drives every checkout's deploys.

The value must be absolute or start with `~`. A relative one would resolve against the working
directory, which is the dependency the key removes, so it fails validation. A `{env}` token in it is
substituted like any other name.

A `source_dir` may be absolute and sit outside that directory. It is zipped and uploaded as bytes,
so nothing depends on where it came from. A Glue script may not. Its S3 key is built from the string
as written. So a script and its `script_prefix`, the two halves of that key, are each plain relative
segments, like `jobs/copy.py`. Anything else spells one file two ways. The key carries a `..`, a
`./` or a doubled separator literally, so a second machine writing `jobs/copy.py` writes a different
object. `config validate` and the deploy both refuse it. The config still loads, which keeps the
rest of the CLI available to say so.

A Glue job declaring no scripts is refused, because a job is defined by its `ScriptLocation` and
there is none to build. A `source_dir` that exists and holds nothing is also refused. It would zip
to a valid empty archive and replace the function's code with nothing.

With `resolve_paths_from` set, `config validate` checks the directory and every path beneath it. It
says of each whether it is absent or present with the wrong type, since those have opposite fixes.
An absent directory is reported once, rather than once per file beneath it.

`config show` and `list` print the resolved directory for every pipeline, with `~` expanded and
`{env}` substituted, and mark the ones falling back to the working directory. They print the
resolved path of every declared path beneath it, named by its config key, so the file a deploy would
send is on the page. Their `--json` carries the same under `resolve_paths_from`, and under `paths`
on each resource that declares one.

## Where it writes

- `$XDG_CONFIG_HOME/dectl/config.yaml`: the config, written only by `config init` and `config edit`.
- `$XDG_CACHE_HOME/clisteno/` and `$XDG_STATE_HOME/clisteno/`: the shortcut table, computed from the
  live command tree.
- `$XDG_STATE_HOME/dectl/`: when the update check last ran, and what it found.
- `~/buckets/PIPELINE/ALIAS`: the directory `s3 ALIAS mount` creates and mounts on.
