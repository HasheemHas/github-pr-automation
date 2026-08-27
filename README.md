# Central PR Automation

Single implementation of PR metadata, labels, reviewers, deterministic review, security
scanning, quality validation, changed-line coverage, PR size and risk policy,
secret detection, dependency and migration review, API compatibility checks,
flaky-test detection, release notes, merge readiness, safe fixes and
merge-conflict resolution for every allowlisted target repository.

Everything is rule-based Python. No AI provider, no API key, no external service
call — the whole pipeline runs on the diff, the PR context and the repository's
own test output.

**Phase 1 (current):** the central pipeline is implemented and tested here.
Target repositories still run their own workflows. **Phase 2 (not started):**
replace the per-repository workflows with thin dispatchers into this one.

## Running it

Actions → **Central PR Automation** → Run workflow:

| Input | Example |
| --- | --- |
| `repository` | `yshrm` (name only, no owner) |
| `pr_number` | `123` |
| `action` | `all` |

### Actions

| Action | What it runs | Needs a checkout |
| --- | --- | --- |
| `all` | everything read-only below except `flaky` | yes |
| `metadata` | PR title and the managed description block | no |
| `labels` | path, text and risk labels | no |
| `reviewers` | central reviewer groups + CODEOWNERS mapping | no |
| `review` | deterministic rule review, inline comments | no |
| `security` | configured scanners (gitleaks, semgrep, trivy) | yes |
| `quality` | install / lint / test / build commands | yes |
| `tests` | test-gap analysis by filename | no |
| `insights` | `size` + `secrets` + `deps` + `migrations` + `api` + `notes` | no |
| `size` | PR size and risk policy | no |
| `secrets` | credential detection on added lines | no |
| `deps` | dependency-change risk | no |
| `migrations` | database migration checks | no |
| `api` | API compatibility checks | no |
| `notes` | release notes from commits and paths | no |
| `coverage` | changed-line test coverage (runs `quality` first) | yes |
| `flaky` | flaky-test detection by repeating the suite | yes |
| `fix` | deterministic auto-fixes, pushed to the branch | yes |
| `resolve-conflicts` | mechanically unambiguous merge resolution | yes |

Three actions are deliberately **not** part of `all`:

- `fix` and `resolve-conflicts` write to the target repository.
- `flaky` re-runs the whole test suite `flaky.runs` times, so the cost has to be
  asked for.

Every action ends with a **merge-readiness verdict** on the PR: one
`READY` / `READY WITH WARNINGS` / `NOT READY` answer with a per-dimension
breakdown of what is holding the merge.

## Which repositories it works on

By default, **any repository in `TARGET_ORG`** — no per-repository entry needed.
`config/repositories.yml` resolves a dispatched repository like this:

```text
explicit entry (merged over defaults)   →  use it
no entry, defaults.enabled: true        →  use the defaults profile + detection
no entry, defaults.enabled: false       →  fail in `plan`, before any token exists
listed in defaults.deny                 →  always refused
```

Set `defaults.enabled: false` to go back to a strict allowlist.

The GitHub App installation is still the real authorisation boundary: a
repository the App is not installed on fails at token minting whatever this file
says. And `action=fix` stays refused for an unconfigured repository, because it
needs both a sandbox image and a non-empty `fix_allowed_paths`.

### Auto-detected validation commands

A repository that declares no `commands` gets them derived from its own manifests
by [detect.py](scripts/common/detect.py) — one recursive tree read plus a handful
of manifest files, over the API. No checkout, no execution.

| Detected | Commands |
| --- | --- |
| `package.json` | `npm ci --ignore-scripts` (or `npm install` without a lockfile), then only the `lint` / `typecheck` / `test` / `build` scripts the manifest actually declares |
| `requirements*.txt`, `*.py` | pip install, a `py_compile` syntax pass, and `pytest` only when test files exist |
| `composer.json`, `*.php` | composer install, `php -l` over the tree (vendor pruned), phpunit when a config exists |
| `go.mod` | `go vet`, `go build`, `go test` |
| `Cargo.toml` | `cargo check`, `cargo test` |
| `Gemfile` | `bundle install`, `rspec` when `spec/` exists |
| `pom.xml` / `build.gradle` | `mvn verify` / `gradlew build` |
| `*.sln` / `*.csproj` | `dotnet restore`, `build`, `test` |

Deliberate properties:

