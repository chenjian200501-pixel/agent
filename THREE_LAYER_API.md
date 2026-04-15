# Three-Layer Architecture — API 使用指南

> 本文档说明如何在 CodeForge v0.3.0 中使用新增的 Session 层和 Execution 层。

## 概览

```
Brain 层（平台侧，始终存活）
├── Harness (编排逻辑) ──── 不住在执行容器里
└── Agents (Diagnostic / Evaluator / Coder ...)

Session 层（外部持久）
├── Wake / GetSession / GetEvents / EmitEvent
└── 存储引擎：SQLite / Redis / PostgreSQL

Execution 层（按需唤起）
├── ExecutionEngine.provision() / execute()
└── 后端：LocalProcess / Docker / MCP
```

**默认行为**：Session 层和 Execution 层默认关闭，向后兼容原有代码。
**启用方式**：通过 `HarnessConfig` 配置。

---

## 1. Session 层

### 1.1 创建 Session 并开发

```python
from core.harness import CodeForgeHarness, HarnessConfig
from core.session_store import SQLiteSessionStore

# 方式A：用 SQLite 持久化 Session
session_store = SQLiteSessionStore(
    db_path="output/.codeforge/sessions.db"
)
config = HarnessConfig(
    project_root="output/my_project",
    session_store=session_store,
)
harness = CodeForgeHarness(config=config)

# 自动创建 Session，任务历史持久化到 SQLite
context = await harness.develop(
    requirements="开发一个博客 REST API",
    project_name="blog-api",
    project_type="api",
)
print(f"Session ID: {harness._current_session.session_id}")
# Session ID: blog-api-session-a1b2c3d4
```

### 1.2 从 Session 恢复任务

```python
# Harness 崩溃后，用 Session ID 恢复
session_id = "blog-api-session-a1b2c3d4"

context = await harness.develop(
    requirements="开发一个博客 REST API",
    project_name="blog-api",
    session_id=session_id,  # ← 传入 Session ID
)

# 内部逻辑：
# 1. 从 SQLite 读取 Session 历史
# 2. 读取已执行的事件流
# 3. 新的 Harness 接手，继续执行
```

### 1.3 读取 Session 历史

```python
from core.types import EventType

# 获取完整 Session
session = await session_store.get_session(session_id)
print(f"事件数: {session.event_count}")
for event in session.events:
    print(f"  [{event.event_type.name}] {event.timestamp}")

# 按范围查询（大型 Session 的分页加载）
events = await session_store.get_events(
    session_id,
    start_index=0,
    limit=20,
    event_types=[EventType.EXECUTION_FAILED],
)

# 获取摘要（不含事件流，用于列表展示）
summary = await session_store.get_session_summary(session_id)
print(summary)
# {'session_id': '...', 'status': 'completed', 'event_count': 47, ...}

# 列出所有 Session
sessions = await session_store.list_sessions(status="completed")
for s in sessions:
    print(f"  {s['session_id']} - {s['task_description'][:50]}")
```

### 1.4 Session 事件类型

```python
from core.types import EventType

# 所有可记录的事件类型
EventType.SESSION_CREATED      # Session 被创建
EventType.BRAIN_STARTED        # Brain 启动（新建或恢复）
EventType.PLAN_CREATED         # 执行计划生成
EventType.EXECUTION_STARTED    # 执行单元启动
EventType.EXECUTION_SUCCESS    # 单次执行成功
EventType.EXECUTION_FAILED     # 单次执行失败
EventType.VERIFICATION_PASSED  # 验证通过
EventType.TASK_COMPLETED      # 任务完成
EventType.TASK_FAILED         # 任务失败
EventType.BRAIN_STOPPED       # Brain 停止
```

### 1.5 注入不同的存储后端

```python
# 方式A：Redis 后端（需要实现 RedisSessionStore）
from core.session_store import SessionStore

class RedisSessionStore(SessionStore):
    async def create_session(self, project_id: str, task_description: str) -> str:
        # 实现 Redis 存储
        ...

session_store = RedisSessionStore(host="localhost", port=6379)
config = HarnessConfig(session_store=session_store)

# 方式B：PostgreSQL 后端
class PostgresSessionStore(SessionStore):
    async def create_session(self, project_id: str, task_description: str) -> str:
        # 实现 PostgreSQL 存储
        ...

session_store = PostgresSessionStore(conn_string="postgresql://...")
config = HarnessConfig(session_store=session_store)
```

