# dectl

Data engineering control CLI for driving AWS pipelines (Glue, Lambda, S3, Jenkins deploys)
from a single config file. Installed via `uv tool install`; the entry point is
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
`print_pipeline`).

## The `make_*_app` factory pattern

Every resource type is a module in `src/dectl/commands/` exposing a factory:

```python
def make_<resource>_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
```

The factory closes over the pipeline and config so each command already knows which pipeline it
belongs to — the commands themselves take only user arguments (an alias, a flag), never the
pipeline. Inside, a `resolve_*` helper turns a config **alias/shortname** into the full config
object (or real AWS name) and exits with a clear error on an unknown key. Follow this shape for
new resources: `glue.py` and `lambda_.py` are the reference implementations, `s3.py` the
newest.

Aliases matter: users reference the short config keys (`raw`, `source-copy`), never the real
AWS names. `dectl PIPELINE list` prints the alias → AWS-name mapping.

## Config

`src/dectl/config.py` — Pydantic models plus `load_config()` / `init_config()`. Config lives at
`~/.config/dectl/config.yaml`. `buckets` is a `shortname -> real-bucket-name` mapping (same
alias→name shape as `glue_jobs` and `lambdas`), not a fixed set of roles. When you change a
model, update `TEMPLATE_CONFIG` in the same file so `config init` stays valid.

## Cross-cutting modules

| Module | Responsibility |
|---|---|
| `session.py` | Builds the boto3 `Session` from config (region + optional profile). Every command that touches AWS goes through `make_session`. |
| `output.py` | The `rich` console and the `error`/`success`/`info` helpers. Use these, not bare `print`, for anything human-facing. |
| `logs.py` | CloudWatch log tailing shared by Glue and Lambda, including structured-JSON pretty-printing. |

## Gotchas

- **Glue `UpdateJob` replaces the whole job definition** — it does not patch. `glue.py`
  reconstructs the update from the existing definition and overrides only what dectl manages, so
  fields set outside dectl survive a deploy. See the comments there before touching it.
- **Lambda `$LATEST` vs. published alias** — `deploy` without `--publish` only moves `$LATEST`;
  alias-following triggers keep running the old published version until you `--publish`. `invoke`
  always targets `$LATEST`.
- **`s3 export` must stay eval-safe** — a CLI cannot mutate its parent shell, so `export` prints
  `export name='s3://…'` lines to stdout for `eval "$(...)"`. That output uses bare `print()`, not
  the rich console, so no markup or ANSI escapes leak into what gets evaluated.
- **`s3 mount` is Linux-only** — it shells out to `mount-s3` (Mountpoint for Amazon S3), which is
  FUSE-based and unavailable on macOS. The command detects a non-Linux OS and refuses with a
  pointer to `export`. Binaries are resolved with `shutil.which` (full path) so bandit's
  partial-path check (B607) stays clean without a `nosec`.

## Tests

`pytest` (`uv run pytest`). Unit tests mock boto3; command tests drive the real Typer apps via
`typer.testing.CliRunner` against a factory-built app. Live AWS integration tests are marked
`integration` and skipped unless `--run-integration` is passed — they create and delete real
resources.
