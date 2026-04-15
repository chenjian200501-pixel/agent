# CodeForge — AI驱动的代码生成 Harness

> 基于 Anthropic **Scaling Managed Agents** 架构方法论构建的多智能体代码开发框架
>
> 核心原则：**当 Brain（思考编排）、Execution（执行动作）、Session（任务历史）三层分离时，每一层都可以独立演进，而层与层之间的接口保持稳定。**

## 核心架构

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           CodeForge 分层架构                                 │
├───────────────────────────────┬──────────────────────────────────────────────┤
│                               │                                              │
│     🧠 Brain 层（平台侧）       │     🤲 Execution 层（按需调用）               │
│                               │                                              │
│  ┌─────────────────────────┐  │  ┌─────────────────────────────┐             │
│  │  Step 1: DIAGNOSE       │  │  │  Sandbox 执行环境            │             │
│  │   识别失败模式           │  │  │  (代码补全 / Linter / Shell)  │             │
│  ├─────────────────────────┤  │  ├─────────────────────────────┤             │
│  │  Step 2: MINIMUM PATCH  │  │  │  Tools (按需接入)            │             │
│  │   最小必要结构           │  │  │  (文件系统 / Git / 终端)     │             │
│  ├─────────────────────────┤  │  ├─────────────────────────────┤             │
│  │  Step 3: VERIFY         │  │  │  Resources (任务专属资源)    │             │
│  │   真实任务验证           │  │  │  (项目文件 / 依赖 / 上下文)   │             │
│  ├─────────────────────────┤  │  └─────────────────────────────┘             │
│  │  Step 4: ABLATE         │  │                                              │
│  │   持续消融实验           │  │  ⚡ Execution 按需唤起:                        │
│  └────────────┬────────────┘  │  • 只是分析/规划 → 不起 Execution              │
│               │               │  • 需要执行动作     → Provision → Execute       │
│               │               │  • 执行单元挂了     → 重新 Provision 一个即可      │
└───────────────┼───────────────┴──────────────────────────────────────────────┘
                │
                ▼
    ┌───────────────────────┐
    │   Session 层（外部持久） │   ← 任务真相，从 Harness 进程中抽离
    │                        │
    │  Wake / Get Session   │  ← 根据 Session ID 唤醒任务，继续执行
    │  Get Events           │  ← 按需读取历史（不必每次全量加载）
    │  Emit Event           │  ← 每一步新事件追加写入
    │                        │
    │  💡 Session ≠ Context Window                        │
    │     Context Window = 当前推理的临时工作台             │
    │     Session         = 外部持久化的任务档案室          │
    └───────────────────────┘