---

## 2. Execution 层

### 2.1 启用 Execution 层

```python
from core.harness import CodeForgeHarness, HarnessConfig
from core.execution_engine import ExecutionEngine, LocalProcessExecution

# 方式A：使用默认的本地进程执行引擎
config = HarnessConfig(
    project_root="output/my_project",
    execution_engine=ExecutionEngine(workspace_root="output/my_project"),
)
harness = CodeForgeHarness(config=config)

# 方式B：自定义后端
config = HarnessConfig(
    project_root="output/my_project",
    execution_engine=ExecutionEngine(
        backend=LocalProcessExecution(workspace_root="output/my_project"),
        max_retries=3,  # 执行失败后重试次数
    ),
)
```

---

## 4. GitHub 工具

### 启用 GitHub 工具

```python
from infrastructure.github_tools import GitHubTools
from codeforge import HarnessConfig

# 方式A：通过 HarnessConfig（推荐）
config = HarnessConfig(
    github_tools=GitHubTools(),  # 自动读取 GITHUB_TOKEN 环境变量
)

# 方式B：附加到 ExecutionEngine
from core.execution_engine import ExecutionEngine
engine = ExecutionEngine()
engine = engine.with_github_tools(GitHubTools(token="ghp_xxx"))
```

### 可用工具一览

| 工具名 | 用途 |
|--------|------|
| `github_search_repos` | 搜索仓库（按 stars/forks/更新时间排序） |
| `github_search_code` | 在代码中搜索（找具体实现片段） |
| `github_get_file` | 读取仓库文件内容（自动解码 base64） |
| `github_get_repo_info` | 获取仓库概览（stars、forks、语言、描述） |
| `github_list_commits` | 列出最近提交（了解开发节奏） |
| `github_get_readme` | 获取 README 内容（快速了解项目） |
| `github_list_issues` | 列出 Issues（了解已知问题和社区反馈） |
| `github_analyze_repo_structure` | 分析目录结构（了解陌生项目组织方式） |

### Agent 使用示例

Agent 在开发时会自动调用 GitHub 工具，例如：

```
User: 开发一个用户认证模块，要支持 JWT + Refresh Token

Agent 思考：
  1. 先搜一下 GitHub 上 FastAPI + JWT 的最佳参考项目
  → 调用 github_search_repos(query="FastAPI JWT authentication", language="python", sort="stars")

  2. 分析排名第一的项目的认证实现
  → 调用 github_get_file(owner="tiangolo", repo="fastapi", path="app/security.py")

  3. 参考该实现，结合用户需求，生成代码
```

### 直接调用（不通过 Agent）

```python
from infrastructure.github_tools import GitHubTools

tools = GitHubTools()  # 需要设置 GITHUB_TOKEN 环境变量

# 搜索参考仓库
result = tools.search_repos(
    query="FastAPI authentication JWT",
    language="python",
    sort="stars",
)
print(tools.summarize_for_agent(result))
# 输出:
# 🔍 找到 5 个仓库:
#   ⭐ 12,345 tiangolo/fastapi
#      FastAPI framework, high-performance...
#   ⭐ 3,456...

# 读取文件内容
result = tools.get_file(
    owner="tiangolo",
    repo="fastapi",
    path="app/security.py",
)
print(tools.summarize_for_agent(result))
# 输出:
# 📄 app/security.py
# SHA: abc123
# [文件内容...]
```

### 环境变量

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

GitHub Tools 会自动使用 `GITHUB_TOKEN`，未设置时也可以使用公开 API（速率限制更严格）。

### 2.2 可用工具

```python
from core.execution_engine import BUILTIN_TOOLS, Tool

for tool in BUILTIN_TOOLS:
    print(f"{tool.name}: {tool.description}")
```

| 工具 | 描述 |
|------|------|
| `write_file` | 写文件，自动创建父目录 |
| `read_file` | 读文件，支持 max_lines 限制 |
| `list_directory` | 列出目录内容，支持递归 |
| `run_command` | 运行 shell 命令，支持超时 |
| `delete_file` | 删除文件 |
| `make_directory` | 创建目录 |

### 2.3 直接调用 Execution Engine