- **An npm script is only run when `package.json` declares it**, so you never get
  a guaranteed-failing `npm run lint` on a repo that has no lint script.
- **Explicit config always wins**, and declaring *any* command opts the
  repository out of detection entirely — half-detected, half-declared command
  sets would be unpredictable.
- **Vendored paths are ignored**: `node_modules`, `vendor`, `dist`, `.venv`, and
  friends never produce commands.
- **Partial coverage is reported, never hidden.** Per-ecosystem and total caps
  exist, and anything they drop is listed in the `resolve-target` job summary as
  a warning. A monorepo with 40 workspaces says so rather than looking green.

Detection is a good default, not a good final answer. `yshrm` keeps an explicit
entry precisely because detection also finds its `automationtests` Maven suite
and two Android Gradle builds — correct, but not what you want on every PR.

## Required configuration

**Actions secrets** (repository or organization scope):

| Secret | Purpose |
| --- | --- |
| `GH_APP_ID` | GitHub App ID |
| `GH_APP_PRIVATE_KEY` | GitHub App private key, PEM contents |
| `GH_APP_CLIENT_ID` | Optional modern GitHub App client ID; preferred over `GH_APP_ID` |

**Actions variables:**

| Variable | Required | Purpose |
| --- | --- | --- |
| `TARGET_ORG` | yes | Owner of the target repositories, e.g. `mycompany` |

No AI provider, model configuration, or AI API key is required. Metadata,
diff review, test-gap detection, safe fixes, and conflict decisions are produced
by versioned Python rules in `scripts/automation/`.

`TARGET_ORG` is not `GITHUB_ORG`: GitHub reserves the `GITHUB_` prefix and will
not let you create a variable with that name.

**GitHub App permissions** (repository level, on each target):

| Permission | Level | Used for |
| --- | --- | --- |
| Contents | Read | checkout for validation; Write only for `fix` / `resolve-conflicts` |
| Pull requests | Write | body, labels, reviewers, comments |
| Issues | Write | labels and the report comment |
| Checks | Write | the `PR Automation / Quality Gate` check run |
| Metadata | Read | mandatory baseline |

Install the App on each target repository, then add it to
`config/repositories.yml` with `enabled: true`.

## Security model

Target-controlled code executes in exactly one job, `validate-target`, and that
job holds no GitHub App token, no private key and no write
permission. It receives the target working tree as an artifact prepared by a
separate job.

| Job | Credentials | Runs target code |
| --- | --- | --- |
| `plan` | none | no |
| `resolve-target` | App read token | no |
| `deterministic-analysis` | none | no |
| `prepare-validation-source` | App read token | no |
| `deterministic-resolve` | none | no |
| `validate-target` | **none** | **yes** |
| `report-to-target` | App write token | no |
| `privileged-fix-write` | App write token | no |

A patch is only ever pushed when the credential-free job validated the exact
same bytes (verified by SHA-256) and the PR head has not moved since analysis.

The extra checks respect the same boundaries. Every insight in `scripts/insights/`
runs in `deterministic-analysis` — no credential, no network, no target code, only
the sanitised diff and PR context. Coverage and flaky detection run in
`validate-target`, because they need the checkout and the test suite; coverage
only parses report files unless the repository configures `coverage.command`, and
flaky detection runs the repository's own test command under the same sandbox
rules as every other target command.

Two inputs those checks read are untrusted output from a test run of target code —
coverage reports and JUnit XML. Both parsers are bounded in bytes, reject a
document declaring a DTD or entity outright (closing the entity-expansion
avenue), and degrade to "what could be read" on a malformed file rather than
raising.

Detected credentials are **never** stored. A finding carries a masked
fingerprint — the first two characters and a length — so no secret value reaches
a log line, an artifact, a job summary or a PR comment. This is asserted directly
by the test suite.

### Sandboxing target commands

Keeping secrets away from target code is not enough on its own for `fix`:
integrity matters too. Set a `sandbox` block per repository and target commands
run inside a container with **only the target tree mounted** — no central
scripts, no artifact directory, no runner temp, no Docker socket, capabilities
dropped, no privilege escalation, and a fixed environment that inherits nothing
from the runner.

```yaml
sandbox:
  enabled: true
  image: 'your-registry/image@sha256:...'   # pin by digest
  required_binaries: [sh, node, npm, python3]
  isolate_network: false   # true = network only for the install group
```