```

### 关键：稳定接口

| 接口 | 方向 | 职责 |
|------|------|------|
| `Provision` | Brain → Execution | 准备好本次任务的执行环境和资源 |
| `Execute` | Brain → Execution | 执行一个动作（写文件/跑测试/调用工具） |
| `Wake` | 调度层 → Session | 根据 Session ID 唤醒任务 |
| `Get Events` | Brain → Session | 按需读取任务历史 |
| `Emit Event` | Brain → Session | 写入新事件 |

## 目录

- [核心架构](#核心架构)
- [Harness 四步循环](#harness-四步循环)
- [分层设计](#分层设计)
- [Session 管理](#session-管理)
- [新增组件](#新增组件)
- [Agent 角色](#agent-角色)
- [CLI 命令](#cli-命令)
- [Harness 注册表](#harness-注册表)
- [消融实验](#消融实验)
- [与原架构对比](#与原架构对比)
- [代码示例](#代码示例)
- [文件结构](#文件结构)

---

## Harness 四步循环

### 核心理念

**两层稳定性问题：**

1. **Harness 层（Harness 会变）**：Harness 本质上是针对当前模型能力缺口的**临时解决方案**。每一个添加的组件都编码了一个假设："模型靠自己还做不到这件事"。这些假设可能从一开始就错了，也可能在模型升级后变得过时。
2. **Execution 层（执行环境会变）**：今天用本地进程，明天可能用远程容器，后天可能是 MCP Server。Execution 层的具体实现会持续变化，但 `Provision` / `Execute` 这两个接口应该保持稳定。

**核心问题**：
- 你添加的每一个组件，都是未来的技术债——这里面哪些真在承重，哪些只是临时支架？
- Brain 和 Hands 分离后，Execution 挂了你怎么恢复任务？

**答案是**：把任务真相从 Harness 进程中抽出来，放到外部 Session 层。Brain 挂了可以从 Session 恢复，Execution 挂了只是一次普通的工具调用失败。

### 故障重定义

| 旧架构 | 新架构 |
|--------|--------|
| 容器一挂 = 整个任务现场塌了 | Execution 单元挂了 = 一次可处理的执行失败 |
| Harness 进程崩了 = 任务丢失 | Harness 挂了 → 从 Session 读取历史，新 Harness 接手继续跑 |
| 任务状态和执行环境耦合在一起 | 任务真相在 Session，不在任何活着的进程中 |

### Step 1: DIAGNOSE — 诊断缺口

在构建任何结构之前，先搞清楚模型到底哪里不行。

- 运行 `DiagnosticAgent` 分析当前模型在真实任务中的失败模式
- 识别的失败模式包括：
  - **上下文焦虑** (Context Anxiety): 长任务中上下文增长导致质量下降
  - **自我评估偏差** (Self-Assessment Bias): 模型系统性地高估自己输出的质量
  - **规划漂移** (Planning Drift): 多步骤任务中失去目标跟踪
  - **过早收工** (Premature Closure): 任务实际未完成却认为已经做完
  - **错误忽视** (Error Overlooking): 忽视自身输出中的明显错误
  - **范围低估** (Scope Underestimation): 低估任务复杂度

每个识别的缺口必须回答：**"模型靠自己还做不到什么？"**

### Step 2: MINIMUM PATCH — 最小修补

针对已识别的缺口，添加**最小必要**的结构。

- **生成器/评估器分离**: 一个 Agent 专门负责生成，另一个专门负责挑剔
  - 关键发现：训练一个独立的挑剔评估器，远比让生成器自我批判容易得多
  - 批判性可以被单独注入，不会破坏创作能力
- **Harness 注册表**: 每个组件必须在注册表中注册，记录它补的是哪个缺口

### Step 3: VERIFY — 真实验证

将 Harness 放入真实任务中，验证它是否真的是"承重结构"。

验证要看三件事：
1. **它改善了哪种失败？** — 对比有/无 Harness 的输出差异
2. **它付出了多少成本？** — 时间、Token 费用
3. **没有它结果会怎样？** — 如果去掉它，质量掉多少？

关键指标：`evaluation_score ≥ 60` = 真的可交付，`< 60` = 表面完成

### Step 4: ABLATE — 持续消融

通过消融实验找出真正沉重的部分，随模型升级持续做减法。

- 优先级：从未消融过的 > 低置信度的 > 旧模型版本添加的 > 高成本的
- 阈值：移除组件后质量下降 `< 5%` → 可以移除；下降 `> 20%` → 仍需保留
- **随着模型升级，被移除的组件会越来越多，但 Harness 的组合空间不会缩小——它只是在移动**

---

## 分层设计

### Brain 层 vs Execution 层

```
Brain 层（平台侧，始终存活）
├── DiagnosticAgent   — 识别失败模式
├── EvaluatorAgent    — 分离的挑剔评估器
├── ReviewerAgent     — YES/NO + 置信度
├── 规划/编排逻辑      — 决定下一步做什么
│
│  Provision(context) / Execute(tool, input) / EmitEvent(event)
│
└── Session           ← 读取历史，追加新事件

Execution 层（按需唤起，可能被替换）
├── Sandbox (本地进程 / 远程容器 / MCP Server)
├── Tools (文件系统 / Git / Linter / Shell)
└── Resources (项目文件 / 依赖 / 上下文)

