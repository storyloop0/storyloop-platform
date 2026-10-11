# StoryLoop Platform

[中文](README.md) · [English](README.en.md)

StoryLoop Platform is a player and creator platform for AI interactive fiction, providing scenario management, single-player saves, accounts, credit settlement, and a web interface. It integrates narrative generation and event execution through the public APIs of the independent [StoryLoop Harness](https://github.com/storyloop0/storyloop-harness).

Ordinary turns use `single_call`: project the history and experiences visible to the player and relevant NPCs, assemble a scene context, generate one structured proposal with a shared scene model, then validate and commit events, state, and observations in the backend. Character contexts represent identity, experience, and information perspectives; individual NPCs do not run independent ReAct instances.

## Current capabilities

| Capability | Scope |
| --- | --- |
| Interactive fiction | Free text input, character replies, action suggestions, interactive and novel presentation; story milestones and elapsed story time. |
| Scenario content | Versioned packages, character cards, initial state, declared mutable fields, openings, and visibility-filtered JSON worldbooks. Public examples live in `examples/`. |
| Player experience | React and FastAPI; accounts, catalog, private scenario uploads, save creation and resume, history, and SSE turn progress and story segments. |
| Content management | Immutable author submissions, isolated reviewer previews, and administrator controls for accounts, roles, public versions, invitations, and audit records. |
| Storage and settlement | SQLAlchemy with local SQLite and online PostgreSQL; persisted events, observations, state, pending work, and accounting. Request IDs support turn retries and settlement recovery. |
| Optional integrations | OpenAI-compatible model APIs, Langfuse tracing, and player-approved Mem0 preference extraction. Player profiles remain separate from game facts and NPC experiences. |

## Architecture and boundaries

```text
React / Vite → FastAPI platform → StoryLoop Harness
                       ↓          projection → generation → validation → commit
                 SQL saves and accounting
```

The platform owns authentication, content lifecycle, model adapters, persistence, billing, and deployment; harness owns the reusable narrative execution contract. The dependency range is `storyloop-harness>=0.2,<0.3`, with source revision `ad0cd6fbe02267a7d26fff36944166551056a8b0` pinned in `pyproject.toml` and `uv.lock`. `scripts/build_harness.py` builds a wheel from that revision, and the installation below names that artifact explicitly.

`bootstrap.build_portal` composes production resources around one SQL Engine and owns their cleanup. `PlayerPortal` authenticates requests and delegates save lifecycle and queries to `GameplayService`, and execution and recovery to `TurnExecutionService`. Both services share content access, player operation locks, and a `GameplayRuntime`; tests inject offline runtime/model implementations through `runtime_factory_builder`.

HTTP and CLI share `portal.operations` to own long operations; disconnecting a request does not cancel an admitted turn. Upload and prologue commands are async, with file and memory threads tracked until their actual completion. `await portal.shutdown()` stops admission and requests memory worker stop, waits 30 seconds, then cancels and waits another 5 seconds. Work surviving both deadlines causes a visible failure with resources left open for the process manager to handle. Synchronous `portal.close()` requires completed operations. These deadlines exclude the server's own request drain and synchronous resource disposal.

The shared scene model sees multiple character contexts, so perspective projection and behavioral constraints do not provide hard information isolation between characters. Long-session context selects recent and relevant experiences from committed history while retaining original events; the current worldbook retrieves JSON entries. One ordinary scene generation does not imply that openings, suggestions, or every other feature use only one model call.

The project is under active incubation. APIs, configuration, and storage formats may change without backward compatibility guarantees. The only current turn engine is `single_call`; saves containing obsolete `npc_reply` pending work are rejected and should be recreated.

## Quick start

Use Python 3.12+, Node.js 24, and Git. Run the following PowerShell commands at the repository root using a dedicated virtual environment. Use an empty `dist/harness` directory for the first build.

```powershell
py -3.12 -m venv .venv
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe scripts/build_harness.py --out-dir dist/harness
.\.venv\Scripts\python.exe -m pip install dist/harness/storyloop_harness-0.2.0-py3-none-any.whl -e ".[agents,portal]"
.\.venv\Scripts\python.exe scripts/check_environment.py --config config/local.json
```

The environment check should point to this checkout's interpreter and source. It checks import ownership, Git revision, and the complete settings schema using the pure loader. It does not initialize credentials, model clients, or databases.

Live play requires a model key. Set it in the environment and start the local API:

```powershell
$env:STORY_MODEL_API_KEY = "your-api-key"
.\.venv\Scripts\python.exe -m storyloop_platform.cli.portal_api --catalog examples/catalog.json
```

The local CLI uses packaged defaults on the first launch. Missing keys can be entered at an interactive prompt; Windows protects remembered credentials with DPAPI. A nonempty provider key environment variable takes priority over remembered credentials, then the prompt. Noninteractive runs cannot prompt. `STORY_MODEL_BASE_URL` overrides the default provider endpoint when nonempty.

Local launch selection is `--catalog`, then `STORY_CATALOG`, then the remembered catalog. Without `--config`, successful local launches remember the custom settings path (or packaged defaults) and database path; `--db` overrides the remembered database. An explicit `--config` starts a fresh selection and requires `--catalog` or `STORY_CATALOG`; it does not inherit remembered settings or database paths. Online launches use `--profile online`, have no remembered local paths or key prompt, and validate required deployment environment values before creating resources. `--profile` must match the settings `environment`.

Start the frontend in another terminal:

```powershell
cd web
npm ci
npm run dev
```

Open the [local frontend](http://127.0.0.1:5173) to register, create a save, and play. Vite proxies `/v1` to `127.0.0.1:8765`; API documentation is available at [local API /docs](http://127.0.0.1:8765/docs). Local mode uses SQLite and disables player profiles by default.

On macOS/Linux, create the environment with `python3.12 -m venv .venv` and replace Python paths with `.venv/bin/python`. Use `export STORY_MODEL_API_KEY="your-api-key"` for the key and `unset PYTHONPATH` to clear import overrides. Other CLI arguments and npm commands are the same.

## Settings and model routing

`default_settings("local")` and `default_settings("online")` load shared packaged model defaults plus a deployment profile. `load_settings(path)` applies an external JSON override; omitted `environment` means local. `config/local.json` and `config/online.json` are small storage, HTTP, and player memory overrides, rather than duplicated model catalogs. `examples/catalog.json` references scenario packages `freeform` and `scheduled` relative to the catalog directory.

Providers define endpoints and credential environment references. Model profiles reference a provider and own the model name, generation options, context window, timeout/retry policy, compatibility options, and tariff snapshot. Task routes (`single_turn`, `adjudication`, `narration`, `prologue`, `followup_actions`) select chat profiles. Player memory independently selects extraction and embedding profiles. `credits` defines welcome points and points per RMB; each model's `rate` defines provider tariffs, cached input prices, multiplier, and pricing version. The included prices and capabilities are sample snapshots, not live verified tariffs or capability guarantees.

For a complete model override, save the following as `config/custom.local.json`. It uses the same sample provider/model as the defaults; replace the endpoint, model, capabilities, and rates with values appropriate for your service:

```json
{
  "environment": "local",
  "providers": {
    "default": {
      "base_url": "https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1",
      "api_key_env": "STORY_MODEL_API_KEY",
      "base_url_env": "STORY_MODEL_BASE_URL"
    }
  },
  "models": {
    "story": {
      "kind": "chat",
      "provider": "default",
      "model": "deepseek-v4.1-flash",
      "generation": {"temperature": 0.7},
      "extra_body": {"enable_thinking": false},
      "tool_choice_policy": "auto_only",
      "structured_output_transport": "tool_call",
      "context_window_tokens": 1000000,
      "rate": {
        "pricing_version": "sample-2026-10-03",
        "input_rmb_per_million": "2",
        "output_rmb_per_million": "8",
        "cached_input_rmb_per_million": "0.2",
        "multiplier": "1"
      }
    }
  },
  "routes": {
    "single_turn": "story",
    "adjudication": "story",
    "narration": "story",
    "prologue": "story",
    "followup_actions": "story"
  }
}
```

Validate and launch it with:

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py --config config/custom.local.json
.\.venv\Scripts\python.exe -m storyloop_platform.cli.portal_api --catalog examples/catalog.json --config config/custom.local.json
```

`providers`, `models`, and `routes` replace their entire maps; include every entry and route you need. Fixed sections (`runtime`, `http`, `player_memory`, `credits`) merge one level. Storage merges when its driver is unchanged and replaces when the driver changes. External relative SQLite paths resolve from the settings file's directory unless `storage.path_base` is `cwd`; packaged defaults use the working directory. Credentials belong in the referenced environment variables, never settings JSON.

New saves take their default temperature from `single_turn` (1.0 when unset). Save related scene, narration, adjudication, and followup generation use the saved temperature override, including after a settings update; each profile's temperature applies when no save override is passed. Standalone prologue generation uses its own profile.

To check configured model access explicitly:

```powershell
.\.venv\Scripts\python.exe -m storyloop_platform.cli.model_check --profile local --config config/custom.local.json --task single_turn --task followup_actions
```

This command makes real provider requests and may incur charges. Omit `--task` to check all configured routes. `--help` is offline; the checker does not require database or player memory resources, including with `--profile online`.

## Deployment example

The API Docker build uses uv 0.11.25, matching CI, to export the `agents,portal,online,observability,player-memory` runtime graph from `uv.lock`. It downloads hash checked dependencies and installs them and both project wheels with dependency resolution disabled. The final stage copies the installed environment; Git, source, the wheelhouse and test dependencies stay in the build stage. The runtime lock does not constrain the PEP 517 build environments' `setuptools>=77`; image tags and OS packages also remain unpinned, so builds are not claimed to be byte identical.

The Compose files in `deploy/ecs` build an installed API package and use the packaged online defaults. Copy `.env.example` to `.env`, keep it private, and copy `config/online.json` to the absolute host path in `STORY_SETTINGS_FILE` (for example `/srv/storyloop-settings/online.json`). Compose mounts that file read only at `/app/config/online.json`; the API selects it with `--profile online --config /app/config/online.json`. If `STORY_SETTINGS_FILE` is omitted, Compose mounts the repository's `config/online.json`.

Set the provider key/endpoint, URL-safe PostgreSQL password, persistent data/private scenario directories, public allowed hosts, and browser origins in `.env`, then run `docker compose --env-file deploy/ecs/.env -f deploy/ecs/compose.yaml up --build -d` from the repository root. The private catalog is `/app/private/catalog.json`; upload and memory paths are `/app/uploads` and `/app/memory`. Compose supplies `DATABASE_URL`, `STORY_UPLOAD_DIR`, and `STORY_MEMORY_DIR`. Collection of player preferences requires `STORY_PLAYER_MEMORY_ENABLED=1` and player consent; configured Mem0 still needs its profiles/credentials for existing preference read/delete access when collection is disabled.

The default story, memory extraction, and embedding profiles share the default provider. Custom profiles can use separate providers and environment references; add each referenced variable to the API service's Compose `environment` block and `.env`. Keep both memory profiles when replacing online model maps, or explicitly select `player_memory.driver: "none"` and clear `extraction_profile`/`embedding_profile` to `null`. Optional Langfuse settings and content tracing remain opt in.

## Development and verification

The Harness repository owns runtime, scene projection, event execution and AgentScope adapter tests. Platform tests own SQL, HTTP, content management, billing, lifecycle and protocol integration, using public Harness interfaces and `storyloop_harness.testing`. The full platform pytest suite runs one outside checkout wheel verification covering locked versions, runtime extras, noneditable installation, an offline HTTP/SQL turn and CLI entrypoints.

Install the test integrations and the uv version used by CI, then run the backend regressions with offline model fixtures. The wheel installation test invokes `uv`, builds the pinned harness revision, and creates a temporary environment, so a first run may still need network access to download dependencies.

```powershell
.\.venv\Scripts\python.exe -m pip install uv==0.11.25 dist/harness/storyloop_harness-0.2.0-py3-none-any.whl -e ".[agents,portal,observability,player-memory,online,test]"
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
$env:STORY_MODEL_API_KEY = "offline-test"
$env:LANGFUSE_PUBLIC_KEY = ""
$env:LANGFUSE_SECRET_KEY = ""
.\.venv\Scripts\python.exe -m pytest tests -q
cd web
npm ci
npm run contracts:check
npm test
npm run build
cd ..
.\.venv\Scripts\python.exe tests/player_contract_corpus.py
```

Player wire contracts are owned by `src/storyloop_platform/api/contracts.py`. From `web`, run `npm run contracts:generate` after changing them, then commit the generated JSON Schema, TypeScript types, and standalone validators under `web/src/lib/generated`. `contracts:check` regenerates from the Python source and fails on drift without writing files; set `CONTRACT_PYTHON` to select another Python executable. The explicit cross-language command above collects actual FastAPI responses using a temporary SQL store and offline models, then validates them with the production Node parsers. Ordinary backend pytest does not require Node.

For browser regressions, from `web` run `npx playwright install chromium` and `npm run test:e2e`; these use a mock API. CI checks the backend with Python 3.12 on Windows/Linux and the frontend and browser flows with Node.js 24 on Linux. The `offline-test` value is not a live model credential.

```text
src/storyloop_platform/  API, SQL, configuration, model adapters, billing, CLI
web/                    React / Vite frontend
config/                 Small deployment settings overrides
examples/               Catalog and synthetic scenario packages
tests/                  Backend and independent installation regressions
deploy/ecs/             Docker Compose deployment resources
```

## Limitations and future directions

Offline functional tests do not establish live narrative quality, the effectiveness of character information constraints, or production recovery guarantees. Live models, PostgreSQL, and deployment environments require separate validation. A single API worker is currently recommended; settlement snapshots support completed-turn recovery, but there is no complete journal of individual model calls or guaranteed recovery across processes or multiple workers. Context selection is also constrained by the available window budget.

Ordinary scene generation uses an estimated complete request budget: the effective window is the minimum of the saved preference, routed `single_turn` model window and runtime window. `single_turn` `generation.max_tokens` reserves output (4096 when omitted), with a 256-token safety margin. Configuration and save edits reject an output reserve that cannot fit. Persisted preferences above a reduced deployment ceiling are clamped on read without changing SQL rows. The supported AgentScope adapter sends the cap as `max_completion_tokens` and checks the expanded SDK request before HTTP. Other generators and whole-campaign spending are outside this budget.

Developers can inspect the actual factory-built engine with `engine.project_context(TurnInput(game_id, text, turn_id, package.version))`. This synchronous read-only method returns a detached request and a serializable `projection.report.to_dict()` with provenance IDs, selection/trimming reasons, component estimates, and estimator/policy identifiers. Use the existing explicit player preference scope when needed. Reports contain metadata rather than source text and remain developer-side; player HTTP/SSE responses receive a fixed safe error on required-material overflow. Estimates use a conservative heuristic rather than a provider tokenizer measurement. Mandatory overflow prevents that scene's model call and world writes; prior campaign work may already have committed.

Future work may explore long-session memory strategies, narrative quality and cost comparisons, and additional failure recovery cases.

## License

This project uses the [MIT License](LICENSE). StoryLoop Harness also uses MIT.
