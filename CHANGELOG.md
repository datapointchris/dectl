# CHANGELOG


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