Session 层（外部持久，存储引擎无关）
├── Wake(session_id)          — 唤醒任务继续执行
├── GetSession(session_id)    — 读取完整历史
├── GetEvents(session_id, range) — 按需切片读取
└── EmitEvent(session_id, event) — 追加新事件
```

### 为什么不把它们绑在一起

早期的做法（pets）：
```
Session + Harness + Sandbox → 共享同一个容器
```

问题：
- 容器一卡住，你不能随手丢掉重来——任务历史也绑在里面
- 从外面看，Harness 的 bug、网络问题、容器挂了，长得一样——调试失真
- Harness 默认 Brain 操作的东西就在自己旁边的容器里——私有环境接不进来

**CodeForge 的做法（cattle）**：
- Brain 不住在容器里，留在平台侧
- Session 不在任何活着的进程里，放在外部持久层
- Execution 挂了只是一次工具调用失败，系统重新 Provision 一个即可
- 下面到底是本地进程、远程容器还是 MCP Server，上面不需要知道

### Brain 不绑定单一 Agent

```
Harness 平台侧
├── DiagnosticBrain
├── CoderBrain
├── EvaluatorBrain
└── ...

执行层共享同一批 Execution 单元，Brain 之间可以互相转交 Harness。
```

---

## Session 管理

### Session ≠ Context Window

| 概念 | 含义 |
|------|------|
| **Context Window** | Brain 当前推理时临时能看到的内容，有上限 |
| **Session** | 任务外部持久化的真实历史流，按时间顺序追加事件 |

类比：
- Context Window = **会议桌上的材料**（每轮可以换、可以摘录压缩）
- Session = **档案室里的原始记录**（长期保存，随时可查）

### 为什么 Session 必须独立

1. **恢复能力**：Harness 挂了 → 新 Harness 从 Session 读取历史 → 继续执行
2. **历史完整性**：原始细节不会因为摘录和压缩被永久丢掉
3. **跨版本兼容**：未来新的 Harness / 新的模型能力，都能回头拿到这些信息
4. **按需加载**：不需要每次把全部历史整包加载到 Context Window，可以只读某一段

### Session 存储引擎无关

```
Session 底层可以用：
├── PostgreSQL    — 强一致，适合长期任务
├── Redis / SQLite — 快速读写
├── Append-only 文件 — 极简模式
└── 任何 AppendOnly 存储

只要实现 Wake / Get / Emit 四个接口，底层可以随时切换。
```

---

## 新增组件

### SessionStore (`core/session_store.py`)

**角色**: Session 的外部持久层。Brain 层和 Execution 层完全不感知底层存储引擎。

```python
store = SessionStore()  # 可注入: SQLite / Redis / PostgreSQL / FileStore

# 四个核心接口（稳定不变）
await store.create_session(project_id, task_description)  # 创建并返回 session_id
await store.wake(session_id)              # 唤醒任务
await store.get_session(sid)             # 获取完整历史
await store.get_events(sid, ...)         # 按需切片读取
await store.emit_event(sid, event)        # 追加新事件
await store.suspend(sid)                  # 暂停任务
```

### ExecutionEngine (`core/execution_engine.py`)

**角色**: Brain 层和 Execution 层之间的稳定接口。上面只关心"要不要动手"和"结果回来了没有"，下面具体实现可随时替换。

```python
engine = ExecutionEngine()

# Provision: 准备好本次任务的执行环境和资源
unit_id = await engine.provision(context=ProvisionContext(
    session_id="...",
    task_description="...",
    resources=["src/"],
    sandbox_type="local",
))

# Execute: 执行一个动作
result = await engine.execute(session_id="...", tool="write_file", input_data={
    "path": "src/main.py",
    "content": "...",
})

# 执行单元挂了 → 重新 Provision 一个，继续 Execute
# 不再是"整个任务现场塌了"

