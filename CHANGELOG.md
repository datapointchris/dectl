# CHANGELOG


## v2.8.2 (2026-08-31)

### Bug Fixes

- **lambda**: Keep the retries that cannot duplicate a run, and split the wait per invocation
  ([`d0d819f`](https://github.com/datapointchris/dectl/commit/d0d819f6b025ef388981fbd901aab166a60640fa))

A client that retries nothing also drops the retries which never could start a second execution, and
  a concurrency throttle is the common one. Lambda returns TooManyRequestsException whenever a
  function is at its limit, so a routine refusal that botocore used to absorb became an unhandled
  traceback. issue_invoke re-sends only what Lambda declined to admit — a 429 or an EC2 throttle,
  where no execution started — with exponential backoff, and reports each attempt. Everything else
  either started an execution or may have, and none of it is re-sent.

The wait is now resolved after --async is read. An Event invoke returns as soon as Lambda has the
  event, so the synchronous ceiling blocked the command for 15.5 minutes on a stalled endpoint, and
  the refusal claimed an execution was running that may never have been queued.

The fallback warning interpolates the wait it returns. It named a number 30 seconds short of the one
  taken, so one invocation reported two different waits.

A connect failure is reported rather than raised as a traceback. It is the one failure that
  certainly invoked nothing, and it is not a ReadTimeoutError, so it can never be described as still
  running.

The invoke domain moves to invoke.py, leaving session.py to build sessions. Same boundary as
  durable.py beside logs.py: this is the Lambda API rather than a boto3 concern, and session.py had
  grown a Lambda read and two imports for it.

botocore is declared. Six modules import it directly and none of them was covered by the boto3
  declaration alone.

The refusal messages are built by named functions, so the tests assert what the command emitted
  rather than words that can be reworded out from under them. The plain run's refusal now names
  logs, the command that shows a still-running function's output.

- **lambda**: Stop a slow invoke from retrying into overlapping runs
  ([`137e5b4`](https://github.com/datapointchris/dectl/commit/137e5b48b9f89d2eda050f543ed83a20ea59a1a3))

Every client from make_session carries botocore's defaults: a 60-second read timeout, legacy retry
  mode, five attempts. Invoke is the one call dectl makes that AWS cannot take back, so those
  defaults turn any function slower than a minute into five overlapping copies of itself — each
  retry is a fresh invocation, Lambda cancels nothing, and the copies race each other over whatever
  the function writes. The function's own MaximumRetryAttempts reaches none of it, because these are
  the client's HTTP retries.

make_invoke_client builds a lambda client for Invoke alone: no retries, and a read timeout that
  outlives the function. invoke_read_timeout takes that wait from the function's own Timeout, so a
  three-second function fails in thirty-three seconds rather than hanging for the fifteen-minute
  ceiling. A durable function skips the lookup, because a synchronous durable invoke waits for the
  whole execution and Lambda caps that at fifteen minutes regardless. Invoke and
  GetFunctionConfiguration are separate permissions, so a failed lookup falls back to the ceiling
  and warns.

make_session is untouched. Every other call dectl makes is a read, where a retry costs one round
  trip and is wanted.

The config uses total_max_attempts, which counts the initial request. The max_attempts key counts
  retries on top of it, so 1 there still fires the duplicate. test_session.py pins both against a
  socket that accepts connections and answers none, counting the requests that arrive — botocore's
  retry loop sits below the client object, so no fake can reach it.

- **lambda**: Stop a slow invoke from retrying into overlapping runs
  ([#3](https://github.com/datapointchris/dectl/pull/3),
  [`7284a11`](https://github.com/datapointchris/dectl/commit/7284a11c0dd61eed729d6ad4d9b41e39e70469fa))

Every client `make_session` hands out carries botocore's defaults, and `Invoke` is the one call
  dectl makes that AWS cannot take back. A function slower than the 60-second default read timeout
  is therefore invoked five times, each retry a fresh run Lambda cannot cancel. This moves the
  Invoke domain into `invoke.py`, sends it through a client that retries nothing, and re-issues by
  hand the one failure class that provably started nothing.

## What to look at

- `src/dectl/invoke.py` — `make_invoke_client` uses `total_max_attempts`, not `max_attempts`. The
  two keys differ by exactly one invocation and read alike, so check that one. -
  `src/dectl/invoke.py` — `admission_refused` decides what may be sent twice. Check that every code
  it admits is one where Lambda declined to start an execution, and that nothing ambiguous is in it.
  A wrong answer here reintroduces the original bug by another route. - `src/dectl/invoke.py` —
  `invoke_read_timeout` returns three different waits for three different things being waited on.
  Check that the `run_async` branch precedes the `durable` one, and that the `ClientError` fallback
  errs long rather than short. - `src/dectl/commands/lambda_.py:197` and `:277` — two clients from
  one session. The read client answers `get_function_configuration`, and in the durable verb also
  `get_alias` and `tail_durable_history`. Check that only the invoking client invokes, and that the
  wait is resolved after `run_async` is known. - `tests/test_invoke.py` — the socket fixture. Check
  that it asserts on requests that arrived, not on the config that was requested.

## How it was verified

`uv run pytest` — 276 passed, 6 skipped, 6.6s. The two socket tests add 1.1s and need no AWS.

Five mutations were applied to the source and the suite re-run against each:

| Mutation | Tests that failed | | --- | --- | | `total_max_attempts: 1` → `max_attempts: 1` | 16 |
  | `--async` falls through to the synchronous wait | 3 | | the fallback warning names the ceiling,
  not the wait taken | 1 | | no throttle is ever re-issued | 4 | | every failure is re-issued,
  including an ambiguous 500 | 1 |

The retry counts in `test_invoke.py` were read off a live botocore rather than assumed:
  `total_max_attempts: 1` opens one connection, `max_attempts: 1` opens two, and the untouched
  default opens five.

## What changes

`lambda <alias> run` waits the function's `Timeout` plus 30 seconds instead of 60 seconds. The
  durable `run` waits 930 seconds, the ceiling Lambda applies to a synchronous durable invoke plus
  the same margin. `run --async` waits 30 seconds, because an Event invoke waits on Lambda taking
  the event and not on anything the function does.

A concurrency throttle is re-sent with exponential backoff, up to five attempts, warning on each.
  Lambda refuses the invocation outright under `TooManyRequestsException` and
  `EC2ThrottledException` and at HTTP 429, so no execution starts and re-sending cannot duplicate
  one. Nothing else is re-sent.

A timed-out run exits 1 and says on stderr that the function is still running and was not retried,
  naming `logs`. The durable one names `executions`. Under `--async` it says instead that the event
  may or may not be queued, which is all an unacknowledged Event invoke supports.

A connect failure exits 1 saying nothing ran, rather than raising a traceback.

Each plain `run` makes one `GetFunctionConfiguration` call before invoking. A caller holding
  `Invoke` without it still works: the wait falls back to 930 seconds and a warning goes to stderr,
  so `--json` on stdout stays clean.

`botocore` is now declared in `[project.dependencies]`. Six modules import it directly, and the lock
  gains two lines — the direct edge only, since boto3 already installed it.

`deploy`, `logs`, `executions` and `history` are unaffected.

## Decisions, and what they rejected

- **The Invoke domain is its own module** — `session.py` builds a session from config. Reading a
  function's `Timeout` is the Lambda API, which is the boundary `durable.py` already keeps from
  `logs.py`. - **The config goes on a second client, not on the session** — every other call dectl
  makes is a read, where a retry costs one round trip and is wanted. - **A throttle is re-sent in
  dectl rather than by botocore** — botocore's retry config is per-client and cannot separate a
  refusal from a timeout. The safety argument is about which failures started an execution, so it is
  written where it can be read. - **The wait is read from the function, not fixed** — a constant is
  wrong in both directions. Too low re-opens the retry storm; too high hangs a three-second function
  for fifteen minutes when its socket dies rather than its handler. - **Rejected: reserved
  concurrency of 1 on the function.** It stops the overlap being destructive and leaves every caller
  still issuing duplicate invocations, including callers that are not dectl. The retry is the
  defect. - **Rejected: `max_attempts: 1`.** It normalises to `total_max_attempts: 2`, which still
  fires one duplicate invoke. `tests/test_invoke.py` pins it at two connections so the wrong key
  stays visibly wrong. - **A fake cannot verify this, so a socket does** — botocore's retry loop
  sits below the client object, so a duplicate invoke never reaches the code under test.

## Risk and rollback

A revert restores the 60-second default and the five attempts. Nothing persists past the process, so
  there is no state a revert cannot undo.

Two new failure modes. A run ends at the function's `Timeout` plus 30 seconds where it previously
  kept trying — intended, and Lambda was going to kill the function at its own `Timeout` regardless.
  And a sustained throttle now fails after five attempts where botocore's five were silent; the
  attempts are reported, so the cause is on screen rather than inferred from a delay.

## What this does not do

`deploy` keeps the session's client. `update_function_code`, `publish_version` and `update_alias`
  are separate calls with different idempotency, and none waits on a running function.

`sfn run` and `glue run` are untouched. `StartExecution` and `StartJobRun` return as soon as the
  work is queued, so no read timeout applies to either.

A `ServiceException` is never re-sent. Lambda may have reached the runtime before failing, so it
  falls in the class that may have started an execution.

There is no live-AWS test of the retry behaviour. Reproducing it would need a function that outruns
  its caller's socket, and the socket harness settles the same question deterministically and for
  free.

## The review

https://github.com/datapointchris/dectl/pull/3#pullrequestreview-5064706771 — 4 correctness, 4 break
  a rule, 1 rule proposed, 1 design. All ten fixed in `d0d819f`, except the citation in
  `iceberg.py`, which is `2503a2b`.

1. fixed — `d0d819f`. `issue_invoke` re-sends an admission refusal. The no-retry client had dropped
  the throttle retries too, and those cannot duplicate a run. 2. fixed — `d0d819f`. The wait is
  resolved after `run_async`, and `--async` gets the acknowledgement timeout with a message that
  claims no execution started. 3. fixed — `d0d819f`. The fallback warning interpolates the value it
  returns. 4. fixed — `d0d819f`. The dict-shape sentence is gone; `Config` normalises at
  construction, so a config assertion does catch the wrong key. The socket tests are described as
  what establishes the meaning in requests. 5. fixed — `d0d819f`. The refusals are built by named
  functions the tests import, so rewording moves both. `stderr_text` stays, normalising rich's soft
  wrap. 6. fixed — `d0d819f`. The Gotchas entries name the module and stop; `invoke.py` carries the
  mechanism. 7. fixed — `d0d819f`. The plain run's refusal names `logs`. 8. fixed — `2503a2b` and
  `d0d819f`. Both citations carry their constraint inline instead. 9. fixed — `d0d819f`. `botocore`
  is declared. Whether the general rule is written is not this PR's to settle. 10. fixed —
  `d0d819f`. `invoke.py` holds the Invoke domain; `session.py` builds sessions.

### Chores

- **deps**: Refresh the lock to what a fresh install resolves
  ([`e8be6ba`](https://github.com/datapointchris/dectl/commit/e8be6bab8f0aaf4d39b8d75298a2bb9af859a15c))

The lock pinned typer 0.25.1, which requires click; a uv tool install resolves 0.27.2, which vendors
  it as typer._click and requires nothing. So click sat in the dev and CI environments and in no
  environment a user has, and an import of it passed every gate before failing at startup on the
  installed tool.

click is now absent locally, so the next such import fails where it is written. Everything else
  moves with it, and the suite, mypy and ruff pass on the new resolution.

### Documentation

- **iceberg**: Carry the constraint in the docstring rather than citing it
  ([`2503a2b`](https://github.com/datapointchris/dectl/commit/2503a2b470c36f0fdf84b14faa6cf95d1600d901))

The repo is public, so a citation naming a private standards file points a reader at something they
  cannot open and discloses apparatus that is not the project's. The sentence carries the rule
  itself instead.


## v2.8.1 (2026-08-31)

### Bug Fixes

- **main**: Drop the click import that is not a declared dependency
  ([`e8c42a8`](https://github.com/datapointchris/dectl/commit/e8c42a8bcd024d03293ea66440f69313a448998c))

typer 0.27 vendors click as typer._click and no longer requires the real package, so a uv tool
  install has no click and dectl fails at import with ModuleNotFoundError before any command runs.
  The dev and CI environments resolve it transitively through another dependency, which is why every
  gate passed.

The two annotations it served have no public name: typer.Context is a subclass of the vendored
  Context, so naming it narrows a parameter the base widens.


## v2.8.0 (2026-08-31)

### Bug Fixes

- **config**: Report the validation error wherever the pipelines are missing
  ([`adcef6a`](https://github.com/datapointchris/dectl/commit/adcef6a17f8ddbb8beef2afd0d8e7244d45d6d21))

An absent config and one that fails to load both leave cfg as None, and every refusal path reported
  the first. A config with a typo in it was answered with 'no config loaded — run dectl config
  init', which refuses because the file it would write is already there.

report_config_error is now the single renderer, called by list, search, config show, config
  validate, bare dectl, and DectlGroup.get_command when the name typed was a pipeline. It carries
  the location, the message and the rejected value, which is what identifies the offending key.

config show called load_config unguarded and surfaced a pydantic traceback. require_config consults
  CONFIG_ERROR before cfg, so init is only ever suggested for a config that is genuinely absent.

Detail lines move to stderr with the rest of the refusals, keeping config show --json parseable on
  the path where it fails.

- **iceberg**: Read the gzip spelling Iceberg writes, and refuse on stderr
  ([`735663a`](https://github.com/datapointchris/dectl/commit/735663aa54feab8af140c3be8c3d11bea6947683))

The gzip check recognised only `<name>.metadata.json.gz`. Iceberg puts the codec extension before
  `.metadata.json`, which is what Java's TableMetadataParser builds and the only form pyiceberg
  decompresses; the trailing spelling is legacy. A table with write.metadata.compression-codec=gzip
  was reported as corrupt, in a message that sent the reader after a metadata file that was fine.

The test could not catch it because the S3 fake gzipped on the same predicate the code checked, so
  the two were one wrong rule written twice. The fake now encodes Iceberg's names, read off the
  writers, and the test covers both spellings.

error() wrote to stdout, so a refusal on a --json read reached a caller piping into jq as a parse
  error rather than a message. It now writes to stderr with warn(), which is the contract every read
  verb in the tool depends on. success() and info() stay on stdout, being the answer.

--limit 0 meant all on history and none on snapshots and files, and the two then printed an
  empty-state sentence that was false about a table holding four commits. One reading now, shared by
  all four list verbs, and a negative is a usage error rather than a silent off-by-one.

resolve_snapshot routed on str.isdigit(), which is true for characters int() rejects, so a
  superscript raised out of a function whose every other bad-input path exits. isdecimal() sends
  those to the ref lookup, which reports what it could not find.

format_elapsed floored to whole minutes, printing (0m) for two commits less than a minute apart —
  which on a streaming table is most of them, and diff with no arguments is exactly that comparison.
  Both duration formatters are now one in output.py, carrying seconds through days. Durable
  executions read 1d02h where they read 26h05m, which is the range that matters when a wait can run
  for months.

Two assertions could not fail and now can: the delete-files test asserted '2' in rendered output,
  which the 2026 timestamp satisfied on a table with no delete files at all, and the diff title test
  asserted a substring that survived appending the ids it forbids. Both were mutated to confirm they
  catch what they name.

Also renames sort_key to snapshot_timestamp_ms, since ordering by id would be a different answer,
  and imports datetime as a module.

### Chores

- Regenerate the shared config
  ([`ad0eb97`](https://github.com/datapointchris/dectl/commit/ad0eb977c129a75644f480d3b9106c81686b1775))

The refcheck block carried a count of active repos, which published the size of a mostly-private
  portfolio and went stale whenever one was added. The comment now states the property it was
  evidence for: the whole-tree half finds nothing on a clean tree, which is the normal state.

- Regenerate the shared config
  ([`2ffa34f`](https://github.com/datapointchris/dectl/commit/2ffa34fea41238cf43fed607ff635732cf78e7d4))

The generated templates named private tooling, so a public repo carrying them published a reference
  to it. Regenerating from the current templates replaces every one with a description of what the
  thing does.

Also drops the repo names, machine home paths and a dependency anecdote the same templates carried,
  and makes the prepare-commit-msg hook skip cleanly when the machine-local hook it calls is absent
  — a clone of a public repo has to be able to run pre-commit without it.

- Sync the generated configs to toolchain 18
  ([`7f1e024`](https://github.com/datapointchris/dectl/commit/7f1e0244979e794935074152dc5bbde127fc34e9))

Both stamped files come from the fleet's version declaration: the pre-commit config and the
  generated workflow. Nothing here is a repo decision.

Stamp 18 carries the refcheck hook at v0.6.0, a codespell exclude widened to go.mod, and — on a
  private repo — runs-on naming the self-hosted pool with the actionlint config that declares the
  label.

- Sync the generated configs to toolchain 19
  ([`38fe9f8`](https://github.com/datapointchris/dectl/commit/38fe9f87dd3b14800d837902cd069f50c7b1bd00))

Both stamped files come from the fleet's version declaration: the pre-commit config and the
  generated workflow. Nothing here is a repo decision.

Stamp 19 passes --allow-parallel-runners to golangci-lint. A repo with two Go components runs two
  Lint jobs at once, and on a single self-hosted box the second one dies on the shared cache lock
  before linting anything.

- **precommit**: Drop the commit-branding hook
  ([`6a71b12`](https://github.com/datapointchris/dectl/commit/6a71b12a2ffb97ecfcdd46ce26db361150f84089))

Claude Code suppresses its own commit and PR attribution through its attribution setting, which
  resolves an empty string to no trailer at all. A hook that strips the trailer afterwards has
  nothing left to remove.

### Documentation

- Cite the four cli-design and testing rules built from this repo
  ([`f908698`](https://github.com/datapointchris/dectl/commit/f908698a647adef6d4fa2684bedee6cfeef31a33))

The scope-is-structural grammar, the static reference command, the payload source and the
  constraint-enforcing fake are all standards that name dectl's own source files as canonical. The
  fakes' specific refusals stay, since those are the constraints this repo learned from the real
  API.

- Mark the fake-predicate guidance as proposed rather than settled
  ([`f8c9783`](https://github.com/datapointchris/dectl/commit/f8c97838fcf2ef3e1ff5ee6fd6b7b2f969087c1e))

The paragraph read as a rule, in the same voice as every ratified one around it, so a session
  reading the file cold could not tell it apart. What is settled is what FakeS3 does; whether that
  generalises to every fake is an open question.

The description stays and the generalisation is marked, with the instruction not to carry it
  elsewhere or cite it as a rule.

### Features

- **iceberg**: Add the iceberg resource type with five read verbs
  ([`0d14af2`](https://github.com/datapointchris/dectl/commit/0d14af2595302c468b833c24368e53623bf54356))

Nothing in the AWS console gives a usable view of an Iceberg table's state, so the questions asked
  during an incident — which commit changed the row count, when the file count started climbing,
  whether the table was rolled back — have no answer short of archaeology.

Adds a pipeline resource assembled from an iceberg_tables block mapping an alias to the Glue Data
  Catalog database and table that own it, with snapshots, history, files, diff and branches on each.
  Every verb takes --json.

The reads go through the table's own metadata file. Glue holds only a pointer to it, so a read is
  GetTable followed by GetObject, and the file carries the snapshots with their per-commit
  summaries, the snapshot log, the refs and the schema history. That covers every verb here without
  a query engine or a new dependency; reading a data-file listing would mean the Avro manifests
  below it, which is why files reports per-snapshot summary counters instead.

diff is the verb the resource is for. With no arguments it compares the current snapshot to its
  parent, and with one it runs from there to now, naming the commits that ran between and the
  record, file and byte deltas across them. A walk that never reaches its base reports no commits
  rather than presenting the target's whole lineage as what ran between two points.

Snapshot ids are 19-digit longs, so a snapshot is addressed by its full id, by any unique tail of
  one, or by a branch or tag; an ambiguous tail is a usage error listing the candidates.

compact, expire, orphans and rollback are deliberately absent. They are writes and the mechanism is
  unsettled: pyiceberg reaches expire and rollback and has neither compaction nor orphan removal,
  while Athena reaches all four but needs a workgroup, an output location and a per-query cost.

The test fakes refuse what the services refuse. Glue raises EntityNotFoundException for an unknown
  table and can serve one carrying neither Iceberg parameter; S3 hands back a stream rather than
  bytes and gzips any key ending .gz. Summary values are written as strings throughout, because that
  is what a real metadata file holds and an integer fixture would let a reader that never coerces
  pass.

Also corrects the README's durable-execution example, which showed a row number as a handle.
  Executions resolve by name or by a unique tail of one.

- **iceberg**: Add the iceberg resource type with five read verbs
  ([#2](https://github.com/datapointchris/dectl/pull/2),
  [`5662422`](https://github.com/datapointchris/dectl/commit/5662422b3d8cc92d2ee51e76c21ce4885b08a64f))

An Iceberg table's committed history has no usable view: the console shows a schema and nothing
  about what the table has done, so working out which commit changed a row count means archaeology.
  This adds an `iceberg` pipeline resource with five read verbs, all served from the table's own
  metadata file through Glue and S3.

## What to look at

`src/dectl/iceberg.py` is where the reading happens and the only file that needs a close read. Three
  things to check about it:

- `locate_metadata` and `read_metadata_object` are the refusal path. Every way this can be pointed
  at something it cannot read — a table absent from Glue, a table that is not Iceberg, an Iceberg
  table with no `metadata_location`, a pointer that is not an S3 URI, an object that IAM denies, a
  body that is not JSON — has to name the thing that is wrong. Pointing dectl at a plain Glue table
  is the first thing anyone does, and the error is the whole of what they get. - `summary_int` is
  the coercion every counter goes through. Iceberg writes summary values as strings, so a counter
  read raw is `'2000'`, which compares and concatenates without ever raising. - `ancestry`,
  `history_rows` and `diff_snapshots` are one idea in three places: the snapshot log and the parent
  chain answer different questions, and a rollback is only visible in the gap between them.

`src/dectl/commands/iceberg.py` is the two-level factory, the same shape as `glue.py` and
  `stepfunctions.py`. The verbs hold no logic beyond choosing between `emit_json` and a renderer.

`tests/test_iceberg.py` holds the fakes. Check that they refuse what the services refuse rather than
  replaying what they were handed.

The wiring is `src/dectl/config.py`, `src/dectl/main.py` and `src/dectl/pipeline_view.py`, and it is
  mechanical: a resource type has to appear in the pipeline loop, the `resources` summary,
  `reference`, `resource_types`, `pipeline_to_dict` and `render_pipeline`, or it exists and nothing
  can find it.

`2ffa34f` is unrelated to the resource type and is a separate read. It regenerates the shared config
  from the current templates, which drops the references to private tooling that a public repo was
  publishing.

## How it was verified

`uv run pytest` — 235 passed, 6 skipped, of which 70 are new. `uv run mypy src` clean across 22
  files, `uv run ruff check src tests` clean, `uv run bandit -c pyproject.toml -r src` silent.

Four assertions were checked to be capable of failing rather than assumed to be. Two by
  construction, two by mutating the source and watching them go red:

- `test_diff_the_wrong_way_round_reports_that_the_two_are_not_on_one_line` asserts the commit path
  is empty. It fails against a `diff_snapshots` that returns the walk unconditionally, because an
  ancestry walk that never reaches its base has collected the target's entire lineage. -
  `test_a_gzipped_metadata_file_reads` cannot pass without the decompression: `json.loads` on
  gzipped bytes raises `UnicodeDecodeError`. Narrowing `GZIP_SUFFIXES` to the trailing spelling
  alone fails the `.gz.metadata.json` case with `is not readable JSON` — the exact symptom a real
  gzipped table would have produced. -
  `test_the_diff_counter_table_title_does_not_carry_two_snapshot_ids` was run against a title
  rewritten to `f'{alias} counters {base_id} {target_id}'`, and caught it. A substring assertion on
  the alias would not have: at any width the rendered title shows one id and clips the other, so the
  fault is not fully visible in the output it is read from. - The delete-files assertion reads the
  row rather than the rendering, because `'2' in out` passes on a table with no delete files at all
  — the ISO timestamp column carries `2026`.

The rendered output of all five verbs was read against fixtures holding a four-commit table and a
  rolled-back one, at several terminal widths. Two properties that only reading the output catches
  carry tests of their own: the diff counter table is titled with the alias alone, because two
  19-digit ids wrap a table that narrow; and the delete-files column appears only when one of the
  listed commits reports a delete file.

The assembled surface was run end to end against the template config — `dectl reference`, `dectl
  PIPELINE list`, and the help at each level.

## What changes

A pipeline that declares `iceberg_tables` gains an `iceberg` command. Nothing changes for a config
  that does not.

```yaml iceberg_tables: events: database: salesdata-{env}-catalog table: events ```

Both fields carry `{env}`. The block is a `StrictModel` with a `{}` default, so every existing
  config stays valid untouched and a misspelling of the key is a loud error rather than a pipeline
  that silently has no Iceberg commands.

The five verbs are `snapshots`, `history`, `files`, `diff` and `branches`, and every one takes
  `--json`. All five are reads: this resource issues `GetTable` and `GetObject` and nothing else,
  and needs `glue:GetTable` plus `s3:GetObject` on the metadata key.

The four that list take `--limit`/`-n`, ten rows by default, and `--limit 0` means all of them on
  all four. A negative is a usage error rather than a slice that quietly drops the last row.

**Three changes reach beyond this resource.** `error()` now writes to stderr rather than stdout, so
  every refusal in the tool does — `success()` and `info()` stay on stdout, being the answer.
  Anything scraping a dectl error message off stdout stops seeing it, and anything piping a `--json`
  read into `jq` stops choking on one. `output.warning_console` is renamed `stderr_console`, since
  errors share it now.

`format_duration` is one function in `output.py` shared by every resource that shows an elapsed
  time. Durable executions read `1d02h` where they read `26h05m`, and `91d00h` where they read
  `2184h00m`. That range is the point: a durable wait can run for months, and the Iceberg diff it
  now shares a notation with spans a single commit or a quarter.

`diff` is the one worth knowing. With no arguments it compares the current snapshot to its parent.
  With one it runs from there to now, naming the commits that ran between and the record, file and
  byte deltas across them.

A snapshot is addressed by its full 19-digit id, by any unique tail of one, or by a branch or tag
  name. All-digits is read as an id and anything else as a ref, so the two namespaces never contend.
  An ambiguous tail exits 2 listing the candidates.

Three limits a reader will meet:

- `files` reports each snapshot's own summary counters — file count, total bytes, average file size
  — and there is no row-per-data-file view. - The delete-files column appears only when one of the
  listed commits reports a delete file. Its absence on a copy-on-write table is the answer, and
  `--json` carries the counters either way along with the position and equality breakdown. - A
  snapshot that has been expired is gone from the metadata file, so nothing can reach it. A ref
  still pointing at one says so rather than failing on a lookup.

The README's durable-execution example showed a row number as a handle for `logs`. Nothing resolves
  a row number; executions resolve by name or by a unique tail of one, and the example now shows
  that.

One paragraph in `CLAUDE.md` is marked *proposed, not settled*: whether a fake's predicate must
  always be written from the service rather than from the code under test. It describes what
  `FakeS3` here does and is not ratified beyond that, so it is not a rule to cite.

## Decisions, and what they rejected

- **The metadata file is read directly, through `GetTable` then `GetObject`** — rejecting pyiceberg.
  Everything these five verbs need is in that JSON, and pyiceberg parses the same bytes into
  dataclasses. It also cannot open an `s3://` table without pyarrow or s3fs, and either one more
  than doubles the install for a tool whose only dependency on AWS is boto3. - **Athena's metadata
  tables were rejected for the reads** — they answer the same questions through a billed query
  against a workgroup and an output location, which is config and latency for bytes that a
  `GetObject` already returns. It stays the candidate for the maintenance verbs, where it reaches
  things nothing else does. - **`iceberg_tables` maps an alias to `{database, table}`** — matching
  `glue_jobs`, `lambdas` and `step_functions`, so the alias is a tree node rather than an argument
  and an unknown table is a Typer usage error listing the ones that exist. - **A non-linear diff
  reports no commits at all**, rather than the walk it collected. Two snapshots on different
  branches have no path between them, and the target's whole lineage presented as "what ran between
  these" is a confident answer to a question with none. - **The snapshot handle is the id's tail,
  not a row number** — `cli-design.md` § "A UUID-keyed resource needs a short handle of its own". A
  position is a property of the listing that printed it, so it means something different under a
  different query.

## Risk and rollback

Nothing here writes to AWS. Every verb is a read, so the worst failure is a wrong or absent answer
  rather than a changed table.

A revert removes the `iceberg` command and the `iceberg_tables` model. Any config carrying that
  block then fails validation under `extra='forbid'`, which surfaces through `config validate` and
  the startup banner rather than silently — the block would have to come out of the config too.

A revert also puts every error message back on stdout and returns `format_duration` to its
  per-module spellings. Neither is recoverable by config, so a caller that had come to depend on
  reading dectl errors from stderr would have to be changed back with it.

## What this does not do

`compact`, `expire`, `orphans` and `rollback` are absent, and no stub stands in for them. All four
  are writes and the mechanism is a genuine fork rather than a detail: pyiceberg reaches `expire`
  and `rollback` through `maintenance` and `manage_snapshots` and, as of 0.11.1, has neither
  compaction nor orphan removal, while Athena reaches all four through `OPTIMIZE ... REWRITE DATA
  USING BIN_PACK` and `VACUUM` but needs a workgroup, an output location and a per-query cost.
  Choosing means taking on either a heavy dependency or new account-level config, and neither
  belongs in a change about reading.

There is no per-data-file listing, for the reason above: everything below the metadata file is Avro.

## The review

https://github.com/datapointchris/dectl/pull/2#pullrequestreview-5034132105 — 7 correctness, 4
  breaks a rule, 3 rules proposed, 1 design. All twelve findings fixed in `735663a`; the three rule
  proposals are for the standards rather than for this branch.

Correctness:

1. fixed — `735663a`. The gzip check missed `<name>.gz.metadata.json`, which is what every current
  writer produces, so a gzipped table was reported as corrupt. 2. fixed — `735663a`. The S3 fake
  gzipped on the code's own predicate, so the test passed on the one spelling the code got right.
  The fake now encodes Iceberg's names. 3. fixed — `735663a`. `error()` moved to stderr, tool-wide.
  4. fixed — `735663a`. One reading of `--limit`, shared by all four list verbs, with `min=0`
  turning a negative into a usage error. 5. fixed — `735663a`. `isdecimal()`, so a superscript is a
  ref lookup rather than a traceback. 6. fixed — `735663a`. Asserts the row's `delete_files` and
  `position_deletes`, not the render. 7. fixed — `735663a`. Asserts the ids are absent from the
  title line.

Breaks a written rule:

1. fixed — `735663a`. `import datetime as dt`. 2. fixed — `735663a`. `sort_key` is
  `snapshot_timestamp_ms`. 3. fixed — `735663a`. `branches` takes `--limit`/`-n`. 4. fixed —
  `735663a`. The three citations keep the lesson and drop the locale.

Design:

1. fixed — `735663a`. One `format_duration` in `output.py`, carrying seconds through days. The
  sub-minute case is the one that mattered: `diff` with no arguments compares the current commit to
  its parent, and on a streaming table those are usually seconds apart, where the old formatter
  printed `(0m)`.

Rules proposed, none of which changes this branch:

1. `sweep-a-cross-repo-citation-as-a-fleet-path` — the sweep behind a documentation rule does not
  recognise a `§` citation into a shared standards directory, which is the most common cross-repo
  pointer a `CLAUDE.md` carries. It returned clean here while three sat in the file. 2.
  `a-flag-means-one-thing-across-every-verb-of-a-resource` — the existing rules settle whether `0`
  may mean "all" on a `--limit` and that a verb means one thing, and neither reaches a flag
  diverging between sibling verbs of one resource, where both help rows read identically. 3.
  `a-fake-restating-the-implementations-rule-proves-nothing` — the fake rule is written against
  permissiveness, and does not reach a fake that enforces the code's constraint instead of the
  service's. That failure is worse, because it leaves a green test named for the property.

### Refactoring

- **update**: Let pyselfupdate resolve the credential
  ([`224ce32`](https://github.com/datapointchris/dectl/commit/224ce329ccd2b9441b8ef7bcd253eb99994171aa))

pyselfupdate now runs gh auth token itself, so the local helper was a second implementation of the
  library's default. GITHUB_TOKEN_COMMAND redirects or empties it and belongs to whoever runs dectl,
  which a hardcoded token_func could not offer.

Removes the shutil and subprocess imports with it. The three tests that covered the helper move to
  the library, where the behaviour now lives; what is left asserts dectl configures no credential of
  its own.

### Testing

- **main**: Assert on a help fragment no style boundary splits
  ([`5f461b5`](https://github.com/datapointchris/dectl/commit/5f461b5f1c4883234d61e5ac3c44104932af8e0e))

rich styles 'Usage:' and the command name separately, so an escape sequence sits between them
  wherever colour is on and the two words are only contiguous without it. The runner has colour on
  and this machine does not, which is the one condition the local suite cannot reproduce.


## v2.7.1 (2026-08-21)

### Bug Fixes

- **update**: Authenticate the release lookup with a gh token
  ([`d7a6ce0`](https://github.com/datapointchris/dectl/commit/d7a6ce00fa9f7ea8a7c0c79d22bb2341d8ce1e7f))

The GitHub API allows 60 unauthenticated requests an hour per IP address, shared with every other
  anonymous caller behind the same egress. Behind a corporate NAT that quota belongs to the whole
  office and is routinely spent before the first dectl command of the day, so the release lookup
  returns 403 and the update reads as broken rather than as a borrowed quota.

pyselfupdate reads $GITHUB_TOKEN and $GH_TOKEN and will not spawn a subprocess a caller did not ask
  for, so add gh as the third source. It is a token_func rather than a token because the config is
  built at import and the notify gate resolves it on every invocation, matching doit, relate and
  fleet.


## v2.7.0 (2026-08-20)

### Bug Fixes

- **durable**: Address an execution by a unique tail of its name, not a row
  ([`28c9e96`](https://github.com/datapointchris/dectl/commit/28c9e964509fd96de519cc917efe70dbc368b674))

A row number is a property of one rendering, not of the execution, so it was only valid for the
  exact query that printed it. executions narrows by --status, --qualifier and --all-versions while
  history and logs always resolved against the live version unfiltered, so the same digit named a
  different execution and resolved silently. cli-design.md 'A UUID-keyed resource needs a short
  handle of its own' prescribes the tail of the name: it survives a deploy --publish, cannot be
  shadowed by a listing, and lands in the existing name path. Ambiguity errors listing the
  candidates.

hide_keys loses its default. Suppression reached render_merged and the non-durable logs, neither of
  which asked for it and neither of which has --context, so glue and monitor silently lost requestId
  and logger. requestId leaves the suppressed set outright: one execution spans many invocations, so
  it varies across a scoped tail and says which one spoke. tail_lambda_logs now adds the execution
  ARN itself, since its filter pattern is the fact that decides whether a tail is scoped.

operation_tag returns the keys it folded, so attempt without an operationName, or a non-integer
  attempt, prints as an ordinary field instead of vanishing where --context could not reach it.

The fake now filters on Statuses, which is what made the wrong-listing case expressible.

- **durable**: Let --context reach a first attempt and bound the tail search
  ([`45f9499`](https://github.com/datapointchris/dectl/commit/45f9499da491890bfac2926ef37999bc5a5a6329))

operation_tag claimed 'attempt' for every integer value while rendering it only past the first, and
  the claim is read before hide_keys, so attempt=1 was folded out of the tag and unreachable by any
  flag. It now claims only what it rendered, so a first attempt prints as an ordinary field and the
  three neighbouring values behave alike.

The tail search window and executions --limit were independent numbers, so past fifty executions a
  tail either failed to resolve or resolved to one of two candidates because the other fell outside
  the window and the ambiguity was never seen. --limit is capped at SUFFIX_SEARCH_LIMIT, which makes
  'anything the listing prints, a tail can reach' true by construction.

The not-found error named --all-versions, which rescans the versions the failed lookup already
  covered. It now says how far the search reached and that an older execution needs its full name or
  its ARN.

Whether to fold the execution ARN is fold_scope_fields rather than a non-empty hide_keys, which was
  carrying two unrelated meanings.

### Build System

- **precommit**: Resync to forge toolchain 14
  ([`6a9d42a`](https://github.com/datapointchris/dectl/commit/6a9d42a94a6a484f5c87b83bc6704dec32b4ae2e))

### Chores

- **pyproject**: Raise assertion verbosity instead of test verbosity
  ([`9604160`](https://github.com/datapointchris/dectl/commit/9604160155081fa3032cd48b96845a7f39c2d75a))

A failing assertion truncated its diff and printed "use -vv to show", so the reader re-ran the whole
  suite to see it. addopts = "-vv" answered that by raising test-list verbosity as well, which is a
  different question: a green run printed a line per test and said nothing. verbosity_assertions
  raises only the half that was wanted.

Written by the forge pyproject die.

### Continuous Integration

- Regenerate validate.yml at toolchain 16
  ([`144650f`](https://github.com/datapointchris/dectl/commit/144650fea350f646b2ab44effb7a1b7c410368b2))

Catches this repo up with the version manifest: StyLua pinned to a release rather than latest, a
  reworded bats discovery note, and double quotes in the node block. Only the blocks this repo
  declares are affected.

Triggers and job structure are unchanged.

### Documentation

- Cite the standards without a machine path
  ([`3968e7d`](https://github.com/datapointchris/dectl/commit/3968e7d0c771c6d334147f44874f9e7a3199d2b4))

The citation carried an absolute path from one machine's layout. What a reader needs is the file and
  the section, and those do not move.

### Features

- **durable**: Fold repeated context out of logs and number execution rows
  ([`bce5797`](https://github.com/datapointchris/dectl/commit/bce57971415fea86a7223c4036c2cfbc67d5e22c))

Scoped to one execution, every record carries the same executionArn, operationId, parentId and
  requestId. Expanded as fields they cost eight lines per message, so six messages filled a screen
  and the messages themselves were the minority of it. render_event now takes hide_keys and folds
  operationName/attempt into a tag beside the level; --context keeps the full record, and --all
  keeps the ARN because across executions it varies. Suppression is a named list, so a handler's own
  extra= survives.

Without run --name an execution is named by a UUID, leaving nothing in the executions table a person
  can retype. The table numbers its rows and history/logs accept that number, trying a name first so
  an execution named 3 still wins its own digit. --json carries the same index.

- **durable**: Fold repeated context out of logs and resolve by name tail
  ([#1](https://github.com/datapointchris/dectl/pull/1),
  [`674c223`](https://github.com/datapointchris/dectl/commit/674c223df8136334a03b4935e2be331407a0bb7b))

Reading one durable execution's logs meant scrolling past the same opaque ids on every record, and
  reaching a different execution meant transcribing a UUID. This folds the ids into a tag and makes
  any unique tail of an execution's name resolve to it.

## What to look at

`bce5797`, then `28c9e96`, then `45f9499`. The later two rework the first after review, so read them
  as the design and `bce5797` only for what it moved.

- `src/dectl/durable.py` — `resolve_execution` tries an ARN, then the exact name, then the same name
  across a version sweep, and only then a tail. Check that ordering is what keeps an execution whose
  name ends another's resolving to itself, and that `execution_by_suffix` errors with candidates
  rather than picking one. - `src/dectl/logs.py` — `render_event` and `tail_lambda_logs` take
  `hide_keys` with no default. Check every caller passes one deliberately, and that
  `tail_lambda_logs` is the only place the execution ARN is added. - `src/dectl/logs.py` —
  `operation_tag` returns the keys it *rendered*, never one it merely read. Check that a first
  attempt, a non-integer attempt, and an attempt with no operation all still print, since the claim
  is consulted before `hide_keys` and nothing reaches past it. - `tests/test_durable.py` — the fake
  now filters on `Statuses`. That filter is what made the wrong-listing case expressible at all.

## How it was verified

`uv run pytest -q` — 164 passed, 6 skipped. `uv run ruff check .`, `uv run ruff format --check .`
  (38 files), `uv run mypy . --install-types --non-interactive` (38 source files) all clean.

The two rendering regressions the reviews measured were re-run against the fix. A record with
  `requestId`, `logger` and a caller's own field through `render_merged` returns all three.
  `{'level':'WARN','message':'retrying upload','attempt':4}` returns `attempt: 4` where it
  previously vanished.

`test_glue_and_monitor_records_keep_every_field` points at `render_merged`, the caller that violated
  the invariant, rather than at `render_event` with an obeying argument.
  `test_an_ambiguous_tail_is_a_usage_error_naming_the_candidates` asserts exit code 2 and both
  candidate names.

## What changes

`logs` on a durable function hides two fields — `operationId` and `parentId` — plus the execution
  ARN when the tail is scoped to one execution. The operation appears as a tag beside the level:
  `wait_for_files`, or `wait_for_files.3` on a retry. `attempt` is folded only on a retry, where it
  shows as `.4`; a first attempt prints as an ordinary field. `--context` restores everything.

`requestId` and `logger` are not hidden. One execution spans many invocations, so `requestId` varies
  across a scoped tail and says which invocation spoke.

`glue logs`, `monitor` and the non-durable `lambda logs` are unchanged. They pass an empty
  suppression set explicitly.

`history` and `logs` accept any unique tail of an execution's name alongside the full name and the
  ARN. An ambiguous tail exits 2 listing the candidates. The `executions` table folds long names
  instead of truncating them, so the tail stays visible.

`executions --json` is unchanged from `main`.

## Decisions, and what they rejected

- **The tail of the name, not a row number** — `cli-design.md` § "A UUID-keyed resource needs a
  short handle of its own" prescribes a server-assigned integer, which AWS does not give, and names
  suffix resolution as the fallback. A row number shipped in `bce5797` and is reverted here: a
  position is only valid for the query that produced it, so `--status`, `--qualifier` and
  `--all-versions` each made the same digit name a different execution with nothing on screen to say
  so. - **The tail rather than the head** — a UUID front-loads its timestamp, so a prefix of one
  carries almost no entropy. - **Ambiguity errors rather than picking the newest** — resolving
  silently to something the caller did not name is the failure the handle exists to prevent. - **No
  default for `hide_keys`** — a suppressed field leaves a record that still reads as complete, so a
  wrong default is invisible. Rejected keeping a default scoped to durable callers: the same
  constant was already reaching three callers that had never asked for it. - **`tail_lambda_logs`
  adds the execution ARN, gated on `fold_scope_fields`** — a non-empty filter pattern is what makes
  a tail scoped, and that fact already lives there. Rejected a scoped and unscoped pair at the call
  site, which differed by one element two lines apart. Rejected inferring "folding was wanted" from
  a non-empty `hide_keys`, because emptiness already means "nothing to suppress" for glue and the
  non-durable `logs`. - **One number for the tail window and the `--limit` cap** — a tail is typed
  off a listing, so letting them drift means a row the table prints that the resolver cannot reach,
  which surfaces as a silent wrong answer rather than an error.

## Risk and rollback

Nothing deploys. `45f9499` reverts cleanly on its own. `28c9e96` does not — reverting it restores
  the row-number handle along with the rendering regressions it fixed.

## What this does not do

No `choose` verb. `sfn` and `glue` rendering is untouched. An execution older than
  `SUFFIX_SEARCH_LIMIT` on its version cannot be reached by a tail and needs its full name or ARN;
  `executions --limit` is capped at the same number so nothing the listing shows falls outside it.

## The review

Three reviews, converging on the same structural finding.

[#pullrequestreview-4985245757](https://github.com/datapointchris/dectl/pull/1#pullrequestreview-4985245757)
  — 5 correctness, 5 breaks, 1 rule proposed, 2 design.

[#pullrequestreview-4985245991](https://github.com/datapointchris/dectl/pull/1#pullrequestreview-4985245991)
  — 6 correctness, 5 breaks, 1 rule proposed, 2 design.

[#pullrequestreview-4985267929](https://github.com/datapointchris/dectl/pull/1#pullrequestreview-4985267929)
  — 4 correctness, 7 breaks, 0 rules proposed, 2 design.

1. fixed — the row-number handle is replaced by name-suffix resolution, which dissolves the
  wrong-listing resolution, the post-`deploy --publish` shadowing, the exit-code and `isdigit`
  findings, the triple computation of the index, the unreachable `execution_to_dict` default, and
  the one-concept-four-words finding. `28c9e96` 2. fixed — `hide_keys` loses its default;
  `render_merged` and the non-durable `logs` pass `frozenset()`. `28c9e96` 3. fixed — `requestId`
  leaves the suppressed set. It varies across a scoped tail because an execution spans many
  invocations. `28c9e96` 4. fixed — `operation_tag` returns the keys it consumed, so `attempt`
  without an `operationName` and a non-integer `attempt` both print. `28c9e96` 5. fixed — the fake
  filters on `Statuses`. `28c9e96` 6. fixed — the test that passed with the `#` column deleted is
  gone with the column; the table is now asserted on the tail staying visible. `28c9e96` 7. fixed —
  the prose names `DURABLE_OPERATION_ID_KEYS` and stops enumerating it; `--context` help states the
  rule rather than the members. `28c9e96` 8. accepted, not done — splitting `bce5797` into two
  commits so the rendering and the handle revert apart. Doing it now means rewriting a commit that
  three reviews are anchored to, and `28c9e96` already isolates the handle change on its own.

### Second round, on `28c9e96`

[#pullrequestreview-4985949874](https://github.com/datapointchris/dectl/pull/1#pullrequestreview-4985949874)
  — 2 correctness, 2 breaks, 0 rules proposed, 1 design. Nineteen of the first round's 24 findings
  verified closed.

9. fixed — `operation_tag` claims only the keys it rendered, so `attempt: 1` prints as a field and
  `--context` reaches it. `45f9499` 10. fixed — `executions --limit` is capped at
  `SUFFIX_SEARCH_LIMIT`, so every row the listing prints is inside the window a tail resolves
  within. The silent-ambiguity half is closed by construction. `45f9499` 11. fixed — the not-found
  error says how far the search reached and that an older execution needs its full name or ARN,
  instead of naming `--all-versions`, which rescans the same span. `45f9499` 12. fixed —
  `test_operation_tag_only_claims_the_keys_it_showed` now asserts the `attempt: 1` case its name
  promised, and `test_context_reaches_a_first_attempt` renders it. `45f9499` 13. fixed — folding is
  `fold_scope_fields`, a parameter, rather than inferred from a non-empty `hide_keys` that was
  carrying two meanings. `45f9499`


## v2.6.0 (2026-08-08)

### Continuous Integration

- Drop the push trigger the release run already covers
  ([`cb7aa58`](https://github.com/datapointchris/dectl/commit/cb7aa58982298de13f00c9eeb144e2253ba90f0f))

release.yml fires on push to main and calls validate.yml, so emitting push here ran every job twice
  for one commit. workflow_call still covers main; only the duplicate goes away.

Generated by forge v5.1.0, which derives this from release.yml rather than a flag, so it cannot
  drift back.

### Features

- Never prompt a caller that cannot answer
  ([`b1141de`](https://github.com/datapointchris/dectl/commit/b1141deee5b34539d02804e7fb65dc1d1a1eb400))

The Glue job-definition update went straight to typer.confirm, so a Jenkins step or a cron run
  either consumed stdin meant for something else or waited on a stdin that never closes — no output,
  no exit code.

confirm_or_exit gates on can_prompt() and otherwise fails naming --yes. --no-input forces that path
  from a terminal, per the interactivity rule in ~/dev/standards/cli-design.md.

The gate lives in its own module holding per-invocation state set from the root callback, the shape
  env.py already uses for --env, so a verb deep in the tree can ask without threading the flag
  through every signature.


## v2.5.1 (2026-08-07)

### Bug Fixes

- Release the one-stroke shortcut form
  ([`3832a2a`](https://github.com/datapointchris/dectl/commit/3832a2abab957363521256a22747f15a5976df84))

The dependency bump to pyclisteno 0.8.0 went in as `build:`, which semantic-release does not
  release, so the installed dectl kept writing the spaced index and the shell hint kept offering
  `dectl ex g s r`. The change is user-visible and needed a releasing type.

### Build System

- Take the one-stroke short form
  ([`8cab6d3`](https://github.com/datapointchris/dectl/commit/8cab6d3cfe918a9439ee5878c9ed0c15061cad55))

0.8.0 runs the per-level prefixes together, so the shortcut is `dectl exgsr` rather than `dectl ex g
  s r`. Long form and real command names are unaffected — `dectl env` still reaches env.

### Documentation

- Describe the shortcut grammar clisteno adds
  ([`116f239`](https://github.com/datapointchris/dectl/commit/116f239178e845cc806d6785c363167bc06e341d))

The compressed form is part of dectl's surface now, and the two things a future reader would
  otherwise have to rediscover: attach() must be the last line of main.py, and a sequence that is
  also a command name is withheld.


## v2.5.0 (2026-08-07)

### Build System

- Take the clisteno index fix
  ([`e963a82`](https://github.com/datapointchris/dectl/commit/e963a827acb416a706aad3fef55bf013de985c60))

0.6.1 strips rich tags from the flat index, so the hint rows no longer read
  "[bold]source-copy[/bold]". Help is unaffected either way — rich rendered those tags correctly
  there.

- Take the clisteno index key fix
  ([`669c8ab`](https://github.com/datapointchris/dectl/commit/669c8abb123cf2c5bf7525a9e40227c228e4f37c))

0.6.2 keys the flat index by the typed sequence rather than the node's own prefix, which is what the
  shell hint needs to look anything up — dectl's index had eleven colliding keys before it.

### Features

- Accept the short form of any command
  ([`7ebf3d1`](https://github.com/datapointchris/dectl/commit/7ebf3d116cfa4b5e7c8fb06fb4c2cd9aa573f200))

Stage 3, the last of adopting clisteno. `dectl ex g s r` now runs `dectl example-pipeline glue
  source-copy run`, which is what the help hint has been offering since stage 1 — until now the tool
  rejected its own advice.

Expansion declines wherever it is not certain: an unknown token, a retired sequence, or anything
  after a leading option is passed through for dectl to answer itself, and a real command name
  expands to itself. Verified all three against the live config.


## v2.4.0 (2026-08-07)

### Features

- Show each command's short form in help
  ([`9ecc7e8`](https://github.com/datapointchris/dectl/commit/9ecc7e8254780e491d1cce56e72ba301f4909d21))

Stage 1 of adopting clisteno. Every row in every listing now carries the prefix that reaches it, so
  reading help trains the fast path instead of only answering the question that opened it.

reference (r) Print the full command grammar, independent of config. config (c) Manage dectl
  configuration at ~/.config/dectl/...

source-copy (s) Glue job source-copy → my-{env}-source-copy-job

Render-time only: nothing about parsing, argument handling or exit codes changes, and the rich
  markup in the pipeline and alias summaries renders as it always did. dectl example-pipeline glue
  source-copy run is now dectl ex g s r.


## v2.3.0 (2026-08-07)

### Chores

- **lint**: Disable SC1091/SC1090 from the forge toolchain
  ([`324c94b`](https://github.com/datapointchris/dectl/commit/324c94bd9167f4bcf1714f2a707b8637d1406825))

### Features

- Publish the command grammar through clisteno
  ([`8bde711`](https://github.com/datapointchris/dectl/commit/8bde71119a7df19320119ba1a19e4df7835afc14))

Stage 0 of adopting clisteno: enrollment only. The grammar dump and its flat index appear under
  ~/.cache/clisteno, and nothing else changes — every node's --help is byte-identical with and
  without the call, checked across the whole tree rather than assumed.

attach(app) is the last line of main.py rather than sitting after the pipeline loop, because the
  global commands are registered below it and a walk that ran earlier would publish a grammar
  missing them. It cannot be lazier either: the tree does not exist until config is read, which is
  what makes this one call instead of a decorator per command.

The {env} token survives into the dump unsubstituted, so a cached grammar never teaches a name that
  only holds under one --env.

Teaching, ghost text and the resolver are the later stages and are deliberately not started here.


## v2.2.2 (2026-08-04)

### Bug Fixes

- Report the update in the verb that ran it
  ([`aacfa18`](https://github.com/datapointchris/dectl/commit/aacfa187b6e793aa27777cb4e0d753c8d4074c14))

pyselfupdate 0.2.2 says "updated" and "update failed" where it used to say "upgraded" and "upgrade
  failed". The command is `update`; one command, one vocabulary.

### Chores

- **toolchain**: Adopt the generated configs and CI
  ([`18368cb`](https://github.com/datapointchris/dectl/commit/18368cb1e37327cde143ef368b94f3a9c41d364c))

Brings the repo onto forge toolchain manifest 11.

bandit, refurb and pyupgrade drop out: pyupgrade is ruff's UP rules, already selected, and the other
  two are the manifest's deliberate narrowing to the rule set every repo actually runs.

### Documentation

- Flush dormant markdownlint violations
  ([`46b4db2`](https://github.com/datapointchris/dectl/commit/46b4db237659dc47c66a05dd4a5254e3b25582e9))

markdownlint only runs on the files a commit touches, so unmodified docs accumulate violations
  invisibly. The toolchain sync bumps markdownlint to v0.47, which added MD060, and runs --all-files
  — surfacing every one of them at once, in the middle of an unrelated change.

Table separators are normalized to the compact `| --- |` style MD060 expects, which --fix cannot
  repair; everything else is markdownlint --fix.

- Stop normalizing the generated CHANGELOG
  ([`477cd47`](https://github.com/datapointchris/dectl/commit/477cd474a08ba8dee1000daeac3f4103bb956621))

semantic-release regenerates CHANGELOG.md on every release, so a markdownlint fix there is undone on
  the next one and comes back as a conflict when a local commit rebases onto the release.


## v2.2.1 (2026-07-31)

### Bug Fixes

- **ci**: Run ruff and pytest without depending on repo dev deps
  ([`1ba1eeb`](https://github.com/datapointchris/dectl/commit/1ba1eeb1d51be8d867615f531ca50849863dde73))

`uv run ruff` resolved ruff from the repo's own dependencies, so a repo that treats ruff as a fleet
  tool rather than a project dependency failed to spawn the binary instead of linting. ruff now runs
  through uvx at the version its pre-commit hook pins; pytest is supplied with --with so a real test
  suite is never silently skipped; and mypy's guard tests for the dependency by import, since the
  [tool.mypy] section it used to look for is now in every repo.

Regenerated by `forge dies run maintenance/sync-ci.sh`.

### Chores

- Drop the gh token lookup now that the repo is public
  ([`115f0b1`](https://github.com/datapointchris/dectl/commit/115f0b1cb2982b53b9f81b1463c4090338eb1a44))

The release lookup 404'd without a credential while dectl was private, so UPDATE_CONFIG carried a
  token_func that shelled out to `gh auth token`. A public repo resolves its latest release
  unauthenticated, so both the helper and the gh dependency for `dectl update` go away.

- **config**: Record the keys the pyproject sync owns
  ([`1d153f9`](https://github.com/datapointchris/dectl/commit/1d153f9a8591a1adf237dfa752bfca72f3b238c3))

forge now writes [tool.forge] managed, listing the exact keys the standard sets. Deletion on a later
  sync is scoped to that record, so dropping a key from the template retracts it here without having
  to guess which settings belong to this project.

Purely additive: nothing else in this file changed.

### Documentation

- Repoint changelog links at the rewritten commits
  ([`c38c566`](https://github.com/datapointchris/dectl/commit/c38c566a3b0861a80d3031cde9284ea5440ef94a))

Every SHA changed when the history was rewritten, so the links generated by semantic-release pointed
  at commits that no longer exist.


## v2.2.0 (2026-07-31)

### Features

- Update from GitHub releases instead of local source
  ([`d5bc350`](https://github.com/datapointchris/dectl/commit/d5bc350ac694e51b455e2b29f550cb441b245a7f))

dectl update reinstalled from a hardcoded ~/tools/dectl checkout, which was right while the tool
  only existed on the dev machine and wrong everywhere else: no source there means no update, and a
  dirty tree installs itself.

Adopt pyselfupdate like the rest of the fleet — update installs the latest GitHub release over the
  running one, --check reports without installing, and the root callback runs the once-a-day notice.
  The repo is private, so a token_func hands pyselfupdate a gh credential for the release lookup.


## v2.1.2 (2026-07-30)

### Bug Fixes

- Bound the Glue log scan to the run's own start time
  ([`f9be5f4`](https://github.com/datapointchris/dectl/commit/f9be5f4969e8359dede30491141371f5e297be2e))

Tailing a Glue run printed nothing for several minutes, then everything at once. Filtering the two
  shared groups by stream-name prefix isolates the run's streams but does not bound the scan:
  filter_log_events pages forward from startTime, and unset that is the start of the group's
  retention. Both Glue groups hold every Python Shell run in the account, so the tailer was paging
  through months of unrelated logs before reaching this run's, and it drained every page before
  rendering a line.

The run's StartedOn is the natural lower bound, so run_log_start reads it and the tailer now
  requires it. A missing log group is also reported rather than silently swallowed, since "printed
  nothing" and "nowhere to print" are different diagnoses.

Verified against a real Python Shell run: both groups readable in under a second, where before the
  same assertions would have waited minutes.

### Chores

- **config**: Adopt the standard pyright section
  ([`ed08e2e`](https://github.com/datapointchris/dectl/commit/ed08e2e07cad3a9df9ec0a80232a3ba3fd9df693))

Synced from forge pyproject template. With no [tool.pyright] section the editor LSP settings
  applied, and their ignore = ["*"] suppressed every diagnostic. A config file takes precedence over
  those settings, so basedpyright now reports against the same "standard" mode as the rest of the
  portfolio instead of reporting nothing.


## v2.1.1 (2026-07-30)

### Bug Fixes

- Resolve the alias to a version before listing executions
  ([`adf6df6`](https://github.com/datapointchris/dectl/commit/adf6df69ddf14a6d83b6735f17bc2ac232bc70f0))

Listing durable executions with the live alias as the qualifier fails with "cannot filter durable
  executions by alias". Lambda resolves an alias to a version number when an execution starts, so
  the alias name never appears in a durable execution ARN and there is nothing for the list API to
  match. The API reference says Qualifier takes "the function version or alias"; for
  ListDurableExecutionsByFunction that is wrong.

Invoking and listing therefore need different qualifiers, so qualifier_for splits: invoke_qualifier
  still sends the alias, which is correct and what triggers do, while listing_qualifier resolves it
  through GetAlias.

Resolving pins the listing to whichever version the alias points at now, so deploy --publish would
  silently drop every run from before it. The resolved version is shown alongside the alias, each
  execution's version is a column, and --all-versions merges recent published versions newest-first,
  naming the ones it scanned. Name lookups for history and logs fall back to that sweep
  automatically, since which version ran a given execution is not something you can be expected to
  know.


## v2.1.0 (2026-07-30)

### Bug Fixes

- Stream Glue run logs the moment they exist
  ([`32879dc`](https://github.com/datapointchris/dectl/commit/32879dca7e93b84bce84975cee2e67cd43cb63f6))

Tailing a Glue run waited for its CloudWatch streams to be created before reading anything:
  describe_log_streams on a 5-second poll for the output stream, then sequentially for the error
  stream. A job that never writes to stderr has no error stream to find, so that second wait burned
  its full 120-second timeout in silence while the output the console was already showing went
  unprinted.

Pinning the stream list at start-up also meant a traceback landing in an error stream created later
  in the run was never displayed at all.

Both groups are now filtered by stream-name prefix (the run id) instead, so an empty group costs one
  empty response rather than blocking the other, and a stream appearing mid-run is picked up on the
  next poll. All three tailers share one LogGroupCursor, which also gains the scoping and
  time-bounding a per-execution tail needs.

Following now ends when the run reaches a terminal state, draining a few passes past it since
  CloudWatch ingestion lags the run's end, and exits non-zero when the run did not succeed.

### Features

- Add durable execution verbs for durable Lambda functions
  ([`c315d70`](https://github.com/datapointchris/dectl/commit/c315d702cef643b14736d9b15ca0af3412bcd533))

A durable function's unit of work is the execution, not the invocation: one execution checkpoints
  across many invocations and can suspend for up to a year between them. None of the
  invocation-shaped views answer "did this run succeed" — Invocations counts replays, and a log
  stream holds interleaved fragments of whichever executions that environment served.

A lambda flagged durable: true in config therefore swaps run and logs for an execution-scoped set:

executions which ones succeeded or failed, with elapsed time history [EXECUTION] its steps, waits,
  retries and failures logs [EXECUTION] its own logger output, filtered by execution ARN

history and logs are complements and mirror the two console tabs: history is the checkpoint log
  Lambda replays from, logs is what the function printed while doing it. The SDK logger stamps the
  execution ARN onto every record, which is what both the console and this filter on.

run is now qualified with the live alias, falling back to $LATEST — Lambda rejects an unqualified
  invoke of a durable function, since an execution is pinned to the version it starts on. It also
  gains --async, lifting the 15-minute synchronous cap, and --name for an idempotent start that
  doubles as the handle for history and logs.


## v2.0.0 (2026-07-30)

### Features

- Diff and confirm glue job definition changes
  ([`140e61d`](https://github.com/datapointchris/dectl/commit/140e61d3640a6c8b079c57c047dbdeb5adf2b877))

dectl and Terraform both write the Glue job definition. Terraform owns it once a pipeline is
  established, but dectl keeps write access because that is the point before Terraform exists: set
  arguments and deploy from the shell instead of commit -> Jenkins -> console. Nothing marked the
  seam, so every deploy silently reasserted a config Terraform may have moved past.

deploy now diffs its computed update against the live definition, skips UpdateJob entirely when
  nothing differs, and otherwise renders a field-level table and confirms. --plan shows it and
  exits; --yes skips the prompt for the pre-Terraform loop. Removals are reported too, since a
  detached connection is invisible in a diff that only walks the new definition.

BREAKING CHANGE: connections is authoritative rather than additive. The union could only add, so a
  stale entry silently reattached a Terraform-renamed connection on every deploy and could not be
  removed from config. Absent means unmanaged, [] detaches all.

Also adds max_capacity, rejected on worker-based Spark jobs since UpdateJob will not accept it
  alongside WorkerType.

### Breaking Changes

- Connections is authoritative rather than additive. The union could only add, so a stale entry
  silently reattached a Terraform-renamed connection on every deploy and could not be removed from
  config. Absent means unmanaged, [] detaches all.


## v1.1.1 (2026-07-30)

### Bug Fixes

- Label only the Glue error stream, align expansions
  ([`f363d27`](https://github.com/datapointchris/dectl/commit/f363d27b5d9a5911cab7d3db4b6306d5fe59b25f))

Every line carried out or err, but out is a constant on a well-configured job (one stdout handler)
  so it spent width carrying no information. Worse, render_event prefixed only the header line,
  leaving expanded JSON fields and tracebacks orphaned at column 0 four columns left of the line
  they belong to.

stdout is now bare and err marks the exceptional stream — a traceback, the warnings module, a
  library writing direct. Continuation lines indent by the prefix's visible width (markup tags
  stripped) so a prefixed record reads as one block. monitor's per-resource prefixes are untouched:
  there many sources interleave, so every line needs its source.


## v1.1.0 (2026-07-28)

### Chores

- Add .planning to gitignore
  ([`193a7b3`](https://github.com/datapointchris/dectl/commit/193a7b31dfdd377e653157898d92f6231d304dbf))

### Continuous Integration

- Add generated validate.yml and gate release on it
  ([`3f754c6`](https://github.com/datapointchris/dectl/commit/3f754c6ac3eff23dcd50b007bf4bd25f721128eb))

Release triggered on push to main with no validation at all, so it published whatever was on main.
  Adds the forge-generated CI block (ruff check, ruff format, mypy, pytest) and makes release depend
  on it.

Verified locally before wiring the gate: all four checks pass.

- Regenerate validate.yml at toolchain 6
  ([`5e44601`](https://github.com/datapointchris/dectl/commit/5e44601f4aae8aa4a0c9566618327e71d16721f2))

Stamp only — the python block is unchanged. Toolchain 6 adds the pinned release-binary mechanism and
  the shell CI block.

### Features

- Warn when an explicit --env substitutes nothing
  ([`ae15f5a`](https://github.com/datapointchris/dectl/commit/ae15f5ab8a41c0e7f8aa5b0897e9cfcafcc15a8d))

Substitution is a literal {env} replacement, so a config that hardcodes its environment
  (salesdata-dev-ds-etl) ignores --env/DECTL_ENV entirely and acts on the wrong environment while
  still succeeding. Nothing surfaced that, so the command looked like it worked.

warn_if_environment_had_no_effect fires once per invocation when the env came from an explicit
  source (--env or DECTL_ENV, never a config default, since a config without placeholders is a
  legitimate single-env setup) and the resource carries no {env} token at all. Wired into every path
  that resolves names: render_env_model, s3's bare-string resolved_bucket and export, monitor, and
  both pipeline_view renderers.

The warning goes to stderr via the new output.warn, so --json output piped to jq and an eval'd `s3
  export` stay clean.


## v1.0.0 (2026-07-23)

### Chores

- **pre-commit**: Restrict hooks to pre-commit stage
  ([`65f76d7`](https://github.com/datapointchris/dectl/commit/65f76d79aadc0f000ee2d3c18c178ec867490424))

Add default_stages: [pre-commit] so hooks without an explicit stages: run only at the pre-commit
  stage. Without it, unrestricted hooks (ruff, codespell, bandit, etc.) also ran at the
  prepare-commit-msg and commit-msg stages, firing multiple times per commit.

### Documentation

- Fix install command and document release model
  ([`545c31b`](https://github.com/datapointchris/dectl/commit/545c31b6c44f42dc2aba48408dec044c13cd1639))

The documented 'uv tool install ...@latest' failed because releases are semver tags with no moving
  'latest' ref. Point install at the default branch (always the latest release under
  python-semantic-release) and document version pinning and the local 'dectl update' path.

### Features

- Add --json to read commands and unify pipeline rendering
  ([`c734463`](https://github.com/datapointchris/dectl/commit/c7344638d16cb0258884dc96f8d02805f5d4af9e))

Add an emit_json output helper (bare print, no rich markup, so piped output stays clean for jq) and
  a stable JSON shape for pipeline listings. Wire --json onto 'dectl list', 'PIPELINE list', 'config
  show', and 'search'. Extract the duplicated pipeline-printing from main.py and config_cmd.py into
  a shared pipeline_view module (importable by both without the main<->config_cmd import cycle).

- Verb-last CLI grammar and unified verbs
  ([`76436aa`](https://github.com/datapointchris/dectl/commit/76436aabf25a5be4da77556b2b3231b8d667b836))

Restructure the command surface to PIPELINE RESOURCE ALIAS VERB, with the verb last so a deploy ->
  run -> logs loop on one resource changes only the trailing word. The alias becomes a
  config-assembled sub-app and each verb closes over its resolved config; {env} is substituted at
  call time. One rule governs the surface: aliased acts on one thing, unaliased on the set.

- Unify verbs: lambda invoke -> run, sfn start -> run, sfn watch -> logs, sfn list -> runs; add glue
  runs. deploy always means "update artifact". - --follow defaults off everywhere (was forced-on for
  glue/lambda logs, glue run, sfn watch); streaming is now an explicit opt-in. - lambda/sfn run take
  payloads via --payload-file PATH or - (stdin). - s3: per-bucket mount/unmount/uri sub-apps; export
  stays set-level with --prefix; new uri prints a bare s3:// for command substitution. - Pipeline
  Jenkins deploy -> release, with --plan (was --dry-run). - Config: lambda alias -> live_alias (no
  back-compat; extra=forbid rejects the old key loudly). buckets docs say alias, not shortname. -
  dectl reference prints the full grammar, config-independent. - Progressive discovery: no-args help
  at every level, each alias node doubles as an info panel, examples-first, instance/set help
  panels.

No confirmation prompts or --yes: dectl is used at the operator's own peril.

BREAKING CHANGE: command grammar is now PIPELINE RESOURCE ALIAS VERB (verb last). Renamed verbs
  (invoke/start/watch/sfn list -> run/run/logs/runs), pipeline deploy -> release (--dry-run ->
  --plan), --follow now defaults to false, inline positional JSON payloads replaced by
  --payload-file, and the lambda config field alias -> live_alias. No deprecation aliases.

### Breaking Changes

- Command grammar is now PIPELINE RESOURCE ALIAS VERB (verb last). Renamed verbs
  (invoke/start/watch/sfn list -> run/run/logs/runs), pipeline deploy -> release (--dry-run ->
  --plan), --follow now defaults to false, inline positional JSON payloads replaced by
  --payload-file, and the lambda config field alias -> live_alias. No deprecation aliases.


## v0.6.1 (2026-07-15)

### Bug Fixes

- **s3**: Mount buckets under ~/buckets instead of cache dir
  ([`467c3ab`](https://github.com/datapointchris/dectl/commit/467c3abc4af17b6e35ae5e6d838b50cdfe100797))

The old ~/.cache/dectl/mounts/PIPELINE/SHORTNAME path was buried and impractical to cd into. Mount
  at ~/buckets/PIPELINE/SHORTNAME instead, keeping the pipeline segment so buckets sharing a
  shortname across pipelines don't collide.


## v0.6.0 (2026-07-15)

### Features

- **config**: Add example, edit, validate, path
  ([`0d4af51`](https://github.com/datapointchris/dectl/commit/0d4af51835c4b0b3defa176a91859d08bee2f602))

Add four config-management commands to the always-present config app:

- example: print the full template config (one of every option) to stdout, syntax-highlighted on a
  TTY and plain when piped, for side-by-side reference while editing the real config in another
  pane. - edit: open the config in $VISUAL then $EDITOR (no hardcoded editor), shlex-split so args
  survive, binary resolved via shutil.which; seeds from the template if none exists. Foreground, so
  terminal editors block and GUI editors follow their own --wait semantics. - validate: parse the
  file and report the exact failing config path. - path: bare-print the config path for shell
  substitution.

Viewing is done in-process via rich.syntax.Syntax rather than shelling out to a pager, so there is
  no PATH lookup or $PAGER parsing and it works everywhere.

Models now inherit StrictModel (extra='forbid'), so an unknown key is a loud validate error instead
  of a silently dropped field. Because that widens what counts as invalid, main.py wraps its
  import-time load_config() so a present-but-invalid config falls back to no config (keeping the
  config commands reachable to fix it) and the root banner prints the reason, rather than crashing
  on import.


## v0.5.0 (2026-07-14)

### Bug Fixes

- **lambda**: Follow all log streams when tailing
  ([`616256a`](https://github.com/datapointchris/dectl/commit/616256a10320a798a61f1b5b45350e60d79f4474))

Lambda writes each execution environment to its own CloudWatch log stream, so an invocation that
  cold-starts after the warm environment is reaped (~5-15 min idle) lands in a new stream.
  tail_lambda_logs pinned to the single newest stream at startup and followed only that one, so
  later runs were invisible until the command was killed and restarted.

Follow the whole log group with filter_log_events instead, advancing a moving startTime and deduping
  the inclusive boundary events by eventId. Glue tailing is unchanged: its run-id-named streams are
  created up front, so pinning is correct there.

### Continuous Integration

- Skip generated CHANGELOG in markdownlint
  ([`97353b3`](https://github.com/datapointchris/dectl/commit/97353b3be41f53e9744ce14d182e46b8161b18d6))

CHANGELOG.md is generated by semantic-release, which owns its format: per-version duplicate section
  headings (MD024) and blank-line spacing (MD012). Style-linting it produced an unfixable failure
  plus a blank-line auto-fix that reverted on every release. Exclude it from the markdownlint hook.

### Features

- Add multi-environment {env} substitution
  ([`05e8ded`](https://github.com/datapointchris/dectl/commit/05e8ded35110098976bb2fe5ca5c708d24f21ba3))

Resource names in config carry an {env} placeholder (e.g. salesdata-{env}-ds-thing) that is
  substituted at runtime, so one config drives dev/staging/prod by swapping a single token instead
  of duplicating every name per environment. No derivation from inconsistent names; the substitution
  point is marked explicitly.

- --env option (envvar DECTL_ENV, default from defaults.environment) gives the priority chain --env
  > DECTL_ENV > config > dev - env.py holds the active env and substitutes {env} generically across
  every string field of a config model; applied at the resolve_* chokepoints and in the display
  loops, at runtime (never at import, since the flag is not known until the command runs) - assumes
  one AWS account across environments: names change, the session does not - the active env is
  surfaced so you know which one you are targeting: a banner on the bare dectl landing and a dectl
  env command, both showing the resolved env and its source (via Click's get_parameter_source) -
  drop top-level no_args_is_help so the callback can print the banner before help

- Add step functions and pipeline monitor
  ([`89b6f2b`](https://github.com/datapointchris/dectl/commit/89b6f2b33b308e1a13cfe2b0565a794635b3745a))

Step Functions resource (sfn), mirroring the glue/lambda factory pattern: - start (with --follow),
  watch, and list executions - watch tails the GetExecutionHistory API, rendering typed state
  transitions (no CloudWatch logging setup required); a generic renderer pulls state name and
  error/cause from whichever *EventDetails field is present - config: step_functions maps alias ->
  {name, optional log_group}; the ARN is built from account_id/region like the other resources

Pipeline-level monitor command: - tails several resources at once as one timestamp-ordered stream
  with a color-coded per-resource prefix, so a multi-lambda / step function pipeline's
  cross-resource sequence reads top to bottom - what it watches is defined explicitly in a
  per-pipeline monitor config block, so the command takes no arguments and requires no recall of
  resource names - logs.tail_log_groups generalizes the glue multi-stream tailer, reusing the
  moving-startTime + eventId boundary dedup from the lambda tail fix - a monitored state machine
  must have log_group set (Express workflows only log to CloudWatch); monitor warns and skips when
  it is missing

- **s3**: Add bucket export and mount commands
  ([`155c829`](https://github.com/datapointchris/dectl/commit/155c8297124df337d1f069c006aebcb0ece5737b))

Buckets become a shortname -> bucket-name mapping (matching the glue/lambda alias shape) instead of
  a fixed raw/curated/error schema, so a pipeline can declare any buckets it wants. Old configs
  still parse: raw/curated/error are now just ordinary shortnames.

The new s3 resource exposes: - export: prints eval-able 'export pipeline_shortname=s3://bucket'
  lines (lowercase) for use with the aws CLI, since a CLI cannot mutate its parent shell's
  environment. - mount/unmount: wrap mount-s3 (Mountpoint for Amazon S3) to expose a bucket as a
  local directory. Linux-only; refuses cleanly on macOS since FUSE is unavailable there.


## v0.4.1 (2026-07-09)

### Bug Fixes

- **glue**: Preserve existing job definition on deploy
  ([`aeb2d4e`](https://github.com/datapointchris/dectl/commit/aeb2d4eb548f0cceed5ede1fd291c74ac7f9cc1d))

update_glue_job built a minimal JobUpdate from scratch, which broke deploys in three ways, since
  Glue's UpdateJob replaces the whole definition rather than patching it:

- Empty Connections: get_job omits the Connections key for jobs with none, so the old default
  produced {'Connections': []}, which UpdateJob rejects with "empty connections list is not allowed
  when Connections is specified". - Field reset: omitted fields (Timeout, GlueVersion, WorkerType,
  MaxRetries, ExecutionProperty, ...) and default arguments set outside dectl silently reverted to
  defaults on every deploy. - Capacity conflict: Spark jobs report a derived MaxCapacity alongside
  WorkerType/NumberOfWorkers, and echoing both back is rejected with "do not set Max Capacity if
  using Worker Type and Number of Workers".

Now start from the existing definition, strip the read-only keys UpdateJob rejects (Name, CreatedOn,
  LastModifiedOn, ProfileName, AllocatedCapacity), drop MaxCapacity when the worker-based model is
  in use, merge connections and default arguments additively, and override only Role and
  ScriptLocation.

Add unit tests for the payload logic plus an opt-in live AWS round-trip test (--run-integration)
  that creates a throwaway role and job of each type, deploys, asserts the definition survives, and
  tears everything down.

- **lambda**: Surface function errors from invoke
  ([`502e513`](https://github.com/datapointchris/dectl/commit/502e513c63d09b5ddd51df84510753d3625482cc))

A handled or unhandled exception in a Lambda still returns HTTP 200 with an error payload;
  FunctionError is the only signal it failed. invoke printed that payload as though it were a
  successful result and exited zero. Check FunctionError, label the output as an error, and exit
  non-zero so failures are visible and scriptable.


## v0.4.0 (2026-07-09)

### Features

- **logs**: Pretty-print structured json log events
  ([`5bffee4`](https://github.com/datapointchris/dectl/commit/5bffee4063af9936d25cc4e3a1d684cc9d5d301c))

Structured loggers (durable functions, python-json-logger) emit each event as one dense JSON line
  with any traceback collapsed into a single \n-escaped field, which is unreadable when tailed
  verbatim. render_event detects when the whole message is a JSON object, lifts timestamp/level/
  message into a colored header, lists remaining fields, and re-expands traceback fields into
  syntax-highlighted frames via rich Syntax. Values are escaped so log text containing brackets is
  not parsed as Rich markup. Non-JSON lines print verbatim -- no partial parsing of half-structured
  input. Wired into both the Glue and Lambda tail loops.

- **logs**: Tag glue events with source stream
  ([`e2a791e`](https://github.com/datapointchris/dectl/commit/e2a791e0def3a13578c40a9378b673a9c5f16eae))

Both the output and error log groups are tailed, but only the error group was tagged, and its
  [ERROR] label conflated the error stream with error-level logs -- misleading on Glue where INFO
  records land in the error group. Tag every event with its source stream (out/err) via a testable
  stream_prefix helper, so a line duplicated across both groups by a propagating logger in the job
  is obvious at a glance.


## v0.3.0 (2026-07-08)

### Documentation

- Rewrite README around command grammar and dev loop
  ([`8528fb9`](https://github.com/datapointchris/dectl/commit/8528fb9de3e3059f6d50c30e3bc7867fc6e68d20))

Replace stale usage examples (that did not match the actual command tree) with the PIPELINE RESOURCE
  ACTION grammar, the lambda dev-loop vs --publish release distinction, and config guidance.

### Features

- **cli**: Make help config-aware at every level
  ([`26ee53a`](https://github.com/datapointchris/dectl/commit/26ee53a5f6472b39770e896f3dc1351867dcd330))

The command tree is generated from config, but help never showed which pipelines, resources, or
  aliases existed, forcing reliance on shell history. Surface the config in help everywhere:

- root help teaches the PIPELINE RESOURCE ACTION [ALIAS] grammar and groups commands into
  Global/Pipelines panels with resource summaries - no_args_is_help on every group, so partial
  commands show a menu instead of erroring - inject available job/function aliases into app help,
  argument help, and per-command example epilogs

Also fixes two bugs found during review: the update command pointed at ~/code/dectl (source lives at
  ~/tools/dectl), and removes unreadable [dim] Rich markup from config show and the Jenkins log
  tailer.

- **lambda**: Publish and promote alias on deploy
  ([`bf88ea9`](https://github.com/datapointchris/dectl/commit/bf88ea99a8f4df7898e106d83c04a74842c597cd))

deploy previously only updated $LATEST, so functions invoked through an alias (S3 triggers, durable
  functions) kept running the last published version. Add --publish to update $LATEST, wait for it
  to settle, publish an immutable version, and repoint the function's configured alias to it.

Add an optional alias field to LambdaConfig; when set, --publish moves it.


## v0.2.0 (2026-06-17)

### Bug Fixes

- Interleave output and error streams in Glue log tailing
  ([`45101bc`](https://github.com/datapointchris/dectl/commit/45101bc71972346af71004b4a1eea1d3a4d02510))

Previously output stream was tailed with follow=True, blocking until it stopped before ever showing
  the error stream. Now both streams are polled in the same loop so errors appear immediately
  alongside output.

### Documentation

- Add CHANGELOG from v0.1.0 release
  ([`b9c4ec5`](https://github.com/datapointchris/dectl/commit/b9c4ec599d66566b93c29a35c83c52f5ec2df97e))

Ports the changelog that previously existed only on the orphaned main history. Commit link updated
  to c906d27 (the initial commit in this canonical history) since main's df445a9 root will be
  discarded.

### Features

- Add glue option for Connections
  ([`0d35773`](https://github.com/datapointchris/dectl/commit/0d35773e548e43c22bea0860bf92e976eefb3cdc))

- Add httpx dependency for Jenkins API integration
  ([`ad182ed`](https://github.com/datapointchris/dectl/commit/ad182ed853a575c6878915fed8cd81f9db809aa6))

### Refactoring

- Restructure pipelines to support multiple resource types
  ([`59b5cb8`](https://github.com/datapointchris/dectl/commit/59b5cb8b2991cbd14b2510afee04e9c18ee06b4c))

Pipelines can now have both glue_jobs and lambdas (and Jenkins deploy) registered as sub-commands.
  Removes the single-type constraint. Adds per-pipeline list command and dectl update for
  reinstalling from source.


## v0.1.0 (2026-06-02)

### Features

- Initial dectl project
  ([`c906d27`](https://github.com/datapointchris/dectl/commit/c906d278a9d214206df7b916d164114003be2c9f))

Config-driven CLI for managing AWS data engineering pipelines. Consolidates repeated justfile
  operations (Glue deploy/run/logs, Lambda deploy/invoke/logs) into a single tool with YAML config
  at ~/.config/dectl/config.yaml.