`action=fix` and `action=resolve-conflicts` **refuse to run** without a working
sandbox, because those decide whether a patch reaches the branch. Read-only
actions warn and fall back to host execution. The image is pulled by the trusted
host and preflighted for the required binaries before any target command runs.

Every artifact is an explicit file list. `$RUNNER_TEMP` is never uploaded whole.
No private key belongs in this repository; `.gitignore` blocks `*.pem`, `*.key`,
`*.private-key.*` and `.env*`, and CI fails if key material is ever committed.

## Deterministic behavior and safety limits

| Failure | Result |
| --- | --- |
| Metadata input is incomplete | Generate only facts available from GitHub metadata |
| Diff is truncated | Mark review coverage `PARTIAL` and name uncovered files |
| Static analyzer cannot run | Report `ERROR`/`PARTIAL`, never `PASS` |
| No supported safe fix exists | No patch, commit, or push |
| Both sides changed a conflicted file | Refuse automatic resolution for human review |
| Head or base moves after validation | Refuse the push and require a re-run |
| One insight raises an exception | That section reports `ERROR`; every other insight still produces its result |
| No coverage report exists | `NOT RUN` naming what to configure, never a silent pass |
| A changed file is in no coverage report | Reported as unmeasured and excluded from the percentage, not scored as zero |
| The changed-line count is below `min_changed_lines` | The percentage threshold is not enforced |
| The test command emits no JUnit XML | Flaky detection is `NOT RUN` with the flag to add |
| A blocking readiness dimension was skipped | `NOT READY` — a check that never ran is not a pass |
| Approvals or mergeability are missing | Reported in readiness only; the check-run conclusion is unaffected |

`PARTIAL` propagates: an enabled scanner that could not run, or a review that did
not cover every file, never collapses into an overall `PASS`.

`WARN` does not propagate. An advisory finding — a large diff, a widened
dependency range, a migration with no rollback note — appears as its own amber
row and in the merge-readiness summary, but it does not turn the
branch-protection check amber. A check that goes amber on every second merge
stops meaning anything.

## The extra checks

All of these are rule-based, run without a credential, and are configured per
repository in `config/repositories.yml`. Each is **advisory by default**: it
reports, and only blocks when the repository sets its `fail_on` /
`fail_on_violation` / threshold key. The two exceptions are the secret scan
(`fail_on: high`) and coverage below a configured threshold, both of which block
out of the box.

### Changed-line test coverage — `coverage`

"Are there tests?" is a much weaker question than "is the code this PR added
actually executed by a test?". Coverage intersects the added lines from the diff
with a coverage report produced by the repository's own test run.

- Formats read: Cobertura XML, LCOV, coverage.py JSON, Istanbul
  `coverage-final.json`, JaCoCo, Clover, Go coverprofile.
- Report paths are matched to repository paths by longest shared path suffix, so
  a report written from a build directory or with package-qualified names still
  lines up without configuration.
- An added line the report does not list is **not executable** (a blank line, a
  comment, a decorator) and is excluded from the denominator rather than counted
  as uncovered.
- A changed source file that appears in **no** report is reported separately as
  unmeasured. "0 of 40 new lines are tested" and "the suite never imports this
  file" are different problems, so they get different answers.
- A change smaller than `coverage.min_changed_lines` is never judged on a
  percentage — 2 of 3 lines is 66% and says nothing.

Point `coverage.command` at a command that emits a report
(`pytest --cov --cov-report=xml`, `jest --coverage`), or `coverage.reports` at
where one already lands. With neither, the check reports `NOT RUN` with the
reason — never a silent pass.

### PR size and risk policy — `size`

Reports three separate things: **volume** (files and lines), **spread** (how many
unrelated areas one PR touches) and **risk** (whether a high-blast-radius path is
riding along with ordinary code).

Generated files — lock files, vendored trees, snapshots, minified bundles — are
counted **separately**, not dropped. "1200 lines, 1150 of them a lock file" and
"1200 hand-written lines" deserve opposite reactions. Tests, docs, dependency and
config changes never count towards "mixed concerns", because every good PR
touches them.

### Secret detection — `secrets`

Complements gitleaks rather than replacing it: it needs no binary on the runner,
scans only the added lines, and always produces an answer, so a scanner download
failure is no longer the difference between "clean" and "nothing was checked".