# 内置工具：write_file / read_file / list_directory / run_command / delete_file / make_directory
```

### GitHub Tools (`infrastructure/github_tools.py`)

**角色**: 让 Agent 能访问 GitHub，搜索参考项目、分析开源代码、自动改进生成质量。

**8 个工具**：

| 工具 | 用途 | Agent 使用场景 |
|------|------|--------------|
| `github_search_repos` | 搜索仓库 | "找 FastAPI 认证的最佳参考项目" |
| `github_search_code` | 搜索代码 | "找 JWT refresh token 的实现方式" |
| `github_get_file` | 读取文件 | "分析 xxx 项目的用户模型实现" |
| `github_get_repo_info` | 仓库概览 | "了解这个项目的活跃度和规模" |
| `github_list_commits` | 提交历史 | "了解项目的开发节奏" |
| `github_get_readme` | 读 README | "快速了解陌生项目的用途" |
| `github_list_issues` | Issues 列表 | "了解项目的已知问题和社区反馈" |
| `github_analyze_repo_structure` | 目录结构 | "了解陌生项目的代码组织方式" |

**使用方式**：

```python
from infrastructure.github_tools import GitHubTools
from codeforge import HarnessConfig

# 方式A：通过 HarnessConfig 配置
config = HarnessConfig(
    github_tools=GitHubTools(),  # 自动读取 GITHUB_TOKEN 环境变量
)

# 方式B：附加到 ExecutionEngine
from core.execution_engine import ExecutionEngine
engine = ExecutionEngine()
engine = engine.with_github_tools(GitHubTools())

# Agent 在开发时会自动调用 GitHub 工具，例如：
# "去 GitHub 搜一下 FastAPI 用户认证的最佳实践，
#  然后参考 xxx 项目的实现方式来写我们的代码"
```

### DiagnosticAgent (`agents/diagnostic.py`)

**角色**: 在构建 Harness 之前诊断模型的真实失败模式

**核心方法**:
```python
report = await diagnostic_agent.diagnose(context)
# report.failure_modes      — 识别的失败模式
# report.capabilities        — 模型各维度能力评分
# report.components_needed   — 推荐的最小必要组件
```

### EvaluatorAgent (`agents/evaluator.py`)

**角色**: 分离的挑剔评估器（Generator/Evaluator Pattern）

**与 ReviewerAgent 的区别**:
- `ReviewerAgent`: YES/NO 门控审查（二元决策）
- `EvaluatorAgent`: 详细评分 + 具体问题（迭代改进）

**评分维度**:
- Completeness (完成度)
- Correctness (正确性)
- Security (安全性)
- Maintainability (可维护性)

### HarnessRegistry (`core/harness_registry.py`)

**角色**: 追踪每个组件的"承重"状态

```python
registry = HarnessRegistry()
registry.register(HarnessComponent(
    name="context_reset",
    purpose="防止上下文焦虑导致的质量下降",
    addresses_gap="context_anxiety",
    added_at_version="claude-opus-4.6",
))
```

**关键方法**:
- `list_stale()`: 哪些组件是旧版本添加的？
- `suggest_ablation_targets()`: 下次应该测试移除哪个组件？
- `can_remove()`: 某组件现在可以安全移除了吗？

### AblationEngine (`core/ablation_engine.py`)

**角色**: 运行消融实验，量化每个组件的贡献

```python
engine = AblationEngine(registry)
experiment = engine.run_experiment(
    component=comp,
    baseline_score=75,  # 有组件时的质量分
    removed_score=73,   # 无组件时的质量分
    model_version="claude-opus-4.6",
)
# experiment.verdict: "pass" | "fail" | "inconclusive"
```

---

## Agent 角色

| Agent | 角色 | 职责 |
|-------|------|------|
| `RequirementAgent` | 需求分析 | 将自然语言需求解析为结构化规范 |
| `ArchitectAgent` | 架构设计 | 设计系统架构和文件结构 |
| `CoderAgent` | 代码生成 | 根据架构生成完整代码 |
| `DiagnosticAgent` | **诊断** (Step 1) | 发现模型在当前任务中的失败模式 |
| `EvaluatorAgent` | **评估** (Step 2) | 分离的挑剔评估器，提供详细评分 |
| `ReviewerAgent` | 审查 | YES/NO + 置信度评分 |
| `TesterAgent` | 测试生成 | 生成 pytest 测试 |
| `DocumenterAgent` | 文档生成 | 生成 README 和 API 文档 |

---

## CLI 命令

```bash
# 完整开发（启用 Harness 4步循环）
codeforge develop requirements.md -o output/

