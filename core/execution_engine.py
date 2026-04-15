"""Execution Engine — the stable interface between Brain (Harness) and execution layer.

This implements the first structural split from Anthropic's approach:
separating the thinking/orchestrating layer (Brain) from the doing layer (Execution).

Core philosophy:
- Brain stays on the platform side, always alive.
- Execution units are spawned on demand and can be replaced freely.
- The interface between them is two operations:

    provision(context)  — Prepare execution environment + resources for this task
    execute(tool, input) — Run a single action and return the result

If an execution unit fails, Brain sees it as a single tool-call failure,
not a catastrophic task loss. The system re-provisions and retries.

The underlying execution backend is injectable:
- LocalProcessExecution (default: run in local subprocess)
- ContainerExecution (run in Docker/container)
- MCPServerExecution (run via MCP protocol)
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.types import ExecutionResult, ExecutionStatus, ProvisionContext, SessionEvent


# ─── Tool Definitions ────────────────────────────────────────────────────────

@dataclass
class Tool:
    """A tool available to the execution layer."""
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema for the input
    output_schema: dict[str, Any] | None = None


# Built-in tools available to all execution backends
BUILTIN_TOOLS: list[Tool] = [
    Tool(
        name="write_file",
        description="Write content to a file. Creates parent directories if needed.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    ),
    Tool(
        name="read_file",
        description="Read the content of a file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "max_lines": {"type": "integer", "description": "Maximum number of lines to read"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="list_directory",
        description="List files and directories at a path.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
                "recursive": {"type": "boolean", "description": "List recursively"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="run_command",
        description="Run a shell command.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "cwd": {"type": "string", "description": "Working directory"},
                "timeout": {"type": "integer", "description": "Timeout in seconds"},
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="delete_file",
        description="Delete a file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="make_directory",
        description="Create a directory (and parents if needed).",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    ),
]


# ─── Abstract Execution Backend ─────────────────────────────────────────────

class ExecutionBackend(ABC):
    """Abstract execution backend — the actual implementation is injectable.

    The stable interface from Brain's perspective is:
        provision(context)  → prepare environment
        execute(tool, input) → run one action
        teardown()          → clean up

    The backend can be: local process, Docker container, MCP server, etc.
    """

    @abstractmethod
    async def provision(self, context: ProvisionContext) -> str:
        """Prepare the execution environment for a task.

        Returns an execution_unit_id that identifies this provisioned environment.
        If the environment is already provisioned for this task, return existing ID.
        """
        ...

    @abstractmethod
    async def execute(self, execution_unit_id: str, tool: str, input_data: dict[str, Any]) -> ExecutionResult:
        """Execute a single tool call in the given execution unit."""
        ...

    @abstractmethod
    async def teardown(self, execution_unit_id: str) -> None:
        """Tear down a specific execution unit.

        Called when an execution unit is no longer needed or has failed.
        The system will provision a new one for subsequent actions.
        """
        ...

    @abstractmethod
    def available_tools(self) -> list[Tool]:
        """Return the list of tools this backend supports."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the backend is healthy and ready."""
        ...


# ─── Local Process Backend (default) ────────────────────────────────────────