Roughly 30 provider patterns (AWS, GitHub, GitLab, Slack, Google, Stripe,
SendGrid, Twilio, OpenAI, Anthropic, NVIDIA, npm, PyPI, DigitalOcean, private key
blocks, JWTs, connection strings with passwords), plus an entropy-gated generic
assignment rule and an environment-file rule.

**A matched value is never stored.** Findings carry a masked fingerprint — first
two characters and a length — and nothing more, so no credential reaches a log,
an artifact or a PR comment. Every finding says to rotate first, and that
deleting the line does not remove it from git history. Placeholders
(`changeme`, `${VAR}`, `os.environ[...]`, base64-encoded prose) are recognised
and excluded; a private key block is reported even inside an
`.env.example`-style allowlisted path, because a committed key is a committed
key.

### CODEOWNERS and reviewer mapping — `reviewers`

CODEOWNERS is now parsed properly, with GitHub's real semantics: the **last**
matching rule wins outright (it does not merge with earlier ones), a rule with no
owners clears ownership for that subtree, and anchoring and directory rules
behave as GitHub resolves them. Owners are split into users, teams and email
addresses.

Ownership is now always **reported** — which owner is on the hook for how many
files, and which changed paths no rule covers. Previously full coverage
suppressed both the request and the mapping, throwing away the useful half.
Whether the owners are also *requested* is `reviewers.codeowners`:

| Mode | Behaviour |
| --- | --- |
| `defer` (default) | report the mapping; let GitHub assign the owners itself |
| `request` | request the owners explicitly as well |
| `ignore` | do not read CODEOWNERS; use the central groups only |

The central groups in `config/reviewers.yml` still apply, and fill any gap
CODEOWNERS leaves. The author is never requested, nothing is requested twice, and
existing reviewers are only ever added to.

### Dependency-change risk — `deps`

A dependency bump is code authored by someone outside the review, so it gets its
own section. Parsed from the diff across npm, Python, PHP, Go, Ruby, Rust, Java,
.NET, Dockerfiles and GitHub Actions:

- manifest changed without its lock file (and the reverse)
- added, removed and upgraded packages with the version transition
- major bumps, downgrades, widened ranges and pre-release pins
- install-time lifecycle hooks and non-registry (git/URL) sources

### Database migration checks — `migrations`

A migration is the one part of a PR that reverting the merge does not undo.
Detects Django, Alembic, Laravel, Rails, Flyway, Liquibase, Prisma, Knex,
golang-migrate and plain SQL, then checks:

- the framework's naming convention (ordering depends on the filename)
- a reverse path, or an explicit statement that the migration is irreversible —
  Flyway and Prisma are forward-only and are not asked for one
- destructive and locking operations: `DROP TABLE`/`COLUMN`, `TRUNCATE`,
  `DELETE`/`UPDATE` with no `WHERE`, `NOT NULL` with no default, type changes,
  renames, non-`CONCURRENTLY` index builds, foreign-key and unique constraints,
  raw SQL and row-by-row data migrations — each with what to do instead
- an existing migration **edited in place**, which desynchronises every
  environment that already applied it
- migration and application code moving together

### API compatibility — `api`

Compares the removed side of the diff against the added side to find what breaks
a caller: routes that no longer exist (Flask, FastAPI, Django, Express, NestJS,
Laravel, Spring, Go, Rails, OpenAPI), removed public functions and classes,
required parameters added to an existing signature, and schema fields removed or
newly made required (Pydantic, DRF serialisers, TypeScript, GraphQL, protobuf).

Wording is deliberately careful: a symbol that moved to a file this PR does not
touch cannot be distinguished from a deletion by reading a diff, so every removal
finding says so.

### Flaky-test detection — `flaky`

A test that fails randomly and a test that is broken produce the same red X.
Treating them the same is how a suite ends up with tests nobody trusts.

Flakiness is established two ways: the suite is run `flaky.runs` times against an
unchanged tree and any test that both passed and failed is flaky by observation;
and the runner's own retry markers (Surefire `flakyFailure`,
pytest-rerunfailures / Jest `rerunFailure`) are proof of a retry even with
`runs: 1`. A test that failed in **every** run is reported separately as a
consistent failure — that is a real defect, and burying it among the flakes is
how a regression gets ignored. A test collected in only some runs is reported as
a third category.

