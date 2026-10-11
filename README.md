# StoryLoop Platform

[中文](README.md) · [English](README.en.md)

**把一个剧本变成可以自由输入、持续游玩的 AI 互动故事。** 玩家在网页中选择剧本、创建单人存档，与角色交谈或尝试行动，再根据故事回应继续探索；创作者编写角色、世界状态与剧情规则，上传剧本并提交版本供审核发布。

例如，示例剧本[港口 NPC 对话](examples/freeform/manifest.json)从尚未开市的广场开始。你可以输入“问码头工今天有什么消息”，也可以观察码头或改变周围事物。交谈和行动推进故事时间，开市钟响时，剧本中的定时事件会改变港口状态。这是玩法示意，具体角色回应由所选模型生成。

[StoryLoop Harness](https://github.com/storyloop0/storyloop-harness) 提供可独立嵌入的叙事运行时；本仓库把它接成完整应用，加入网页、账号、内容目录、SQL 存档和积分结算。想直接体验或运营故事平台，从这里开始；想把叙事执行嵌入其他应用，可从 Harness 开始。

## 玩家与创作者如何使用

```mermaid
flowchart TB
    Author["创作者：编写并上传私有剧本"] --> Private["私有试玩"]
    Author --> Review["提交不可变版本：隔离试玩与审核"]
    Review --> Publish["发布公开版本：玩家选择剧本"]
    Publish --> Save["创建或续玩单人存档"]
    Save --> Play["输入行动并阅读回应"]
    Play --> Save
```

私有剧本可以自行游玩；公开目录通过内容审核流程发布版本。每个存档保存自己的历史和状态，网页支持交互与小说两种展示方式。

## 当前能力

| 能力 | 实现范围 |
| --- | --- |
| 互动叙事 | 自由输入、角色回应、行动建议、交互与小说展示模式；支持剧情节点和故事时间推进。 |
| 剧本内容 | 版本化剧本包、角色卡、初始状态、声明式状态字段、开场及按可见范围筛选的 JSON 世界书。公开示例位于 `examples/`。 |
| 玩家体验 | React 前端与 FastAPI API；注册登录、目录、私有剧本上传、创建和续玩单人存档、历史记录、SSE 回合进度与故事片段。 |
| 内容管理 | 作者送审不可变版本、审核员隔离试玩、管理员管理账号与角色、公开版本、邀请码和操作审计。 |
| 存储与结算 | SQLAlchemy 支持本地 SQLite 和线上 PostgreSQL；保存事件、观察、状态、待办工作与账务。请求 ID 支持回合重试和结算恢复。 |
| 可选集成 | OpenAI 兼容模型接口、Langfuse 追踪，以及经玩家授权的 Mem0 偏好提炼。玩家画像与游戏事实、NPC 经历分离。 |

## 架构与边界

```mermaid
flowchart TB
    UI["React / Vite：玩家与创作者界面"] --> Platform["FastAPI 平台：账号、内容、配置与路由、结算"]
    Platform --> Harness["Harness：上下文投影、叙事执行、校验、AgentScope 传输"]
    Harness --> Model["OpenAI 兼容模型服务"]
    Platform --> SQL["平台 SQL：存档、事件、状态、账务"]
    Harness --> Store["平台提供的 GameStore 适配器"]
    Store --> SQL
    Platform -.-> Mem["可选 Mem0：经授权的玩家偏好"]
    Platform -.-> Trace["可选 Langfuse：追踪"]
```

平台负责鉴权、内容生命周期、模型配置与路由、持久化、计费和部署；Harness 负责叙事运行契约、上下文投影、事件执行及 AgentScope 模型传输适配。当前依赖范围为 `storyloop-harness>=0.2,<0.3`，`pyproject.toml` 与 `uv.lock` 固定源码提交 `ad0cd6fbe02267a7d26fff36944166551056a8b0`。`scripts/build_harness.py` 从该提交构建 wheel；下方安装命令显式使用这个产物。

<details>
<summary>服务装配与关闭生命周期</summary>

`bootstrap.build_portal` 围绕同一个 SQL Engine 组装生产资源并负责关闭。`PlayerPortal` 完成请求鉴权后，把存档生命周期与查询交给 `GameplayService`，把回合执行与恢复交给 `TurnExecutionService`。两个服务共享内容访问、玩家操作锁与 `GameplayRuntime`；测试通过 `runtime_factory_builder` 注入离线运行时或模型实现。

HTTP 与 CLI 使用同一个 `portal.operations` 管理耗时操作；请求断开不会取消已接收的回合。上传与序章生成采用异步命令，文件和记忆线程按实际完成状态跟踪。`await portal.shutdown()` 先停止接收新操作并通知记忆 worker，再等待 30 秒、取消后再等待 5 秒；仍未结束则显式报错并保留资源，由进程管理器决定终止。同步 `portal.close()` 只允许在操作已结束时调用。上述时限不包含服务器自身的请求排空和同步资源释放。

</details>

项目仍在孵化，API、配置和存储格式可直接调整，不承诺向后兼容。当前唯一回合引擎为 `single_call`；包含旧 `npc_reply` 待办的存档会被拒绝，应重新创建存档。

## 一次普通回合如何执行

普通回合采用 `single_call`：玩家与相关 NPC 的可见历史、经历和世界书条目被投影到同一个场景请求中。模型给出结构化提议，Harness 校验并通过平台存储适配器提交事件、状态与观察；平台随后处理用量和积分结算。

```mermaid
sequenceDiagram
    participant Player as 玩家网页
    participant Platform as 平台
    participant Harness as Harness
    participant Model as 场景模型
    participant SQL as SQL 存储
    Player->>Platform: 行动文本与请求 ID
    Platform->>Platform: 鉴权、存档与执行设置
    Platform->>Harness: TurnInput 与运行资源
    Harness->>SQL: 读取已提交历史与状态
    Harness->>Harness: 投影上下文、估算请求预算
    alt 必需上下文超过预算
        Harness-->>Platform: 安全错误，不发出该场景模型请求
        Platform-->>Player: 回合错误
    else 请求可容纳
        Harness->>Model: 场景请求
        Model-->>Harness: 结构化提议
        Harness->>Harness: 校验提议
        Harness->>SQL: 提交通过校验的事件、状态与观察
        Harness-->>Platform: 执行结果与用量
        Platform->>SQL: 保存结算记录
        Platform-->>Player: 故事结果与进度
    end
```

长局上下文从已提交历史中选择近期与相关经历，原始事件保留；当前世界书检索 JSON 条目。

图中是成功普通场景及预算拒绝路径，省略校验失败、重试和其他剧情工作。世界提交与积分结算是分开的步骤，不承诺整场战役原子提交。NPC 上下文代表身份、经历和信息视角，每个 NPC 不对应独立 ReAct 实例；共享模型可看到多个角色的上下文，因此不构成角色之间的硬信息隔离。一次普通场景生成也不代表开场、行动建议或整场战役都只有一次模型调用。

## 快速开始

需要 Python 3.12+、Node.js 24 和 Git。以下 PowerShell 命令在仓库根目录执行，使用本仓库独立虚拟环境。首次构建使用空的 `dist/harness` 输出目录。

```powershell
py -3.12 -m venv .venv
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe scripts/build_harness.py --out-dir dist/harness
.\.venv\Scripts\python.exe -m pip install dist/harness/storyloop_harness-0.2.0-py3-none-any.whl -e ".[agents,portal]"
.\.venv\Scripts\python.exe scripts/check_environment.py --config config/local.json
```

环境检查应指向本仓库的解释器与源码；它通过纯配置加载器检查导入归属、Git 提交和完整配置结构，不初始化凭据、模型客户端或数据库。

真实游玩需要模型密钥。下面通过环境变量提供密钥并启动本地 API：

```powershell
$env:STORY_MODEL_API_KEY = "your-api-key"
.\.venv\Scripts\python.exe -m storyloop_platform.cli.portal_api --catalog examples/catalog.json
```

<details>
<summary>本地启动参数与凭据选择规则</summary>

本地 CLI 首次启动使用包内默认配置。缺少密钥时可在交互终端输入，Windows 通过 DPAPI 保护记住的凭据。非空的供应商密钥环境变量优先，其次是记住的凭据，最后是交互输入；非交互运行不会询问。非空的 `STORY_MODEL_BASE_URL` 覆盖默认供应商端点。

本地剧本目录选择优先级为 `--catalog`、`STORY_CATALOG`、记住的目录。不传 `--config` 时，成功启动会记住自定义配置路径（或使用包内默认配置）和数据库路径；`--db` 覆盖记住的数据库。显式传入 `--config` 会重新选择，需要同时提供 `--catalog` 或 `STORY_CATALOG`，不会继承记住的配置或数据库路径。线上启动使用 `--profile online`，不读取本地记住的路径、不交互询问密钥，并在创建资源前验证必要的部署环境变量。`--profile` 必须与配置的 `environment` 一致。

</details>

另开终端启动前端：

```powershell
cd web
npm ci
npm run dev
```

访问 [本地前端](http://127.0.0.1:5173) 注册、创建存档并游玩。Vite 将 `/v1` 代理到 `127.0.0.1:8765`，API 文档位于 [本地 API /docs](http://127.0.0.1:8765/docs)。本地默认使用 SQLite，玩家画像默认关闭。

macOS/Linux 用 `python3.12 -m venv .venv` 创建环境，将 Python 路径替换为 `.venv/bin/python`；环境变量使用 `export STORY_MODEL_API_KEY="your-api-key"`，清除导入覆盖使用 `unset PYTHONPATH`。其余 CLI 参数和 npm 命令相同。

## 配置与模型路由

`default_settings("local")` 和 `default_settings("online")` 加载包内共享模型配置及对应部署配置；`load_settings(path)` 应用外部 JSON 覆盖，省略 `environment` 表示本地。`config/local.json` 与 `config/online.json` 只包含存储、HTTP 和玩家记忆的少量部署差异，不复制模型目录。`examples/catalog.json` 的 `freeform` 和 `scheduled` 剧本包路径相对于该目录文件所在目录。

供应商定义端点和凭据环境变量引用；模型配置引用供应商，定义模型名称、生成参数、上下文窗口、超时重试、接口适配选项和价格快照；任务路由（`single_turn`、`adjudication`、`narration`、`prologue`、`followup_actions`）选择聊天模型配置。玩家记忆分别选择提炼与向量模型配置。`credits` 定义赠送积分和每人民币对应积分，模型的 `rate` 定义供应商价格、缓存输入价格、倍率与价格版本。内置价格和能力是示例快照，未经实时验证，不构成当前价格或能力保证。

<details>
<summary>模型覆盖示例、合并规则与访问检查</summary>

完整的模型覆盖示例如下，保存为 `config/custom.local.json`。它使用与默认配置相同的示例供应商和模型；换用其他服务时，应调整端点、模型、能力参数和价格：

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

验证并启动：

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py --config config/custom.local.json
.\.venv\Scripts\python.exe -m storyloop_platform.cli.portal_api --catalog examples/catalog.json --config config/custom.local.json
```

`providers`、`models`、`routes` 整张映射替换，需包含所需的全部条目和路由。固定配置段（`runtime`、`http`、`player_memory`、`credits`）合并一层；存储驱动相同时合并，改变驱动时替换。外部配置中的相对 SQLite 路径基于配置文件目录解析，除非显式指定 `storage.path_base: "cwd"`；包内默认路径基于当前工作目录。密钥只放在引用的环境变量中，不写入配置 JSON。

新存档从 `single_turn` 取得默认温度（未设置时为 1.0）。存档相关的场景、叙述、裁定和后续建议生成使用该存档保存的温度覆盖，包括更新设置后；没有传入存档覆盖时使用各模型配置自己的温度。独立开场生成使用自己的模型配置。

显式检查模型访问：

```powershell
.\.venv\Scripts\python.exe -m storyloop_platform.cli.model_check --profile local --config config/custom.local.json --task single_turn --task followup_actions
```

此命令会实际请求供应商，可能产生费用。省略 `--task` 会检查全部已配置路由；`--help` 可离线运行。模型检查无需数据库或玩家记忆资源，包括使用 `--profile online` 时。

</details>

## 部署示例

API Docker 构建使用 CI 同版本的 uv 0.11.25，从 `uv.lock` 导出 `agents,portal,online,observability,player-memory` 运行依赖，下载并校验哈希，再禁用依赖解析安装第三方包与两个项目 wheel。最终阶段只复制安装后的环境；Git、源码、wheelhouse 和测试依赖留在构建阶段。运行依赖锁不约束 PEP 517 构建环境中的 `setuptools>=77`，镜像标签和系统包也未固定，因此不承诺逐字节可复现。

`deploy/ecs` 中的 Compose 文件构建并安装 API 包，使用包内线上默认配置。将 `.env.example` 复制为 `.env` 并妥善保密，将 `config/online.json` 复制到 `STORY_SETTINGS_FILE` 指定的主机绝对路径（例如 `/srv/storyloop-settings/online.json`）。Compose 将该文件只读挂载到 `/app/config/online.json`，API 使用 `--profile online --config /app/config/online.json` 选择它。未指定 `STORY_SETTINGS_FILE` 时，Compose 挂载仓库中的 `config/online.json`。

在 `.env` 设置供应商密钥和端点、URL 安全的 PostgreSQL 密码、持久化数据和私人剧本目录、公开允许主机和浏览器来源；然后从仓库根目录执行 `docker compose --env-file deploy/ecs/.env -f deploy/ecs/compose.yaml up --build -d`。私人目录为 `/app/private/catalog.json`，上传和记忆路径分别为 `/app/uploads`、`/app/memory`；Compose 提供 `DATABASE_URL`、`STORY_UPLOAD_DIR` 和 `STORY_MEMORY_DIR`。收集玩家偏好需设置 `STORY_PLAYER_MEMORY_ENABLED=1` 并获得玩家同意；即使关闭收集，已配置的 Mem0 仍需模型配置和凭据，以读取或删除已有偏好。

默认故事、记忆提炼和向量模型共享默认供应商。自定义配置可使用不同供应商和环境变量引用，需把每个引用变量加入 API 服务的 Compose `environment` 和 `.env`。替换线上模型映射时应保留两个记忆模型配置，或显式选择 `player_memory.driver: "none"` 并把 `extraction_profile`、`embedding_profile` 置为 `null`。Langfuse 和内容追踪仍按需开启。

## 开发与验证

Harness 仓库负责运行时、场景投影、事件执行和 AgentScope 适配器测试；平台测试负责 SQL、HTTP、内容管理、计费、生命周期与协议集成，只使用 Harness 公共接口及 `storyloop_harness.testing`。平台完整 pytest 包含一次仓库外 wheel 验证：检查锁定版本、运行时 extras、非 editable 安装、离线 HTTP/SQL 回合与 CLI。

安装全部测试相关集成及 CI 使用的 uv 版本，然后运行后端离线回归。wheel 安装测试会调用 `uv`、构建固定 harness 提交并创建临时环境，因此首次运行仍可能需要网络下载依赖。

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

玩家接口契约由 `src/storyloop_platform/api/contracts.py` 定义。修改后在 `web` 运行 `npm run contracts:generate`，提交 `web/src/lib/generated` 中生成的 JSON Schema、TypeScript 类型与独立验证器。`contracts:check` 从 Python 源码重新生成并检查差异，不写入文件；可用 `CONTRACT_PYTHON` 指定 Python 可执行文件。上面的独立跨语言命令用临时 SQL 存储和离线模型收集真实 FastAPI 响应，再通过生产 Node 解析器验证；普通后端 pytest 无需 Node。

浏览器回归另在 `web` 运行 `npx playwright install chromium` 与 `npm run test:e2e`，使用模拟 API。CI 在 Windows/Linux 使用 Python 3.12 验证后端，并在 Linux 使用 Node.js 24 验证前端与浏览器。测试中的 `offline-test` 不是可用于真实游玩的密钥。

```text
src/storyloop_platform/  API、SQL、配置、模型适配、计费和 CLI
web/                    React / Vite 前端
config/                 少量部署配置覆盖
examples/               剧本目录与合成剧本包
tests/                  后端与独立安装回归
deploy/ecs/             Docker Compose 部署资源
```

## 限制与后续方向

离线功能测试不证明真实模型叙事质量、角色信息约束效果或生产环境的恢复能力；真实模型、PostgreSQL 和部署环境需单独验证。当前建议使用单个 API worker；结算快照支持已完成回合的恢复，但尚无完整的逐模型调用日志，也不保证跨进程或多 worker 恢复。当前上下文选择也会受窗口预算影响。

普通场景生成使用完整请求的估算预算：有效窗口取存档偏好、`single_turn` 路由模型窗口与运行时窗口的最小值。该模型的 `generation.max_tokens` 预留输出（未设置时为 4096），并保留 256 token 安全余量。配置与存档设置更新会拒绝无法容纳输出预留的窗口；部署上限降低后，存档偏好在读取时收紧，不改写 SQL 行。支持的 AgentScope 适配器以 `max_completion_tokens` 发送输出上限，并在 HTTP 前检查 SDK 展开后的请求。其他生成器和整场战役费用不属于此预算范围。

开发者可对实际工厂构建的引擎调用 `engine.project_context(TurnInput(game_id, text, turn_id, package.version))`。此同步只读方法返回独立请求及可序列化的 `projection.report.to_dict()`，包含来源 ID、选择与裁剪原因、分项估算和估算器/策略标识；需要玩家偏好时使用现有显式偏好作用域。报告仅包含元数据，不包含来源正文，仅用于开发者检查；必需材料溢出时，玩家 HTTP/SSE 接口只返回固定安全错误。token 数是保守启发式估算，不是供应商 tokenizer 测量。必需材料溢出会阻止该场景的模型调用及世界写入；更早的战役工作可能已经提交。

后续可探索长局记忆策略，叙事质量与成本对照，以及更多故障恢复案例。

## 许可证

本项目采用 [MIT License](LICENSE)。StoryLoop Harness 同样采用 MIT。
