# atlantis-codex

A [CRS](https://github.com/oss-crs) (Cyber Reasoning System) that uses [Codex CLI](https://developers.openai.com/codex/overview) to autonomously find and patch vulnerabilities in open-source projects.

Given proof-of-vulnerability (POV) inputs that crash a target binary, the agent analyzes the crashes, edits source code, builds, tests, iterates, and submits a verified patch.

## How it works

```
POV files -> reproduce crashes -> Codex agent -> .diff patch
                                    <->
                              libCRS (build + test)
```

1. `run_patcher` scans POV files and reproduces crashes against the unpatched binary.
2. All POVs are batched as variants of one bug and passed to the selected agent (`CRS_AGENT`, default `codex`).
3. The agent runs `codex exec` non-interactively, edits source, and validates patches with libCRS build/test commands.
4. A verified `.diff` is written to `/patches/`, where a daemon auto-submits it.

## Project structure

```
bin/
  run_patcher
  compile_target
agents/
  codex.py
  codex.md
  template.py
oss-crs/
  crs.yaml
  example-compose.yaml
  base.Dockerfile
  builder.Dockerfile
  patcher.Dockerfile
  docker-bake.hcl
  sample-litellm-config.yaml
```

## Prerequisites

- [oss-crs](https://github.com/oss-crs/oss-crs) (`crs-compose`)
- [oss-crs-builder](https://github.com/oss-crs/builder) (builder sidecar)

## Runtime behavior (Codex-specific)

- Execution command: `codex exec`
- Approval/sandbox behavior: `--dangerously-bypass-approvals-and-sandbox`
  - Equivalent intent to Claude Code's dangerously-skip-permissions mode
  - Runs with no interactive permission prompts
- Container hint: `IS_SANDBOX=1` is set by the agent setup
- Instruction file generated per run: `AGENTS.md` in the target repo
- Codex config file generated per run: `$CODEX_HOME/config.toml` (default `/root/.codex/config.toml`)
- Debug artifacts:
  - Shared directory: `/root/.codex` (registered as `codex-home`)
  - Per-run logs: `/work/agent/codex_stdout.log`, `/work/agent/codex_stderr.log`
  - Codex internal logs: `/root/.codex/log/`

## Endpoint and model setup

The patcher gets endpoint credentials from OSS-CRS env vars:
- `OSS_CRS_LLM_API_URL`
- `OSS_CRS_LLM_API_KEY`

If both are present, the agent writes a Codex provider block in `config.toml`:
- `model_provider = "oss_crs"`
- `model_providers.oss_crs.base_url = <OSS_CRS_LLM_API_URL>`
- `model_providers.oss_crs.env_key = "OSS_CRS_LLM_API_KEY"`
- `model_providers.oss_crs.wire_api = "responses"`

Model is provided by env var:
- `CODEX_MODEL` (optional, default: `gpt-5.2-codex`)
The agent writes `model = "<CODEX_MODEL>"` into `$CODEX_HOME/config.toml`.

If `OSS_CRS_LLM_API_URL/KEY` are absent, Codex falls back to its default provider config.

## Quick start

### 1. Configure `crs-compose.yaml`

Copy `oss-crs/example-compose.yaml` and update paths:

```yaml
atlantis-codex:
  source:
    local_path: /path/to/crs-codex
  cpuset: "2-7"
  memory: "16G"
  llm_budget: 10
  additional_env:
    CRS_AGENT: codex
    CODEX_MODEL: gpt-5.2-codex

llm_config:
  litellm_config: /path/to/sample-litellm-config.yaml
```

### 2. Configure LiteLLM

Copy `oss-crs/sample-litellm-config.yaml` and set API credentials/endpoints for the models you intend to use (at minimum `gpt-5.2-codex`).

### 3. Run

```bash
crs-compose up -f crs-compose.yaml
```

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `CRS_AGENT` | `codex` | Agent module name (`agents/<name>.py`) |
| `CODEX_MODEL` | `gpt-5.2-codex` | Model passed to `codex exec --model` |
| `AGENT_TIMEOUT` | `0` | Timeout seconds (`0` = no limit) |

## Patch validity

A patch is submitted only if all are true:
1. Builds successfully
2. All POV variants stop crashing
3. Test suite passes (or is skipped if none)
4. Fix is semantically correct and minimal

## Adding a new agent

1. Copy `agents/template.py` to `agents/my_agent.py`.
2. Implement `setup()` and `run()`.
3. Set `CRS_AGENT=my_agent`.

The agent receives:
- `source_dir`: clean git repo of target project
- `povs`: list of `(pov_path, crash_log)`
- `harness`: harness name for `run-pov`
- `patches_dir`: write verified `.diff` here
- `work_dir`: scratch dir
- `language`: target language
- `sanitizer`: sanitizer type

Available libCRS commands:
- `libCRS apply-patch-build <patch.diff> <response_dir>`
- `libCRS run-pov <pov> <response_dir> --harness <h> --build-id <id>`
- `libCRS run-test <response_dir> --build-id <id>`