Requires the runner to emit JUnit XML (`pytest --junitxml`, `jest-junit`,
`phpunit --log-junit`, `gotestsum --junitfile`). Off by default.

### Release notes — `notes`

Templates, not a model. Conventional Commit subjects are parsed where present;
where they are absent the commit is classified from its wording and the paths it
touched, so a repository that never enforced a convention still gets usable
notes. Grouped into Added / Fixed / Changed / Performance / Security / Deprecated
/ Removed / Dependencies / Documentation, plus **Deployment Notes** derived from
the changed paths and from the migration and dependency insights, so the notes
and the report cannot disagree. Written to the PR body's Release Notes section
and uploaded as `release-notes.md`.

### Merge readiness — always

One final answer, assembled from every section above plus the two things only
GitHub knows: human approvals and mergeability.

| Verdict | Meaning |
| --- | --- |
| `READY` | every blocking dimension passed |
| `READY WITH WARNINGS` | nothing blocking failed, but something needs judgement |
| `NOT READY` | a blocking dimension failed, or never ran |

Which dimensions block is configuration, not opinion: `readiness.block_on`. A
dimension named there that **did not run** also holds the merge, because a check
that never ran is not a pass — so enabling a check is what makes it mandatory,
never an accident of which job was skipped. A non-blocking dimension that was
never requested is not listed as a warning; one that actually failed is.

Approvals use each reviewer's *latest* decisive review, the way GitHub resolves
it: a superseded `APPROVED` is not an approval, a later `APPROVED` clears an
earlier `CHANGES_REQUESTED`, and a self-review never counts. Mergeability prefers
the local merge probe over GitHub's cached `mergeable` flag, which lags.

The check run's **conclusion still comes from the quality gate alone**. Readiness
includes approvals and mergeability, which branch protection already enforces; a
check that failed for a missing approval would deadlock against it. Readiness
appears in the check title, the summary and the PR comment, where it informs
without gating twice.

## What lands on the PR

- **Body** — a managed description block between two HTML markers. Author text
  outside it is preserved verbatim; regenerating replaces only the block. It opens
  with the description itself, not a generated-summary banner. Sections:

  | Section | Content |
  | --- | --- |
  | Description | the full what/why/how, in prose |
  | Change Type | `feature` / `fix` / `refactor` / `documentation` / `chore` / `security` / `test` |
  | Risk | level, icon, and *why* that level |
  | Components | the modules or areas touched |
  | Changes | itemised change list |
  | Release Notes | changelog-ready lines ("Added…", "Fixed…") |
  | Breaking Changes | always stated, `None.` included |
  | Migration Notes | config, env var, data or deploy steps |
  | Recommended Testing | what a reviewer should verify |
  | Review Focus | the files or behaviours needing closest attention |
  | Rollback | how to revert safely |
  | Related Issues | references found in the PR text (never invented) |
  | Change Stats | files, ±lines and commits from the PR |

  An empty section is omitted, never padded with `None`/`N/A`, so a one-line PR
  gets a short block. Choose and order sections per repository with
  `metadata.sections` in `config/repositories.yml`.
- **Title** — replaced only when the human title is a placeholder (`update`,
  `fix`, `Update auth.py`, `wip`, a bare ticket ref…). A descriptive title such
  as `fix(auth): prevent refresh token reuse` is never touched. Configure with
  `metadata.title_policy` (`weak` default, `always`, `never`), and the report
  always states which way it went and why.
- **Inline review comments** — critical/high/medium findings are posted as a
  review on the exact changed line. The line is verified against the right-side
  diff map; a finding that cannot be anchored stays
  in the summary rather than landing on a wrong line. Re-runs de-duplicate on a
  finding fingerprint, capped at 25 comments per review.
- **Labels**, **reviewer requests**, one **report comment**, and the
  `PR Automation / Quality Gate` **check run** on the target head SHA.
- **The report comment** leads with the merge-readiness verdict and what is
  blocking it, then the changed-line coverage figure, labels, reviewers and file
  ownership. The full check table and every insight's findings are collapsed
  behind `<details>` so the comment stays readable — detected credentials (masked),
  interface changes, migration checks, dependency changes with a package table,
  uncovered changed line numbers, flaky tests and the draft release notes.
- **Artifacts** — `gate.json` now also carries the readiness verdict, the
  changed-line coverage figure, the secret count and the reviewer decision;
  `release-notes.md` is uploaded alongside `analysis.json`.

