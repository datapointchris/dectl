# dectl

Data engineering control CLI for managing AWS pipelines.

## Installing

```bash
uv tool install git+https://github.com/datapointchris/dectl.git@latest
```

## Usage

```bash
dectl --help
dectl config init
dectl uslegal deploy
dectl uslegal run
dectl aana deploy download
dectl aana invoke download
dectl search uslegal
```

## Config

Config lives at `~/.config/dectl/config.yaml`.

Run `dectl config init` to create a template.
