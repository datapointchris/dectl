# dectl

Data engineering control CLI for managing AWS pipelines.

## Installing

```bash
uv tool install git+https://github.com/datapointchris/dectl.git@latest
```

## Command grammar

Commands follow the shape `dectl PIPELINE RESOURCE ACTION [ALIAS] [OPTIONS]`.
Pipelines and their resources (`glue`, `lambda`, `deploy`) are built from your
config. Every level is self-documenting — run any partial command or add
`--help` to see what's available next, including the live list of aliases.

```bash
dectl                              # top-level help: pipelines + global commands
dectl uslegal                      # what the uslegal pipeline manages
dectl uslegal list                 # alias -> real AWS name mapping
dectl uslegal lambda --help        # functions and their actions
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

## Other commands

```bash
dectl uslegal glue run source-copy   # start a Glue job and tail its logs
dectl search my-bucket               # find AWS resources by keyword
dectl config show                    # inspect the loaded config
```

## Config

Config lives at `~/.config/dectl/config.yaml`. Run `dectl config init` to create
a template, then `dectl config show` to verify what loaded.
