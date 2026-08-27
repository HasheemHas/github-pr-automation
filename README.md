# Central PR Automation

Single implementation of PR metadata, labels, reviewers, deterministic review, security
scanning, quality validation, test-gap analysis, safe fixes and merge-conflict
resolution for every allowlisted target repository.

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

`action` values: `all`, `metadata`, `labels`, `reviewers`, `review`, `security`,
`quality`, `tests`, `fix`, `resolve-conflicts`. `all` runs everything read-only;
`fix` and `resolve-conflicts` are never part of `all` and must be requested
explicitly.

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

`PARTIAL` propagates: an enabled scanner that could not run, or a review that did
not cover every file, never collapses into an overall `PASS`.

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

## Actions run summaries

Every job writes a `$GITHUB_STEP_SUMMARY` block, so the run page tells the whole
story without opening the logs or the PR:

| Job | Summary block |
| --- | --- |
| `plan` | target, action, which steps were requested, sandbox and title policy |
| `resolve-target` | PR title, author, head/base, fork flag, file and line counts, test-file count, changed-file list |
| `deterministic-analysis` | generated metadata, reproducible rule findings, test gaps and coverage |
| `validate-target` | a PASS/FAIL/SKIPPED row per command, host-vs-container execution, and the output tail of each failing command |
| `report-to-target` | the full gate table, severity counts, exactly what was written to the PR, findings, unanchorable findings, test gaps and notes |

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
  plan.py                 action dispatch + allowlist + plan artifact
  common/                 config, globs, labels, reviewers, titles, sandbox,
                          command runner
  automation/             deterministic metadata, review, test gaps, fixes,
                          and safe three-way conflict resolution
  github/                 paginated REST client, get_pr, diffmap, report
  gitops/                 merge probe, patch inspection, validate, push
  quality/  security/     validation runners (sandboxed, credential-free)
  release/                semantic version selection (read-only in Phase 1)
config/
  repositories.yml        target allowlist, validation commands, sandbox
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
