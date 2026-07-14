# dectl

Data engineering control CLI for managing AWS pipelines.

## Installing

```bash
uv tool install git+https://github.com/datapointchris/dectl.git@latest
```

## Command grammar

Commands follow the shape `dectl PIPELINE RESOURCE ACTION [ALIAS] [OPTIONS]`.
Pipelines and their resources (`glue`, `lambda`, `sfn`, `s3`, `deploy`) are built
from your config. Every level is self-documenting — run any partial command or add
`--help` to see what's available next, including the live list of aliases.

```bash
dectl                              # top-level help: pipelines + global commands
dectl uslegal                      # what the uslegal pipeline manages
dectl uslegal list                 # alias -> real AWS name mapping
dectl uslegal lambda --help        # functions and their actions
```

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

Resolution priority is `--env` > `DECTL_ENV` > `defaults.environment` in config > `dev`. This
assumes one AWS account across environments — only the names change.

To see which environment you're pointed at (and why), run `dectl env`, or just `dectl` — the
active env is printed as a banner above the help:

```bash
dectl env
# environment: prod  (from DECTL_ENV)
```

## Lambda dev loop vs release

`ALIAS` values are the short config keys, not the full AWS names.

```bash
# Fast local loop — updates $LATEST, test it directly
dectl uslegal lambda deploy error-notifier
dectl uslegal lambda invoke error-notifier '{"key": "value"}'
dectl uslegal lambda logs error-notifier

# Make it live behind the alias (S3 triggers / durable functions see it):
# updates $LATEST, publishes an immutable version, moves the configured alias
dectl uslegal lambda deploy error-notifier --publish

# Canonical release through Jenkins + Terraform
dectl uslegal deploy
```

`--publish` requires an `alias` on the function in config; without it, deploy
only ever touches `$LATEST` and alias-triggered functions keep running old code.

## Step Functions

```bash
dectl uslegal sfn start ingest                 # start an execution, print its ARN
dectl uslegal sfn start ingest '{"k":"v"}' -f  # start with input and tail the history
dectl uslegal sfn watch ingest                 # tail the most recent execution
dectl uslegal sfn list ingest                  # recent executions with status
```

`watch` renders the execution history — the typed state transitions
(`TaskStateEntered`, `LambdaFunctionScheduled`, `ExecutionSucceeded`, …) — using
the `GetExecutionHistory` API, so no CloudWatch logging setup is required.

## Monitor a whole pipeline

`monitor` tails several resources at once as a single time-ordered stream, so you
can watch a multi-Lambda / Step Functions pipeline behave end to end without
opening each log by hand. What it watches is defined explicitly in config:

```yaml
pipelines:
  uslegal:
    monitor:
      lambdas: [ingest, transform, load]
      step_functions: [flow]      # needs log_group set on the state machine
```

```bash
dectl uslegal monitor    # interleaved, colored, one line per event, prefixed by resource
```

Each monitored line is prefixed and colored by its source, and the combined stream
is ordered by timestamp so you see the actual cross-resource sequence. Step
Functions is included only when the state machine has a `log_group` configured
(CloudWatch logging enabled); monitor tells you when one is missing.

## S3 buckets

Buckets are declared in config as a `shortname -> real bucket name` mapping. Two
ways to work with them:

```bash
# Load bucket URIs into your shell as $pipeline_shortname (lowercase), then use
# them with the aws CLI. A CLI can't set the parent shell's env, so you eval it:
eval "$(dectl uslegal s3 export)"
aws s3 cp "$uslegal_raw/incoming/file.txt" .

# Mount a bucket as a local directory (Linux only — uses mount-s3 / FUSE):
dectl uslegal s3 mount raw       # -> ~/.cache/dectl/mounts/uslegal/raw
dectl uslegal s3 unmount raw
```

`s3 mount` refuses on macOS and points you at `export` instead.

## Other commands

```bash
dectl uslegal glue run source-copy   # start a Glue job and tail its logs
dectl search my-bucket               # find AWS resources by keyword
dectl config show                    # inspect the loaded config
```

## Config

Config lives at `~/.config/dectl/config.yaml`. Run `dectl config init` to create
a template, then `dectl config show` to verify what loaded.