```python
from core.execution_engine import ExecutionEngine
from core.types import ExecutionStatus, ProvisionContext

engine = ExecutionEngine()

# 1. Provision：准备执行环境
ctx = ProvisionContext(
    session_id="my-session",
    task_description="生成代码文件",
    resources=["output/my_project/src"],  # 要加载的资源
    sandbox_type="local",
)
unit_id = await engine.provision(ctx)
print(f"执行单元已启动: {unit_id}")

# 2. Execute：执行单个工具
result = await engine.execute(
    session_id="my-session",
    tool="write_file",
    input_data={
        "path": "app/main.py",
        "content": "from fastapi import FastAPI\napp = FastAPI()",
    },
)
print(f"状态: {result.status}")  # ExecutionStatus.SUCCESS
print(f"耗时: {result.duration_ms}ms")

# 3. 执行失败 → 自动重试 → 重新 Provision
# 自动重试最多 2 次（可配置 max_retries）

# 4. Teardown：清理执行单元
await engine.teardown()
```

### 2.4 执行结果处理

```python
from core.types import ExecutionStatus

result = await engine.execute(
    session_id="my-session",
    tool="run_command",
    input_data={"command": "pytest tests/", "timeout": 30},
)

if result.status == ExecutionStatus.SUCCESS:
    print(f"输出: {result.output}")
elif result.status == ExecutionStatus.FAILED:
    print(f"失败原因: {result.error}")
    print(f"执行单元: {result.execution_unit_id}")
```

### 2.5 自定义 Execution 后端

```python
from core.execution_engine import ExecutionBackend, ExecutionResult, ExecutionStatus, ProvisionContext

class DockerExecution(ExecutionBackend):
    """在 Docker 容器中执行工具调用"""

    async def provision(self, context: ProvisionContext) -> str:
        # 启动 Docker 容器
        container_id = await self._docker_run(context)
        return container_id

    async def execute(self, unit_id: str, tool: str, input_data: dict) -> ExecutionResult:
        # 在容器中执行命令
        result = await self._docker_exec(unit_id, tool, input_data)
        return result

    async def teardown(self, unit_id: str) -> None:
        await self._docker_stop(unit_id)

    def available_tools(self) -> list[Tool]:
        return BUILTIN_TOOLS  # 使用内置工具集

    async def health_check(self) -> bool:
        return await self._docker_ping()

# 使用自定义后端
engine = ExecutionEngine(backend=DockerExecution())
```

---

## 3. 完整示例：三层全启用

```python
from core.harness import CodeForgeHarness, HarnessConfig
from core.session_store import SQLiteSessionStore
from core.execution_engine import ExecutionEngine

# 1. 配置三层
session_store = SQLiteSessionStore(db_path="output/.codeforge/sessions.db")
execution_engine = ExecutionEngine(max_retries=2)

config = HarnessConfig(
    project_root="output/my_project",
    session_store=session_store,
    execution_engine=execution_engine,
    enable_diagnostic=True,
    enable_evaluator=True,
    enable_verification=True,
    enable_ablation=True,
    model_version="claude-opus-4-6",
)

# 2. 启动 Harness
harness = CodeForgeHarness(config=config)

# 3. 开发任务 → 自动创建 Session + 通过 Execution 层写入文件
context = await harness.develop(
    requirements="开发一个用户管理 REST API",
    project_name="user-api",
    project_type="api",
)

# 4. 检查 Session 事件流
session = await session_store.get_session(harness._current_session.session_id)
print(f"任务完成，共 {session.event_count} 个事件")

# 5. Harness 崩溃？用同一个 session_id 恢复
# 新 harness.develop(..., session_id="user-api-session-xxx")
# → 从上次失败的地方继续
```

---

## 5. 配置参考

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `session_store` | `SessionStore \| None` | `None` | Session 持久化后端 |
| `execution_engine` | `ExecutionEngine \| None` | `None` | Execution 执行引擎 |
| `github_tools` | `GitHubTools \| None` | `None` | GitHub API 工具，让 Agent 能搜索参考项目 |

**向后兼容**：
- 三个配置都是 `None` 时 → 使用原有行为（无 Session，无 Execution 层，无 GitHub 工具）
- 任一配置了 → 自动启用对应层

---

*本文档对应 CodeForge v0.4.0 — 基于 Anthropic Scaling Managed Agents 架构方法论*