## Actions run summaries

Every job writes a `$GITHUB_STEP_SUMMARY` block, so the run page tells the whole
story without opening the logs or the PR:

| Job | Summary block |
| --- | --- |
| `plan` | target, action, which steps were requested, sandbox, title policy, CODEOWNERS mode and the configured policies |
| `resolve-target` | PR title, author, head/base, fork flag, file and line counts, test-file count, approvals and changes-requested, changed-file list |
| `deterministic-analysis` | a row per section, every insight's findings in full, and the draft release notes |
| `validate-target` | a PASS/FAIL/SKIPPED row per command, host-vs-container execution, the output tail of each failing command, the coverage table with uncovered line numbers, and the flaky/consistent-failure split |
| `report-to-target` | the merge-readiness table with what is blocking, the full gate table, severity counts, exactly what was written to the PR, per-insight findings, coverage, flaky tests, release notes and notes |

Missing `GITHUB_STEP_SUMMARY` is a no-op, so the scripts stay runnable locally,
and a summary write failure can never fail a job.

`report-to-target` succeeds when it successfully publishes the result, even when
the target quality gate is `FAIL`. The `PR Automation / Quality Gate` check on
the target commit retains the failing conclusion for branch protection; the
reporting job is not marked as crashed merely because it reported that failure.

## Review and fix rules

The built-in diff review detects credential-shaped literals, private keys,
dynamic code execution, shell execution, disabled TLS verification, bare Python
exceptions, debug output, work markers and trailing whitespace. Ecosystem tools
such as Ruff, ESLint, Bandit, Semgrep and Gitleaks should be enabled through the
repository command configuration for deeper language-aware coverage.

Automatic fixes are intentionally narrower than review: the built-in fixer
removes trailing whitespace and replaces bare Python `except:` handlers with
`except Exception:` only on lines added by the PR. Add centrally reviewed
formatter or codemod rules when a repository needs more transformations.

Conflict resolution performs a verified three-way merge. It resolves identical
or one-sided changes and semantically merges JSON objects when the two branches
changed different keys. Same-key JSON conflicts and text files changed by both
sides are left for human review; automation never chooses `ours` or `theirs`
arbitrarily.

Test-gap analysis reads the whole repository tree at the PR head (one Git Trees
call, no checkout), so existing tests are found even when the PR does not touch
them.

## Layout

```text
scripts/
  plan.py                 action dispatch + allowlist + policy + plan artifact
  common/                 config, globs, labels, titles, sandbox, command runner
    codeowners.py           CODEOWNERS parsing and per-file ownership
    reviewers.py            central groups + CODEOWNERS reviewer resolution
    readiness.py            the merge-readiness verdict
  automation/             deterministic metadata, review, test gaps, fixes,
                          and safe three-way conflict resolution
  insights/               diff-only insights, all credential-free
    changes.py              per-file added/removed line parsing
    base.py                 the shared status/finding envelope
    size.py                 PR size and risk policy
    secrets.py              credential detection (values always masked)
    deps.py                 dependency-change risk
    migrations.py           database migration checks
    apicompat.py            API compatibility checks
    notes.py                release notes from commits and paths
    run.py                  runs the requested insights in one diff pass
  coverage/               changed-line coverage (needs the checkout)
    parsers.py              Cobertura, LCOV, coverage.py, Istanbul, JaCoCo,
                            Clover, Go coverprofile
    run.py                  diff-to-report intersection and threshold
  flaky/                  flaky-test detection (needs the checkout)
    junit.py                JUnit XML parsing
    run.py                  repeat runs and outcome classification
  github/                 paginated REST client, get_pr, diffmap, report
  gitops/                 merge probe, patch inspection, validate, push
  quality/  security/     validation runners (sandboxed, credential-free)
  release/                semantic version selection (read-only in Phase 1)
config/
  repositories.yml        target allowlist, validation commands, sandbox,
                          and every check policy
  reviewers.yml           reviewer routing
.github/workflows/
  central-pr-automation.yml   the pipeline
  ci.yml                      self-tests
  release-central.yml         read-only version proposal
```

There are deliberately no per-repository dispatcher workflows here:
`central-pr-automation.yml`'s own `workflow_dispatch` covers every action.

Scripts are package modules; run them as `python -m scripts.<module>`.

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q scripts tests
```
