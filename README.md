# Central PR Automation

Single implementation of PR metadata, labels, reviewers, AI review, security
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
| `NVIDIA_API_KEY` | OpenAI-compatible NVIDIA API key |

**Actions variables:**

| Variable | Required | Purpose |
| --- | --- | --- |
| `TARGET_ORG` | yes | Owner of the target repositories, e.g. `mycompany` |
| `AI_BASE_URL` | no | Overrides `https://integrate.api.nvidia.com/v1` |
| `AI_METADATA_MODEL` | no | Overrides `nvidia/nemotron-3.5-lightning-30b-a3b` |
| `AI_REVIEW_MODEL` | no | Overrides `nvidia/nemotron-3.5-lightning-30b-a3b` (review, test-gap analysis and patches) |

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

Run **Verify AI model availability** once after setup to confirm the configured
model IDs exist on your endpoint.

## Security model

Target-controlled code executes in exactly one job, `validate-target`, and that
job holds no GitHub App token, no private key, no NVIDIA key and no write
permission. It receives the target working tree as an artifact prepared by a
separate job.

| Job | Credentials | Runs target code |
| --- | --- | --- |
| `plan` | none | no |
| `resolve-target` | App read token | no |
| `ai-analysis` | NVIDIA key | no |
| `prepare-validation-source` | App read token | no |
| `ai-resolve-conflicts` | NVIDIA key | no |
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

## Fail-safe AI behaviour

| Failure | Result |
| --- | --- |
| Metadata call fails | PR title and body left unchanged |
| Review call fails | no findings posted, nothing fabricated |
| Some review batches fail | surviving findings kept, verdict drops to `PARTIAL` |
| Malformed JSON or schema violation | treated as a failure, never written |
| Fix generation fails or is unconfident | no patch, no commit, no push |
| Conflict resolution unconfident or out of allowlist | no resolution |

An AI failure shows in the report as `ERROR` or `NOT RUN`, never as `PASS`.
`PARTIAL` propagates: an enabled scanner that could not run, or a review that did
not cover every file, never collapses into an overall `PASS`.

## What lands on the PR

- **Body** — a managed description block between two HTML markers. Author text
  outside it is preserved verbatim; regenerating replaces only the block. It opens
  with the description itself, not an "AI summary" banner. Sections:

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
  | Change Stats | files, ±lines, commits — from the PR, not the model |

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
  diff map, not trusted from the model; a finding that cannot be anchored stays
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
| `ai-analysis` | per-section status, both model IDs, the proposed title and summary, the findings table, batch coverage and any unreviewed files |
| `validate-target` | a PASS/FAIL/SKIPPED row per command, host-vs-container execution, and the output tail of each failing command |
| `report-to-target` | the full gate table, severity counts, exactly what was written to the PR, findings, unanchorable findings, test gaps and notes |

Missing `GITHUB_STEP_SUMMARY` is a no-op, so the scripts stay runnable locally,
and a summary write failure can never fail a job.

## Large pull requests

The review splits the diff by file, splits oversized files by hunk, packs the
pieces into batches, and merges the findings. Tune with `AI_REVIEW_BATCH_CHARS`,
`AI_REVIEW_BATCH_FILES` and `AI_REVIEW_MAX_BATCHES`. If the batch cap is hit, the
uncovered files are named in the report and the verdict is `PARTIAL` — a review
never claims coverage it does not have.

Test-gap analysis reads the whole repository tree at the PR head (one Git Trees
call, no checkout), so existing tests are found even when the PR does not touch
them.

## Layout

```text
scripts/
  plan.py                 action dispatch + allowlist + plan artifact
  common/                 config, globs, labels, reviewers, titles, sandbox,
                          command runner
  ai/                     client (JSON fallback), schemas, prompts, chunking,
                          analyze, resolve, check_models
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
  verify-ai-models.yml        model availability diagnostic
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