class LocalProcessExecution(ExecutionBackend):
    """Default execution backend: runs tools in local subprocesses.

    Simple, zero-dependency, suitable for local development.
    """

    def __init__(self, workspace_root: Path | str = "output"):
        self.workspace_root = Path(workspace_root)
        self._units: dict[str, dict[str, Any]] = {}  # execution_unit_id -> state

    async def provision(self, context: ProvisionContext) -> str:
        unit_id = f"local-{uuid.uuid4().hex[:8]}"

        # Set up workspace
        workspace = self.workspace_root / f".execution/{unit_id}"
        workspace.mkdir(parents=True, exist_ok=True)

        self._units[unit_id] = {
            "id": unit_id,
            "workspace": workspace,
            "context": context,
            "created_at": time.time(),
        }

        # Load resources (copy/link project files into workspace)
        # Only copy files, not the parent directory structure
        for resource in context.resources:
            src = Path(resource)
            if src.exists() and src.is_dir():
                import shutil
                # Copy directory contents into workspace, not the directory itself
                for item in src.rglob("*"):
                    if item.is_file():
                        # Skip hidden directories (.git, .pytest_cache, etc.)
                        if any(p.startswith(".") for p in item.parts):
                            continue
                        rel = item.relative_to(src)
                        dst = workspace / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        # Use binary mode to handle all file types (text and binary)
                        dst.write_bytes(item.read_bytes())

        return unit_id

    async def execute(self, unit_id: str, tool: str, input_data: dict[str, Any]) -> ExecutionResult:
        if unit_id not in self._units:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                tool=tool,
                input_data=input_data,
                error=f"Unknown execution unit: {unit_id}",
                duration_ms=0,
                execution_unit_id=unit_id,
            )

        unit = self._units[unit_id]
        workspace = unit["workspace"]
        start = time.time()

        try:
            if tool == "write_file":
                path = workspace / input_data["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                content = input_data["content"]
                if isinstance(content, str):
                    path.write_text(content, encoding="utf-8")
                else:
                    path.write_bytes(content)
                output = {"path": str(path), "bytes": len(content) if hasattr(content, "__len__") else 0}

            elif tool == "read_file":
                path = workspace / input_data["path"]
                if input_data.get("max_lines"):
                    lines = path.read_text().splitlines()[:input_data["max_lines"]]
                    content = "\n".join(lines)
                else:
                    content = path.read_text()
                output = {"path": str(path), "content": content, "lines": len(content.splitlines())}

            elif tool == "list_directory":
                path = workspace / input_data.get("path", ".")
                recursive = input_data.get("recursive", False)
                if recursive:
                    entries = [str(p.relative_to(workspace)) for p in path.rglob("*")]
                else:
                    entries = [e.name for e in path.iterdir()]
                output = {"path": str(path), "entries": entries}

            elif tool == "run_command":
                cwd = workspace / input_data.get("cwd", ".")
                timeout = input_data.get("timeout", 60)
                result = subprocess.run(
                    input_data["command"],
                    shell=True,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                output = {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                }

            elif tool == "delete_file":
                path = workspace / input_data["path"]
                path.unlink(missing_ok=True)
                output = {"deleted": True}

            elif tool == "make_directory":
                path = workspace / input_data["path"]
                path.mkdir(parents=True, exist_ok=True)
                output = {"path": str(path)}

            else:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    tool=tool,
                    input_data=input_data,
                    error=f"Unknown tool: {tool}",
                    duration_ms=(time.time() - start) * 1000,
                    execution_unit_id=unit_id,
                )

            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                tool=tool,
                input_data=input_data,
                output=output,
                duration_ms=(time.time() - start) * 1000,
                execution_unit_id=unit_id,
            )

        except subprocess.TimeoutExpired:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                tool=tool,
                input_data=input_data,
                error="Command timed out",
                duration_ms=(time.time() - start) * 1000,
                execution_unit_id=unit_id,
            )
        except Exception as e:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                tool=tool,
                input_data=input_data,
                error=str(e),
                duration_ms=(time.time() - start) * 1000,
                execution_unit_id=unit_id,
            )

    async def teardown(self, unit_id: str) -> None:
        if unit_id in self._units:
            unit = self._units.pop(unit_id)
            workspace = unit.get("workspace")
            if workspace and workspace.exists():
                import shutil
                shutil.rmtree(workspace.parent, ignore_errors=True)

    def available_tools(self) -> list[Tool]:
        return BUILTIN_TOOLS

    async def health_check(self) -> bool:
        return True

    def with_github_tools(self, github_tools: "GitHubTools") -> "LocalProcessExecution":
        """Attach GitHub tools to this execution backend.

        Example:
            backend = LocalProcessExecution()
            backend = backend.with_github_tools(GitHubTools())
        """
        self._github_tools = github_tools
        return self


