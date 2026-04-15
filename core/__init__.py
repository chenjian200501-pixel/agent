"""Core framework for CodeForge."""

from core.session_store import (
    SessionStore,
    Session,
    SQLiteSessionStore,
)
from core.execution_engine import (
    ExecutionEngine,
    ExecutionBackend,
    LocalProcessExecution,
    Tool,
    BUILTIN_TOOLS,
)
from core.harness_registry import HarnessComponent, HarnessRegistry
from core.ablation_engine import AblationEngine, AblationReport
from core.state import ProjectContext, ConversationTurn
from core.types import (
    AgentOutput,
    AgentRole,
    ArchitectureDesign,
    CodeArtifact,
    EventType,
    ExecutionResult,
    ExecutionStatus,
    ProvisionContext,
    RequirementSpec,
    ReviewComment,
    SessionEvent,
    TaskState,
)