# 禁用特定阶段
codeforge develop requirements.md --no-diagnostic      # 跳过诊断
codeforge develop requirements.md --no-verification    # 跳过验证
codeforge develop requirements.md --no-ablation        # 跳过消融

# 指定模型版本（用于消融跟踪）
codeforge develop requirements.md --model-version "claude-opus-4.6"

# 仅分析需求
codeforge analyze "开发一个博客系统"

# 继续开发（从检查点/Session 恢复）
codeforge continue checkpoint.json
```

---

## Harness 注册表

### 注册表生命周期

```
模型 v1.0                    模型 v2.0                    模型 v3.0
   │                           │                           │
   ▼                           ▼                           ▼
[添加组件A] ──────────> [A已过时] ──────────> [A已移除]
  gap=规划漂移               confidence=80%               status=removed
  confidence=100%           status=pending_removal
                              │
                              ▼
                       [添加组件B]
                         gap=创意停滞
                         confidence=100%
```

### 组件状态

| 状态 | 含义 |
|------|------|
| `active` | 正在使用，置信度高 |
| `experimental` | 模型升级后需要重新评估 |
| `pending_removal` | 消融通过，可以移除 |
| `removed` | 已移除 |

---

## 消融实验

### 运行时机

1. **周期性检查**: 每个项目完成后，检查是否有需要消融的组件
2. **模型升级后**: 当模型版本变化时，所有旧版本组件标记为 `experimental`
3. **按需触发**: 通过 CLI 或 API 手动触发

### 消融优先级算法

```python
score = (
    +0.5 if never_ablated
    +0.3 if last_result_was_pass
    +0.3 if low_confidence
    +0.4 if added_under_old_version
    +0.2 if avg_duration > 30s
)
# 选择得分最高的候选组件进行消融
```

### 消融阈值

| 质量变化 | 判定 |
|---------|------|
| ≥ -5% | **PASS** — 可以移除（质量未显著下降） |
| -5% ~ -20% | **INCONCLUSIVE** — 不确定，需更多测试 |
| < -20% | **FAIL** — 仍需保留（质量显著下降） |

---

## 与原架构对比

| 维度 | 原架构 | 新架构 (三层分离) |
|------|--------|------------------|
| **Brain / Execution 关系** | 耦合在同一进程 | Brain 留平台侧，Execution 按需唤起 |
| **Session 存储** | 嵌在 Harness 进程内存中 | 独立外部持久层，存储引擎无关 |
| **故障恢复** | 进程崩溃 = 任务丢失 | Execution 挂了只是一次可处理的失败 |
| **安全边界** | Agent 和密钥在同一环境 | Token 不进 Execution 层 |
| **Context Window** | 和 Session 混用 | 明确分离：档案室 vs 会议桌 |
| **扩展方式** | 单容器扩展 | Brain 和 Execution 各自独立扩展 |
| **生成器/评估器** | 单一 CoderAgent | CoderBrain + EvaluatorBrain 分离 |
| **验证** | 无 | 真实任务验证（evaluation_score） |
| **简化** | 无 | 消融实验持续做减法 |
| **组件管理** | 静态配置 | 动态注册表，随模型进化 |

### 关键洞察

> **原架构的问题**: 一旦某套 Harness 有效，就永远保留它。随着时间推移，Harness 越来越臃肿、越来越慢、越来越贵。加上 Brain/Execution/Session 耦合在同一进程，任何一层挂了都会拖垮整个任务。

> **三层分离的核心改变**: 把"有效性"变成"必要性"的追问，同时建立故障隔离。不是"这个 Harness 有没有用"，而是"这个组件是否还在承重，还是已经变成了临时支架？Execution 挂了会不会让我丢失任务？"

---

## 代码示例

### 默认用法（向后兼容）

```python
from codeforge import CodeForgeHarness, HarnessConfig, LLMManager
from pathlib import Path

