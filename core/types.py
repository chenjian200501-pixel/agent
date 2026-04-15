"""Core type definitions for the CodeForge harness."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Literal


class TaskState(Enum):
    PENDING = auto()
    RUNNING = auto()
    WAITING_APPROVAL = auto()
    CHECKPOINTING = auto()
    SUSPENDED = auto()
    COMPLETED = auto()
    FAILED = auto()


class AgentRole(Enum):
    REQUIREMENT = "requirement"
    ARCHITECT = "architect"
    CODER = "coder"
    REVIEWER = "reviewer"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    ORCHESTRATOR = "orchestrator"
    # Harness-specific roles
    DIAGNOSTIC = "diagnostic"    # Step 1: diagnose failure modes
    EVALUATOR = "evaluator"      # Separated critic/generator pattern


class LLMPreference(Enum):
    CLAUDE = "claude"
    GPT = "gpt"
    LOCAL = "local"


@dataclass
class AgentOutput:
    role: AgentRole
    content: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    token_usage: int = 0
    model: str = ""


@dataclass
class CodeArtifact:
    """A generated code file or snippet."""
    path: str
    language: str
    content: str
    description: str = ""
    is_test: bool = False
    is_doc: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    token_usage: int = 0


@dataclass
class ReviewComment:
    """A code review comment."""
    file_path: str
    message: str
    line: int | None = None
    severity: Literal["info", "warning", "error", "critical"] = "warning"
    suggestion: str | None = None
    rule: str | None = None


@dataclass
class RequirementSpec:
    """Structured requirement specification."""
    project_name: str
    project_type: str  # "api", "cli", "web", "microservice"
    summary: str
    features: list[str]
    tech_stack: dict[str, str]
    endpoints: list[dict] = field(default_factory=list)
    data_models: list[dict] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    raw_requirements: str = ""


@dataclass
class ArchitectureDesign:
    """System architecture specification."""
    overview: str
    components: list[dict]
    file_structure: dict[str, list[str]]  # dir -> files
    dependencies: list[dict]
    api_spec: dict | None = None
    database_schema: dict | None = None
    deployment_notes: list[str] = field(default_factory=list)


# ─── Session / Execution Layer Types ───────────────────────────────────────

class ExecutionStatus(Enum):
    """Status of an Execution action."""
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()
    RETRYING = auto()


class EventType(Enum):
    """Types of events in a Session event stream."""
    SESSION_CREATED = auto()
    BRAIN_STARTED = auto()
    BRAIN_STOPPED = auto()
    PLAN_CREATED = auto()
    EXECUTION_STARTED = auto()
    EXECUTION_SUCCESS = auto()
    EXECUTION_FAILED = auto()
    VERIFICATION_STARTED = auto()
    VERIFICATION_PASSED = auto()
    VERIFICATION_FAILED = auto()
    TASK_COMPLETED = auto()
    TASK_FAILED = auto()


@dataclass
class SessionEvent:
    """A single event in the Session event stream.

    This is the atomic unit of truth — every step forward in the task
    is recorded as an event. The Session can be replayed by reading events
    in order, or queried selectively.
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    event_type: EventType = EventType.BRAIN_STOPPED
    agent_role: AgentRole | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Optional: link to parent event (for grouping)
    parent_id: str | None = None
    # Outcome metadata
    success: bool = True
    error_message: str | None = None


@dataclass
class ExecutionResult:
    """Result of an Execution call."""
    status: ExecutionStatus
    tool: str
    input_data: dict[str, Any]
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    execution_unit_id: str | None = None


@dataclass
class ProvisionContext:
    """Context passed to Execution.provision()."""
    session_id: str
    task_description: str
    resources: list[str] = field(default_factory=list)  # file paths, URLs, etc.
    environment_vars: dict[str, str] = field(default_factory=dict)
    sandbox_type: Literal["local", "container", "mcp"] = "local"