# ─── Execution Engine Facade ─────────────────────────────────────────────────

class ExecutionEngine:
    """Facades the execution backend and provides the stable interface to Brain.

    Brain only knows two things:
    - Should I execute this action? (provision first if needed)
    - Did the action succeed? (ExecutionResult)

    The underlying backend can be swapped at any time without changing Brain.

    GitHub tools are also supported — if `github_tools` is set,
    tools prefixed with "github_" are routed to the GitHub tools layer
    (no sandbox needed, direct API call).
    """

    def __init__(
        self,
        backend: ExecutionBackend | None = None,
        workspace_root: Path | str = "output",
        max_retries: int = 2,
        github_tools: "GitHubTools | None" = None,
    ):
        self.backend = backend or LocalProcessExecution(workspace_root)
        self.workspace_root = Path(workspace_root)
        self.max_retries = max_retries
        self._current_unit_id: str | None = None
        self._retry_count: dict[str, int] = {}
        self._github_tools = github_tools

    async def provision(self, context: ProvisionContext) -> str:
        """Prepare execution environment for the given task context.

        If already provisioned for the same session, this is a no-op
        (the same unit is reused). If a different session, teardown old and provision new.
        """
        # If already provisioned for this session, reuse
        if self._current_unit_id and self._retry_count.get(f"{context.session_id}_unit") == self._current_unit_id:
            return self._current_unit_id

        # Teardown old unit if exists
        if self._current_unit_id:
            await self.backend.teardown(self._current_unit_id)

        unit_id = await self.backend.provision(context)
        self._current_unit_id = unit_id
        self._retry_count[context.session_id] = 0
        return unit_id

    async def execute(
        self,
        session_id: str,
        tool: str,
        input_data: dict[str, Any],
        context: ProvisionContext | None = None,
    ) -> ExecutionResult:
        """Execute a single tool call, with automatic retry on failure.

        Flow:
        1. Ensure execution environment is provisioned
        2. Call backend.execute()
        3. If failed and retries remaining → re-provision and retry
        4. Return ExecutionResult
        """
        if self._current_unit_id is None and context:
            await self.provision(context)

        retries_key = f"{session_id}_retry"
        current_retries = self._retry_count.get(retries_key, 0)
        unit_id = self._current_unit_id or ""

        result = await self.backend.execute(unit_id, tool, input_data)

        # GitHub tools: routed to GitHub layer (no sandbox needed)
        if result.status == ExecutionStatus.FAILED and tool.startswith("github_") and self._github_tools:
            gh_result = self._github_tools.call_tool(tool, **input_data)
            if gh_result.get("ok"):
                return ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    tool=tool,
                    input_data=input_data,
                    output=gh_result.get("data"),
                    duration_ms=0,
                )
            else:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    tool=tool,
                    input_data=input_data,
                    error=gh_result.get("error", "Unknown GitHub tool error"),
                    duration_ms=0,
                )

        # Retry logic: if execution failed, re-provision and retry
        if result.status == ExecutionStatus.FAILED and current_retries < self.max_retries:
            self._retry_count[retries_key] = current_retries + 1
            if context:
                unit_id = await self.provision(context)
            result = await self.backend.execute(unit_id, tool, input_data)

        return result

    async def teardown(self) -> None:
        """Teardown the current execution unit."""
        if self._current_unit_id:
            await self.backend.teardown(self._current_unit_id)
            self._current_unit_id = None

    async def health_check(self) -> bool:
        """Check if the execution backend is healthy."""
        return await self.backend.health_check()

    def tools(self) -> list[Tool]:
        """Return all tools available (built-in + GitHub)."""
        from infrastructure.github_tools import TOOLS as GITHUB_TOOLS
        base = self.backend.available_tools()
        github = GITHUB_TOOLS if self._github_tools else []
        return base + github