config = HarnessConfig(
    project_root=Path("output/my_project"),
    enable_diagnostic=True,      # Step 1: 诊断缺口
    enable_evaluator=True,       # Step 2: 分离评估器
    enable_verification=True,     # Step 3: 真实验证
    enable_ablation=True,         # Step 4: 消融实验
    model_version="claude-opus-4.6",
    auto_fix=True,
)

llm = LLMManager()
harness = CodeForgeHarness(config=config, llm_manager=llm)

context = await harness.develop(
    requirements="开发一个用户管理 REST API...",
    project_name="user-api",
    project_type="api",
)

# 检查消融建议
registry = harness.harness_registry
print(registry.format_summary())
```

### 启用 Session 层（任务持久化 + 中断恢复）

```python
from codeforge import CodeForgeHarness, HarnessConfig
from core.session_store import SQLiteSessionStore

session_store = SQLiteSessionStore(db_path="output/.codeforge/sessions.db")
config = HarnessConfig(
    project_root=Path("output/my_project"),
    session_store=session_store,  # ← 启用 Session 持久化
)

harness = CodeForgeHarness(config=config)
context = await harness.develop(
    requirements="开发一个用户管理 REST API...",
    project_name="user-api",
)
session_id = harness._current_session.session_id

# Harness 崩溃后，从 Session 恢复继续执行
harness2 = CodeForgeHarness(config=config)
context2 = await harness2.develop(
    requirements="开发一个用户管理 REST API...",
    project_name="user-api",
    session_id=session_id,  # ← 恢复同一个任务
)
```

### 三层全启用（生产级）

```python
from codeforge import CodeForgeHarness, HarnessConfig
from core.session_store import SQLiteSessionStore
from core.execution_engine import ExecutionEngine

config = HarnessConfig(
    project_root=Path("output/my_project"),
    session_store=SQLiteSessionStore(db_path="output/.codeforge/sessions.db"),
    execution_engine=ExecutionEngine(max_retries=2),
    model_version="claude-opus-4.6",
)

harness = CodeForgeHarness(config=config)
context = await harness.develop(
    requirements="开发一个用户管理 REST API...",
    project_name="user-api",
    project_type="api",
)
```

---

## 文件结构

```
codeforge/
├── agents/                         # Brain 层（平台侧）
│   ├── architect.py                # 架构设计 Brain
│   ├── coder.py                    # 代码生成 Brain
│   ├── diagnostic.py               # 诊断 Brain (Step 1)
│   ├── documenter.py              # 文档生成 Brain
│   ├── evaluator.py                # 分离评估 Brain (Step 2)
│   ├── requirement.py              # 需求分析 Brain
│   ├── reviewer.py                 # 审查 Brain (YES/NO + 置信度)
│   └── tester.py                   # 测试生成 Brain
├── core/
│   ├── session_store.py            # 【NEW】Session 外部持久层
│   ├── execution_engine.py         # 【NEW】Execution 稳定接口 + GitHub 工具路由
│   ├── ablation_engine.py          # 消融实验引擎 (Step 4)
│   ├── agent.py                    # Agent 基类
│   ├── harness.py                  # 主 Harness（Brain 编排逻辑）
│   ├── harness_registry.py          # 组件注册表（承重状态追踪）
│   ├── llm_client.py              # 多模型 LLM 客户端
│   ├── state.py                   # 项目上下文
│   └── types.py                   # 类型定义
├── cli/
│   └── __init__.py               # CLI 入口
└── infrastructure/
    ├── git_manager.py            # Git 版本管理
    ├── github_tools.py             # 【NEW】GitHub API 工具集
    └── logging.py                 # 结构化日志
```

---

*本文档基于 Anthropic Scaling Managed Agents 架构方法论优化 CodeForge | CodeForge v0.4.0*
