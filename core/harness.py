"""The main Harness — orchestrates all agents for code development.

Implements the Harness 4-Step Cycle (Anthropic methodology):
1. DIAGNOSE — Find model failure modes before building
2. MINIMUM PATCH — Add only necessary scaffolding for identified gaps
3. VERIFY — Run real tasks to confirm harness moves output from "looks done" to "actually deliverable"
4. ABLATE — Continuously test which components are still load-bearing vs. temporary scaffolding

Key philosophy: Every harness component encodes an assumption about what the
current model cannot do. These assumptions must be: (a) identified BEFORE
adding scaffolding, (b) periodically tested to see if the model has outgrown them.

The harness evolves with the model — components are added as gaps are found,
and removed as the model improves past those gaps.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.tree import Tree

from agents.architect import ArchitectAgent
from agents.coder import CoderAgent
from agents.diagnostic import DiagnosticAgent, DiagnosticReport
from agents.documenter import DocumenterAgent
from agents.evaluator import EvaluatorAgent, EvaluationResult
from agents.requirement import RequirementAgent
from agents.reviewer import ReviewerAgent, ReviewResult
from agents.tester import TesterAgent
from core.ablation_engine import AblationEngine, AblationReport
from core.harness_registry import HarnessComponent, HarnessRegistry
from core.llm_client import LLMManager
from core.session_store import Session, SessionStore, SQLiteSessionStore
from core.execution_engine import ExecutionEngine, LocalProcessExecution  # noqa: F401
from core.state import ProjectContext
from core.types import (
    AgentRole,
    CodeArtifact,
    EventType,
    ExecutionResult,
    ExecutionStatus,
    ProvisionContext,
    SessionEvent,
    TaskState,
)
from infrastructure.git_manager import GitManager
from infrastructure.logging import CodeForgeLogger, LogLevel


console = Console()


@dataclass
class HarnessConfig:
    """Configuration for the CodeForge harness.

    Extended with Harness 4-step cycle configuration:
    - enable_diagnostic: Run diagnostic phase before building (Step 1)
    - enable_evaluator: Use separated evaluator (generator/evaluator pattern)
    - enable_verification: Run verification phase after code generation (Step 3)
    - enable_ablation: Enable ablation experiments for continuous simplification (Step 4)
    - model_version: Current model version (for ablation tracking)

    Three-layer architecture (Anthropic Scaling Managed Agents approach):
    - session_store: External persistence for task event stream (Brain lives outside)
    - execution_engine: Stable interface between Brain and execution layer
    """
    project_root: Path = field(default_factory=lambda: Path("output"))
    git_enabled: bool = True
    git_auto_commit: bool = True
    git_auto_tag: bool = True
    checkpoint_enabled: bool = True
    auto_fix: bool = True
    max_fix_iterations: int = 3
    # YES/NO approval at each phase (None = auto, True = require, False = skip)
    require_architect_approval: bool | None = None
    require_coder_approval: bool | None = None
    require_final_review: bool = True
    log_level: LogLevel = LogLevel.INFO
    verbose: bool = False
    # Harness 4-step cycle options
    enable_diagnostic: bool = True    # Step 1: Diagnose gaps before building
    enable_evaluator: bool = True     # Use separated evaluator (not self-critique)
    enable_verification: bool = True  # Step 3: Verify output quality
    enable_ablation: bool = True      # Step 4: Test which components are obsolete
    model_version: str = "unknown"    # Current model for ablation tracking
    # Three-layer architecture (NEW — lazy init, opt-in)
    session_store: SessionStore | None = None  # Set to enable external Session persistence
    execution_engine: ExecutionEngine | None = None  # Set to enable Execution layer for file writes


# ---- Pipeline Phase Definition ----

@dataclass
class Phase:
    name: str
    description: str
    agent_key: str  # Key in self.agents dict
    requires_approval: bool = False
    can_skip: bool = False

    # Phase execution function signature:
    # async def fn(context) -> Any
    fn: Callable = field(default=None)
    args: tuple = field(default_factory=tuple)


class CodeForgeHarness:
    """
    The main orchestration harness for multi-agent code development.

    Implements the Harness 4-Step Cycle:
    1. DIAGNOSE (Step 1) — Identify model failure modes
    2. REQUIRE → ARCHITECT → GENERATE → EVALUATE (Step 2) — Minimum patches
    3. VERIFY (Step 3) — Confirm output is actually deliverable
    4. ABLATE (Step 4) — Test which components are obsolete

    Pipeline (with all phases):
    diagnostic → requirement → architecture → code_generation →
    [evaluator] → code_review → [verification] → test_generation → documentation →
    [ablation]

    Key difference from original: Each component's necessity is continuously
    questioned. The harness is never "done" — it evolves with the model.
    """

    # Full pipeline with all optional phases
    PIPELINE: list[Phase] = [
        Phase(
            name="diagnostic",
            description="Diagnosing model failure modes (Harness Step 1)",
            agent_key="diagnostic",
        ),
        Phase(
            name="requirement",
            description="Analyzing requirements",
            agent_key="requirement",
        ),
        Phase(
            name="architecture",
            description="Designing system architecture",
            agent_key="architect",
            requires_approval=False,
        ),
        Phase(
            name="code_generation",
            description="Generating code files",
            agent_key="coder",
            requires_approval=False,
        ),
        Phase(
            name="evaluator",
            description="Evaluating code quality (Harness Step 2: generator/evaluator separation)",
            agent_key="evaluator",
        ),
        Phase(
            name="code_review",
            description="Code review (YES/NO approval + confidence scoring)",
            agent_key="reviewer",
        ),
        # Fix loop is dynamic, not a fixed phase
        Phase(
            name="verification",
            description="Verifying output quality (Harness Step 3: actually deliverable?)",
            agent_key="verification",
        ),
        Phase(
            name="test_generation",
            description="Generating tests",
            agent_key="tester",
        ),
        Phase(
            name="documentation",
            description="Generating documentation",
            agent_key="documenter",
        ),
        Phase(
            name="ablation",
            description="Ablation experiment (Harness Step 4: testing obsolete components)",
            agent_key="ablation",
        ),
    ]

    def __init__(self, config: HarnessConfig | None = None, llm_manager: LLMManager | None = None):
        self.config = config or HarnessConfig()
        self.llm = llm_manager or LLMManager()
        self.console = console

        # Three-layer architecture initialization (lazy — only when config is set)
        # Session Store and Execution Engine are initialized on first use (in develop())
        self._session_store: SessionStore | None = None
        self._execution_engine: ExecutionEngine | None = None

        # Current session (set during develop())
        self._current_session: Session | None = None

        # Initialize all agents (optional ones gated by config flags)
        self.agents: dict = {
            "requirement": RequirementAgent(self.llm),
            "architect": ArchitectAgent(self.llm),
            "coder": CoderAgent(self.llm, max_concurrent=3),
            "reviewer": ReviewerAgent(self.llm),
            "tester": TesterAgent(self.llm),
            "documenter": DocumenterAgent(self.llm),
        }

        # Optional agents for Harness 4-step cycle
        if self.config.enable_diagnostic:
            from agents.diagnostic import DiagnosticAgent
            self.agents["diagnostic"] = DiagnosticAgent(self.llm)

        if self.config.enable_evaluator:
            from agents.evaluator import EvaluatorAgent
            self.agents["evaluator"] = EvaluatorAgent(self.llm)

        # Harness Registry: tracks which components are load-bearing vs. scaffolding
        self.harness_registry = HarnessRegistry()
        self.harness_registry.version = self.config.model_version

        # Ablation Engine: tests which components can be removed
        self.ablation_engine = AblationEngine(self.harness_registry)

        # Bind phase functions
        self._bind_phases()

        # Infrastructure
        self.git: GitManager | None = None
        self.logger: CodeForgeLogger | None = None

        # Hooks
        self._approval_hooks: list[Callable] = []

    def _bind_phases(self):
        """Bind phase names to their execution methods."""
        for phase in self.PIPELINE:
            fn_name = f"_phase_{phase.name}"
            if hasattr(self, fn_name):
                phase.fn = getattr(self, fn_name)

    # ---- Public API ----

    def on_approval(self, callback: Callable):
        """Register a callback for approval requests."""
        self._approval_hooks.append(callback)

    async def develop(
        self,
        requirements: str,
        project_name: str | None = None,
        project_type: str = "api",
        tech_stack: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> ProjectContext:
        """
        Main entry point: develop a complete project from requirements.

        Implements the Harness 4-step cycle + three-layer architecture:
        1. DIAGNOSE — Identify model failure modes
        2. REQUIRE → ARCHITECT → GENERATE → EVALUATOR → REVIEW — Build with minimum patches
        3. VERIFY — Confirm output is actually deliverable
        4. ABLATE — Test which components can be removed

        Three-layer flow:
        - Brain (this harness) stays on platform side, orchestrates all phases
        - Session persists task events externally — Brain crash = new Brain resumes from Session
        - Execution runs tools (write_file, run_command, etc.) on demand

        Args:
            requirements: Natural language project requirements
            project_name: Optional project name
            project_type: Type of project (api, cli, web, etc.)
            tech_stack: Optional tech stack override
            session_id: Optional. Resume from an existing session. If None, creates a new session.

        Returns:
            ProjectContext with all generated artifacts
        """
        # Initialize project
        context = ProjectContext(project_root=self.config.project_root)
        if project_name:
            context.shared["project_name"] = project_name
        context.state = TaskState.RUNNING

        # Store inputs
        context.shared["initial_requirements"] = requirements
        context.shared["project_type"] = project_type
        if tech_stack:
            context.shared["tech_stack_override"] = tech_stack

        proj_name = project_name or "untitled"

        # Initialize infrastructure
        self._init_infrastructure(context, proj_name)

        start_time = time.time()

        # Build the execution plan based on config
        execution_plan = self._build_execution_plan()

        # ── Lazy Init: Session Store and Execution Engine ───────────────────
        # Only initialize if configured (opt-in to preserve backward compatibility)
        if self._session_store is None and self.config.session_store is not None:
            self._session_store = self.config.session_store
        if self._execution_engine is None and self.config.execution_engine is not None:
            self._execution_engine = self.config.execution_engine

        # ── Session Layer: Create or Resume ───────────────────────────────────
        # Session layer is only active when session_store is configured
        if self._session_store is not None:
            if session_id:
                # Resume from existing session
                self._current_session = await self._session_store.get_session(session_id)
                if self._current_session:
                    await self._emit_event(
                        EventType.BRAIN_STARTED,
                        payload={"resumed_from": session_id, "project_name": proj_name},
                    )
                    self.console.print(f"[dim]Resuming session: {session_id}[/dim]")
                else:
                    self.console.print(f"[yellow]Session {session_id} not found, creating new[/yellow]")
                    session_id = await self._session_store.create_session(
                        project_id=proj_name, task_description=requirements[:200],
                    )
                    self._current_session = await self._session_store.get_session(session_id)
            else:
                # Create new session
                session_id = await self._session_store.create_session(
                    project_id=proj_name, task_description=requirements[:200],
                )
                self._current_session = await self._session_store.get_session(session_id)

            # Emit PLAN_CREATED — record the execution plan
            await self._emit_event(
                EventType.PLAN_CREATED,
                payload={"plan": list(execution_plan.keys())},
            )
        else:
            self._current_session = None
            session_id = None

        # Track whether execution has been provisioned
        execution_provisioned = False

        try:
            # STEP 1: DIAGNOSE — Find failure modes before building
            if "diagnostic" in execution_plan:
                await self._run_phase(
                    "diagnostic", context,
                    lambda ctx: self._phase_diagnostic(ctx),
                )
                self._log_harness_components(context)

            # STEP 2: REQUIREMENTS → ARCHITECTURE → GENERATE → EVALUATE → REVIEW
            phase_sequence = [
                ("requirement", lambda ctx: self._phase_requirement(ctx, requirements, project_type, tech_stack)),
                ("architecture", lambda ctx: self._phase_architecture(ctx)),
                ("code_generation", lambda ctx: self._phase_code_generation(ctx)),
            ]

            for phase_name, phase_fn in phase_sequence:
                if phase_name in execution_plan:
                    await self._run_phase(phase_name, context, phase_fn)

            if "evaluator" in execution_plan and self.config.enable_evaluator:
                await self._run_phase(
                    "evaluator", context,
                    lambda ctx: self._phase_evaluator(ctx),
                )

            if "code_review" in execution_plan:
                await self._run_phase(
                    "code_review", context,
                    lambda ctx: self._phase_code_review(ctx),
                )
                await self._handle_review_result(context)

            # STEP 3: VERIFY — Is output actually deliverable?
            if "verification" in execution_plan and self.config.enable_verification:
                await self._run_phase(
                    "verification", context,
                    lambda ctx: self._phase_verification(ctx),
                )

            if "test_generation" in execution_plan:
                await self._run_phase(
                    "test_generation", context,
                    lambda ctx: self._phase_test_generation(ctx),
                )

            if "documentation" in execution_plan:
                await self._run_phase(
                    "documentation", context,
                    lambda ctx: self._phase_documentation(ctx),
                )

            # STEP 4: ABLATE — Test which components can be removed
            if "ablation" in execution_plan and self.config.enable_ablation:
                await self._run_phase(
                    "ablation", context,
                    lambda ctx: self._phase_ablation(ctx),
                )

            if project_name and context.requirement_spec:
                context.requirement_spec.project_name = project_name

            # ── Execution Layer: Provision + write files ─────────────────────
            # Write files through Execution Engine when enabled
            if self.config.execution_engine is not None:
                if not execution_provisioned:
                    await self._provision_execution(context, session_id)
                    execution_provisioned = True
                await self._write_all_files_through_execution(context, session_id)
            else:
                # Fallback: write files directly (original behavior)
                await self._write_all_files(context)

            # Final git commit
            self._git_commit_phase("final", context, "All phases complete")

            context.state = TaskState.COMPLETED

            # Emit TASK_COMPLETED to Session
            await self._emit_event(
                EventType.TASK_COMPLETED,
                payload={
                    "project_name": proj_name,
                    "files_generated": len(context.generated_files),
                    "elapsed_seconds": time.time() - start_time,
                },
            )

            elapsed = time.time() - start_time
            self._print_summary(context, elapsed)

        except Exception as e:
            context.state = TaskState.FAILED
            context.shared["error"] = str(e)

            # Emit TASK_FAILED to Session — preserves full history for recovery
            await self._emit_event(
                EventType.TASK_FAILED,
                payload={
                    "error": str(e),
                    "phase": context.current_phase,
                    "elapsed_seconds": time.time() - start_time,
                },
                success=False,
                error_message=str(e),
            )

            self.logger.log_exception("pipeline", e, {"phase": context.current_phase})
            self.console.print(f"\n[red]Error: {e}[/red]")
            raise

        finally:
            # Emit BRAIN_STOPPED
            await self._emit_event(EventType.BRAIN_STOPPED)
            if self._execution_engine is not None:
                await self._execution_engine.teardown()
            self._finalize(context)

        return context

    # ─── Session / Execution Layer Helpers ───────────────────────────────────

    async def _emit_event(
        self,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        agent_role: AgentRole | None = None,
        success: bool = True,
        error_message: str | None = None,
    ) -> None:
        """Emit an event to the current Session.

        This is the core write interface — every forward step in the task
        is recorded as an event. If no session exists, this is a no-op.
        """
        if self._current_session is None or self._session_store is None:
            return

        event = SessionEvent(
            event_type=event_type,
            agent_role=agent_role,
            payload=payload or {},
            success=success,
            error_message=error_message,
        )
        await self._session_store.emit_event(self._current_session.session_id, event)

    async def _provision_execution(self, context: ProjectContext, session_id: str) -> None:
        """Provision the execution environment for this task.

        This is called lazily — only when we actually need to write files.
        The ExecutionEngine handles sandbox setup and resource loading.
        """
        if self._execution_engine is None:
            return
        provision_ctx = ProvisionContext(
            session_id=session_id,
            task_description=context.shared.get("initial_requirements", ""),
            resources=[str(self.config.project_root)],
            sandbox_type="local",
        )
        unit_id = await self._execution_engine.provision(provision_ctx)
        self.console.print(f"[dim]Execution provisioned: {unit_id}[/dim]")

    async def _write_all_files_through_execution(
        self, context: ProjectContext, session_id: str
    ) -> None:
        """Write all generated files through the Execution Engine.

        Instead of direct file I/O, files are written via the Execution layer.
        This means the write operation is recorded as an event in the Session,
        and if the execution unit fails, the operation can be retried.
        """
        if self._execution_engine is None:
            return
        all_files = context.generated_files + context.test_files + context.doc_files

        for artifact in all_files:
            # artifact.path is relative (e.g., "src/main.py")
            # workspace is already inside project_root, so write relative paths directly
            result = await self._execution_engine.execute(
                session_id=session_id,
                tool="write_file",
                input_data={
                    "path": artifact.path,
                    "content": artifact.content,
                },
            )

            if result.status == ExecutionStatus.FAILED:
                self.console.print(
                    f"[yellow]Warning: Failed to write {artifact.path}: {result.error}[/yellow]"
                )
            else:
                await self._emit_event(
                    EventType.EXECUTION_SUCCESS,
                    payload={
                        "tool": "write_file",
                        "path": artifact.path,
                        "bytes": len(artifact.content),
                    },
                )

    def _build_execution_plan(self) -> dict[str, bool]:
        """Build which phases to execute based on config.

        This makes the pipeline adaptive: components are only included
        if they address identified failure modes.
        """
        plan = {
            # Always run these core phases
            "requirement": True,
            "architecture": True,
            "code_generation": True,
            "code_review": True,
            # Optional Harness 4-step cycle phases
            "diagnostic": self.config.enable_diagnostic,
            "evaluator": self.config.enable_evaluator,
            "verification": self.config.enable_verification,
            "test_generation": True,
            "documentation": True,
            "ablation": self.config.enable_ablation,
        }
        return plan

    async def continue_project(
        self,
        checkpoint_path: Path,
        from_phase: str | None = None,
    ) -> ProjectContext:
        """
        Continue development from a checkpoint.

        Args:
            checkpoint_path: Path to the checkpoint JSON file
            from_phase: Optional phase to continue from (default: auto-detect)
        """
        from core.state import ProjectContext

        # Load checkpoint
        context = ProjectContext.load_checkpoint(checkpoint_path)
        context.project_root = self.config.project_root

        # Initialize infrastructure
        proj_name = context.requirement_spec.project_name if context.requirement_spec else "untitled"
        self._init_infrastructure(context, proj_name)

        start_time = time.time()

        try:
            # Determine which phase to resume from
            current = context.current_phase

            # Build phase order map
            phase_order = ["requirement", "architecture", "code_generation",
                          "code_review", "test", "document"]
            if from_phase:
                resume_idx = phase_order.index(from_phase)
            else:
                resume_idx = phase_order.index(current) if current in phase_order else 0

            # Load generated files from checkpoint data
            if checkpoint_path.exists():
                with open(checkpoint_path) as f:
                    cp_data = json.load(f)
                for item in cp_data.get("generated_files", []):
                    artifact = CodeArtifact(
                        path=item["path"],
                        language=item.get("language", ""),
                        content=item.get("content", ""),
                        description=item.get("description", ""),
                        is_test=item.get("is_test", False),
                        is_doc=item.get("is_doc", False),
                        metadata=item.get("metadata", {}),
                    )
                    context.generated_files.append(artifact)

            # Determine next phase after current
            next_phase = phase_order[resume_idx] if resume_idx < len(phase_order) else None

            if next_phase == "code_review":
                # Resume with review handling (fix loop)
                self.console.print(f"[yellow]Resuming from code_review fix loop[/yellow]")
                await self._run_phase("code_review", context,
                    lambda ctx: self._phase_code_review(ctx))
                await self._handle_review_result(context)
                await self._write_all_files(context)
            elif next_phase:
                # Resume from next phase
                self.console.print(f"[yellow]Resuming from phase: {next_phase}[/yellow]")
                for phase in self.PIPELINE:
                    phase_name = phase.name
                    if phase_name == next_phase or phase_order.index(phase_name) >= resume_idx:
                        await self._run_phase(
                            phase_name, context,
                            lambda ctx, pn=phase_name: self._execute_phase_by_name(ctx, pn),
                        )
                        if phase_name == "code_review":
                            await self._handle_review_result(context)

            context.state = TaskState.COMPLETED
            elapsed = time.time() - start_time
            self._print_summary(context, elapsed)

        except Exception as e:
            context.state = TaskState.FAILED
            context.shared["error"] = str(e)
            if self.logger:
                self.logger.log_exception("pipeline", e, {"phase": context.current_phase})
            self.console.print(f"\n[red]Error: {e}[/red]")
            raise

        finally:
            self._finalize(context)

        return context

    async def develop_from_requirements_file(
        self,
        requirements_file: Path,
        **kwargs,
    ) -> ProjectContext:
        """Convenience: develop from a requirements markdown/text file."""
        requirements = requirements_file.read_text(encoding="utf-8")
        return await self.develop(requirements, **kwargs)

    def rollback(self, phase: str | None = None, commit_hash: str | None = None) -> bool:
        """
        Rollback to a previous phase.

        Args:
            phase: Phase name to rollback to (e.g. "architecture")
            commit_hash: Specific commit hash to rollback to

        Returns:
            True if rollback succeeded
        """
        if not self.git or not self.git.is_repo():
            self.console.print("[red]Git not initialized[/red]")
            return False

        if commit_hash:
            return self.git.rollback_to_commit(commit_hash)
        elif phase:
            return self.git.rollback_to_phase(phase)
        else:
            # Rollback one commit
            return self.git.rollback_n_commits(1)

    def list_versions(self) -> list:
        """List all phase versions."""
        if not self.git:
            return []
        return self.git.list_versions()

    def get_logger(self) -> CodeForgeLogger | None:
        return self.logger

    # ---- Phase Execution ----

    async def _run_phase(
        self,
        phase_name: str,
        context: ProjectContext,
        fn: Callable,
    ):
        """Execute a single phase with full logging and git tracking."""
        context.current_phase = phase_name
        self.logger.start_phase(phase_name)
        self.console.print(f"\n[bold cyan]━━━ Phase: {phase_name} ━━━[/bold cyan]")

        start = time.time()

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=self.console,
            ) as progress:
                task = progress.add_task(f"[cyan]{phase_name}...", total=None)
                result = await fn(context)
                progress.update(task, completed=True, description=f"[green]✓ {phase_name}")

            duration_ms = (time.time() - start) * 1000
            self.logger.end_phase(phase_name, status="completed", metadata={
                "duration_ms": duration_ms,
            })

            self._git_commit_phase(phase_name, context)

        except Exception as e:
            self.logger.end_phase(phase_name, status="failed")
            self.logger.log_exception(phase_name, e)
            raise

    async def _execute_phase_by_name(self, context: ProjectContext, phase_name: str) -> Any:
        """Dispatch to the appropriate phase function by name."""
        # Try to find a _phase_{name} method
        fn_name = f"_phase_{phase_name}"
        if hasattr(self, fn_name):
            return await getattr(self, fn_name)(context)
        return None

    async def _execute_phase(self, context: ProjectContext, phase: Phase) -> Any:
        """Dispatch to the appropriate phase function."""
        if phase.fn and hasattr(phase.fn, "__call__"):
            return await phase.fn(context)
        # Fallback: call by name
        return await self._execute_phase_by_name(context, phase.name)

    # ---- Phase Implementations ----

    async def _phase_requirement(
        self,
        context: ProjectContext,
        requirements: str,
        project_type: str,
        tech_stack: dict | None,
    ) -> None:
        """Phase 1: Analyze requirements."""
        agent = self.agents["requirement"]
        spec = await agent.analyze(requirements, context)

        if project_type:
            spec.project_type = project_type
        if tech_stack:
            spec.tech_stack.update(tech_stack)

        self.logger.info("requirement", "spec_generated",
            f"Spec: {spec.project_name} ({len(spec.features)} features)")
        self.console.print(Panel(
            f"[bold]{spec.project_name}[/bold]\n"
            f"Type: {spec.project_type}\n"
            f"Features: {len(spec.features)}\n"
            f"Summary: {spec.summary[:80]}...",
            title="Requirements",
            border_style="cyan",
        ))

    async def _phase_architecture(self, context: ProjectContext) -> None:
        """Phase 2: Design architecture."""
        agent = self.agents["architect"]
        arch = await agent.design(context)

        self.logger.info("architecture", "design_complete",
            f"{len(arch.components)} components, {len(arch.file_structure)} directories")

        tree = Tree(f"[bold]Architecture: {context.requirement_spec.project_name}[/bold]")
        for comp in arch.components[:8]:
            tree.add(f"📦 {comp['name']}: {comp['responsibility']}")
        if len(arch.components) > 8:
            tree.add(f"... and {len(arch.components) - 8} more")
        self.console.print(tree)

    async def _phase_code_generation(self, context: ProjectContext) -> None:
        """Phase 3: Generate code."""
        agent = self.agents["coder"]
        artifacts = await agent.generate_all(context)

        by_dir: dict[str, list] = {}
        for a in artifacts:
            parts = a.path.split("/")
            d = parts[0] if len(parts) > 1 else "."
            by_dir.setdefault(d, []).append(a)

        file_tree = Tree("[bold]Generated Files[/bold]")
        for d, files in by_dir.items():
            dir_tree = file_tree.add(f"[blue]{d}/[/blue]")
            for f in files[:10]:
                icon = "🧪" if f.is_test else "📄"
                dir_tree.add(f"{icon} {f.path}")
            if len(files) > 10:
                dir_tree.add(f"... {len(files) - 10} more")

        self.console.print(file_tree)
        self.console.print(f"[green]✓ Generated {len(artifacts)} files[/green]")
        self.logger.info("code_generation", "files_generated",
            f"{len(artifacts)} files generated")

    async def _phase_code_review(self, context: ProjectContext) -> ReviewResult:
        """Phase 4: Code review with YES/NO verdict."""
        agent = self.agents["reviewer"]
        result = await agent.review_all(context)

        # Log each file review
        for artifact in context.generated_files:
            if artifact.is_test or artifact.is_doc:
                continue
            comments = [c for c in context.review_comments if c.file_path == artifact.path]
            self.logger.log_review(
                phase="code_review",
                file_path=artifact.path,
                issue_count=len(comments),
                critical=sum(1 for c in comments if c.severity == "critical"),
                errors=sum(1 for c in comments if c.severity == "error"),
                warnings=sum(1 for c in comments if c.severity == "warning"),
                approved=(artifact.path not in [c.file_path for c in result.blocking_issues]),
            )

        self.console.print(Panel(
            result.format_report(),
            title=f"Review: {result.verdict}",
            border_style="green" if result.approved else "red",
        ))

        return result

    async def _handle_review_result(self, context: ProjectContext):
        """Handle review result: auto-fix loop if NO, or proceed."""
        agent = self.agents["reviewer"]
        reviewer_result: ReviewResult | None = None

        # Find the last review result from context.review_comments
        for turn in reversed(context.conversation):
            if turn.agent == AgentRole.REVIEWER:
                # Re-run review to get the result object
                reviewer_result = await agent.review_all(context)
                break

        if reviewer_result is None:
            reviewer_result = await agent.review_all(context)

        if reviewer_result.approved:
            self.logger.log_decision(
                phase="code_review",
                agent="reviewer",
                decision="APPROVED",
                rationale=reviewer_result.reason,
                approved=True,
            )
            self.console.print("[green]✅ Code APPROVED — proceeding to next phase[/green]")
            return

        # NO — run fix loop
        self.logger.log_decision(
            phase="code_review",
            agent="reviewer",
            decision="REJECTED",
            rationale=reviewer_result.reason,
            approved=False,
        )
        self.console.print(f"[red]❌ Code REJECTED — {len(reviewer_result.blocking_issues)} blocking issue(s)[/red]")

        for iteration in range(1, self.config.max_fix_iterations + 1):
            self.console.print(f"\n[yellow]🔧 Fix iteration {iteration}/{self.config.max_fix_iterations}[/yellow]")
            self.logger.start_phase(f"fix_iteration_{iteration}")

            fixed = await self._run_fix_iteration(context, reviewer_result.blocking_issues)
            self.logger.info(f"fix_iteration_{iteration}", "fix_complete",
                f"Fixed {fixed} out of {len(reviewer_result.blocking_issues)} issues")

            # Re-review
            reviewer_result = await agent.review_all(context)

            self.console.print(Panel(
                reviewer_result.format_report(),
                title=f"Re-review: {reviewer_result.verdict}",
                border_style="green" if reviewer_result.approved else "red",
            ))

            self.logger.log_decision(
                phase=f"fix_iteration_{iteration}_review",
                agent="reviewer",
                decision="APPROVED" if reviewer_result.approved else "REJECTED",
                rationale=reviewer_result.reason,
                approved=reviewer_result.approved,
            )

            self.logger.end_phase(f"fix_iteration_{iteration}",
                status="completed")

            if reviewer_result.approved:
                self.console.print("[green]✅ Fixed and APPROVED[/green]")
                return

        self.console.print(
            f"[red]⚠️ Max fix iterations ({self.config.max_fix_iterations}) reached. "
            f"{len(reviewer_result.blocking_issues)} issue(s) remain.[/red]"
        )

    async def _run_fix_iteration(
        self,
        context: ProjectContext,
        blocking_issues: list,
    ) -> int:
        """Fix blocking issues from a review result."""
        agent = self.agents["coder"]
        fixed = 0

        for issue in blocking_issues:
            if not issue.suggestion:
                continue

            self.console.print(f"  🔧 {issue.file_path}: {issue.message[:60]}")

            feedback = f"[{issue.severity.upper()}] {issue.message}"
            feedback += f"\nFix: {issue.suggestion}"

            result = await agent.regenerate_file(
                path=issue.file_path,
                feedback=feedback,
                context=context,
            )

            if result:
                fixed += 1
                self.logger.log_file_write(
                    phase=f"fix_iteration",
                    file_path=issue.file_path,
                    size_bytes=len(result.content),
                    language=result.language,
                )
                self.console.print(f"    ✓ Regenerated: {issue.file_path}")

        self.console.print(f"  Fixed {fixed}/{len(blocking_issues)} issues")
        return fixed

    async def _phase_test_generation(self, context: ProjectContext) -> None:
        """Phase 5: Generate tests."""
        agent = self.agents["tester"]
        tests = await agent.generate_tests(context)

        for t in tests:
            self.logger.log_file_write(
                phase="test_generation",
                file_path=t.path,
                size_bytes=len(t.content),
                language=t.language,
            )

        self.console.print(f"[green]✓ Generated {len(tests)} test files[/green]")
        self.logger.info("test_generation", "tests_generated",
            f"{len(tests)} test files")

    async def _phase_documentation(self, context: ProjectContext) -> None:
        """Phase 6: Generate documentation."""
        agent = self.agents["documenter"]
        docs = await agent.generate_all(context)

        for d in docs:
            self.logger.log_file_write(
                phase="documentation",
                file_path=d.path,
                size_bytes=len(d.content),
                language=d.language,
            )

        self.console.print(f"[green]✓ Generated {len(docs)} documentation files[/green]")
        self.logger.info("documentation", "docs_generated",
            f"{len(docs)} doc files")

    # =============================================================================
    # HARNESS 4-STEP CYCLE PHASES
    # =============================================================================

    async def _phase_diagnostic(self, context: ProjectContext) -> DiagnosticReport:
        """
        Phase: DIAGNOSTIC (Harness Step 1)

        Identify model failure modes BEFORE adding any harness components.
        This is the most important step — wrong assumptions here lead to
        over-engineered harnesses that never get simplified.

        The diagnostic agent examines:
        1. The requirement specification
        2. Past project history for failure patterns
        3. General model limitations for this task type

        Output: DiagnosticReport with identified gaps and recommended components.
        """
        agent = self.agents.get("diagnostic")
        if not agent:
            return DiagnosticReport()

        report = await agent.diagnose(context)

        # Register recommended components in the harness registry
        for comp_data in report.recommended_harness_components:
            comp = HarnessComponent(
                name=comp_data.get("name", "unknown"),
                purpose=comp_data.get("purpose", ""),
                addresses_gap=comp_data.get("addresses_gap", ""),
                added_at_version=self.config.model_version,
            )
            self.harness_registry.register(comp)

        self.console.print(Panel(
            report.format_report(),
            title="Diagnostic Report (Harness Step 1)",
            border_style="cyan",
        ))

        # Log complexity assessment
        complexity = report.estimated_harness_complexity
        self.logger.info("diagnostic", "harness_complexity",
            f"Harness complexity: {complexity}",
            extra={
                "failure_modes": len(report.failure_modes),
                "components_needed": len(report.recommended_harness_components),
                "complexity": complexity,
            })

        return report

    async def _phase_evaluator(self, context: ProjectContext) -> list[EvaluationResult]:
        """
        Phase: EVALUATOR (Harness Step 2 — Generator/Evaluator Separation)

        Run the separated evaluator on generated code.
        Key insight: A critical evaluator can be trained separately from the
        generator, without destroying the generator's creativity.

        This follows the Anthropic finding that separate evaluator >> self-critique.
        """
        agent = self.agents.get("evaluator")
        if not agent:
            return []

        results = await agent.evaluate_all(context)

        # Aggregate scores
        total_score = sum(r.overall_score for r in results)
        avg_score = total_score // len(results) if results else 0

        failed = sum(1 for r in results if r.is_fail)
        passed = sum(1 for r in results if r.is_pass)

        self.console.print(Panel(
            f"**Evaluated**: {len(results)} files\n"
            f"**Average Score**: {avg_score}/100\n"
            f"**Passed**: {passed} | **Failed**: {failed}",
            title="Evaluator Results (Harness Step 2)",
            border_style="yellow" if failed > 0 else "green",
        ))

        # Store evaluation results in shared context for verification
        context.shared["evaluation_results"] = results
        context.shared["evaluation_avg_score"] = avg_score

        self.logger.info("evaluator", "evaluations_complete",
            f"Evaluated {len(results)} files, avg_score={avg_score}, failed={failed}")

        # Log component effectiveness
        self._log_component_effectiveness(context)

        return results

    async def _phase_verification(self, context: ProjectContext) -> dict:
        """
        Phase: VERIFICATION (Harness Step 3)

        Verify that the harness actually moves output from "looks done"
        to "actually deliverable."

        Verification checks three things:
        1. WHAT improved: Which failure modes did the harness address?
        2. COST: How much extra time/complexity did the harness add?
        3. WITHOUT IT: Would the output have been worse without the harness?

        This is answered by comparing evaluation scores and running ablation.
        """
        eval_results: list[EvaluationResult] = context.shared.get("evaluation_results", [])
        eval_avg = context.shared.get("evaluation_avg_score", 0)

        # Get review confidence
        review_result = None
        for turn in reversed(context.conversation):
            if turn.agent == AgentRole.REVIEWER:
                # Get review result from the last review phase
                break

        # Compute verification metrics
        verification = {
            "files_reviewed": len(context.generated_files),
            "evaluation_score": eval_avg,
            "test_count": len(context.test_files),
            "review_issues": len(context.review_comments),
            "harness_complexity": len(self.harness_registry.list_active()),
            "verification_passed": eval_avg >= 60,
        }

        # Key question: did we move from "looks done" to "actually deliverable"?
        # This is measured by the evaluation score — 60+ means actually deliverable
        is_deliverable = eval_avg >= 60

        status_color = "green" if is_deliverable else "red"
        status_text = "✅ ACTUALLY DELIVERABLE" if is_deliverable else "❌ STILL LOOKS COMPLETE"

        self.console.print(Panel(
            f"**Verification Result**: {status_text}\n\n"
            f"**Evaluation Score**: {eval_avg}/100\n"
            f"**Files Generated**: {verification['files_reviewed']}\n"
            f"**Tests Generated**: {verification['test_count']}\n"
            f"**Review Issues**: {verification['review_issues']}\n"
            f"**Active Harness Components**: {verification['harness_complexity']}\n\n"
            f"*Harness Step 3: Did we move from 'looks done' to 'actually deliverable'?*\n"
            f"*Score ≥ 60 = actually deliverable. < 60 = needs more work.*",
            title=f"Verification (Harness Step 3) — {status_text}",
            border_style=status_color,
        ))

        self.logger.info("verification", "verification_complete",
            f"Verification {'PASSED' if is_deliverable else 'FAILED'}: "
            f"score={eval_avg}, harness_components={verification['harness_complexity']}",
            extra=verification)

        context.shared["verification_result"] = verification
        return verification

    async def _phase_ablation(self, context: ProjectContext) -> AblationReport:
        """
        Phase: ABLATION (Harness Step 4)

        Run ablation experiments to identify which harness components are
        still load-bearing vs. which have become temporary scaffolding.

        This is the "continuous simplification" step — the harness should
        NEVER stop being questioned. A component that was necessary at
        model version X might be obsolete at version Y.

        Key question: "Which of our current components would the model
        no longer need if we tested without them?"
        """
        verification: dict = context.shared.get("verification_result", {})
        eval_score = verification.get("evaluation_score", 70)
        harness_complexity = len(self.harness_registry.list_active())

        if harness_complexity == 0:
            self.console.print("[dim]No harness components registered — skipping ablation[/dim]")
            return AblationReport()

        # Get the next ablation target
        next_target = self.ablation_engine.plan_next_experiment()

        if not next_target:
            self.console.print(Panel(
                "No ablation targets — all components are recently tested or high-confidence.\n\n"
                "Components registered:\n" +
                "\n".join(f"- {c.name}: {c.purpose}" for c in self.harness_registry.list_active()),
                title="Ablation (Harness Step 4) — No experiment needed this run",
                border_style="green",
            ))
            return AblationReport()

        # Simulate ablation result based on model version
        # In practice, this would run a full project without the component
        # For now, we log the suggestion
        self.console.print(Panel(
            f"**Next Ablation Target**: {next_target.name}\n\n"
            f"Purpose: {next_target.purpose}\n"
            f"Addresses gap: {next_target.addresses_gap}\n"
            f"Added at version: {next_target.added_at_version or 'unknown'}\n"
            f"Current confidence: {next_target.confidence_score:.0%}\n"
            f"Avg duration: {next_target.avg_duration_ms/1000:.1f}s\n\n"
            f"*To run this ablation: remove the component and re-run the project.*\n"
            f"*Compare results to determine if component is still load-bearing.*\n\n"
            f"**Ablation Question**: Has the model evolved past needing '{next_target.name}'?\n"
            f"If removing it doesn't degrade output quality, it should be removed.",
            title=f"Ablation Suggestion (Harness Step 4)",
            border_style="yellow",
        ))

        # Log ablation status
        self.logger.info("ablation", "ablation_suggestion",
            f"Suggested ablation: {next_target.name}, "
            f"confidence={next_target.confidence_score:.0%}, "
            f"model_version={self.harness_registry.version}",
            extra={
                "suggested_component": next_target.name,
                "confidence": next_target.confidence_score,
                "registered_components": harness_complexity,
            })

        # Registry summary
        registry_summary = self.harness_registry.format_summary()
        self.logger.info("ablation", "harness_registry_summary",
            registry_summary)

        return self.ablation_engine.generate_report()

    # =============================================================================
    # HELPER METHODS
    # =============================================================================

    def _log_harness_components(self, context: ProjectContext):
        """Log registered harness components to the structured log."""
        report: DiagnosticReport = context.shared.get("diagnostic_report", DiagnosticReport())
        for comp in self.harness_registry.list_active():
            self.logger.info("harness", "component_registered",
                f"Registered: {comp.name} (gap: {comp.addresses_gap})",
                extra={
                    "component": comp.name,
                    "purpose": comp.purpose,
                    "addresses_gap": comp.addresses_gap,
                    "complexity": "minimal",
                })

    def _log_component_effectiveness(self, context: ProjectContext):
        """Log which harness components effectively addressed issues vs. which didn't."""
        eval_results: list[EvaluationResult] = context.shared.get("evaluation_results", [])
        review_comments = context.review_comments

        # For each issue found, attribute it to a harness component
        component_issues: dict[str, int] = {}
        for comment in review_comments:
            rule = getattr(comment, 'rule', None) or "N/A"
            component_issues[rule] = component_issues.get(rule, 0) + 1

        for comp_name, issue_count in component_issues.items():
            self.logger.info("harness", "component_effectiveness",
                f"Component '{comp_name}' caught {issue_count} issues",
                extra={"component": comp_name, "issues_caught": issue_count})

    # ---- Infrastructure ----

    def _init_infrastructure(self, context: ProjectContext, project_name: str):
        """Initialize git and logging infrastructure."""
        proj_id = context.project_id

        # Logger
        log_dir = self.config.project_root / ".codeforge" / "logs"
        self.logger = CodeForgeLogger(
            log_dir=log_dir,
            session_id=None,  # Auto-generate
            project_id=proj_id,
            project_name=project_name,
            console_output=True,
            file_output=True,
            log_level=self.config.log_level,
            config={
                "git_enabled": self.config.git_enabled,
                "auto_fix": self.config.auto_fix,
                "max_fix_iterations": self.config.max_fix_iterations,
                "llm_providers": list(self.llm.configs.keys()),
                # Harness 4-step cycle config
                "enable_diagnostic": self.config.enable_diagnostic,
                "enable_evaluator": self.config.enable_evaluator,
                "enable_verification": self.config.enable_verification,
                "enable_ablation": self.config.enable_ablation,
                "model_version": self.config.model_version,
            },
        )

        self.logger.info("pipeline", "init",
            f"CodeForge session started for project: {project_name}",
            extra={"project_id": proj_id})

        # Git
        if self.config.git_enabled:
            self.git = GitManager(self.config.project_root)
            if not self.git.is_repo():
                self.git.init()
            status = self.git.status()
            self.logger.info("pipeline", "git_init",
                f"Git {'initialized' if status.get('initialized') else 'already active'}")

    def _git_commit_phase(self, phase: str, context: ProjectContext, message: str | None = None):
        """Commit the current state after a phase."""
        if not self.config.git_enabled or not self.config.git_auto_commit or not self.git:
            return

        files = list(self.config.project_root.rglob("*"))
        files = [f for f in files if f.is_file() and not str(f).startswith(str(self.config.project_root / ".codeforge"))]

        version = self.git.commit_phase(
            phase=phase,
            files=files,
            message=message or f"Phase: {phase}",
            metadata={
                "files": len(files),
                "tokens": context.total_tokens,
            },
        )

        if version:
            if self.config.git_auto_tag:
                self.git.tag_phase(phase, version)
            self.logger.info(phase, "git_commit",
                f"Committed as {version.commit_hash[:8]}")

    def _finalize(self, context: ProjectContext):
        """Finalize: flush logs, save checkpoint."""
        if self.config.checkpoint_enabled:
            cp_path = self.config.project_root / f".codeforge/checkpoint_{context.project_id}.json"
            context.save_checkpoint(cp_path)
            self.console.print(f"[dim]Checkpoint saved to {cp_path}[/dim]")

        if self.logger:
            self.logger.complete(status=context.state.name.lower())
            report_path = self.config.project_root / f".codeforge/logs/report_{self.logger._session_id}.md"
            if report_path.exists():
                self.console.print(f"[dim]Full log report: {report_path}[/dim]")

    async def _write_all_files(self, context: ProjectContext):
        """Write all artifacts to disk."""
        root = Path(self.config.project_root)
        root.mkdir(parents=True, exist_ok=True)

        all_artifacts = (
            context.generated_files
            + context.test_files
            + context.doc_files
        )

        written = 0
        for artifact in all_artifacts:
            file_path = root / artifact.path
            file_path.parent.mkdir(parents=True, exist_ok=True)

            content = artifact.content
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            self.logger.log_file_write(
                phase=context.current_phase,
                file_path=artifact.path,
                size_bytes=len(content),
                language=artifact.language,
            )
            written += 1

        self.console.print(f"\n[green]📁 Wrote {written} files to {root}[/green]")
        self.logger.info("pipeline", "files_written",
            f"{written} files written to disk", extra={"output_dir": str(root)})

    def _print_summary(self, context: ProjectContext, elapsed: float):
        """Print the final summary, including Harness 4-step cycle metrics."""
        spec = context.requirement_spec
        critical = sum(1 for c in context.review_comments if c.severity == "critical")
        errors = sum(1 for c in context.review_comments if c.severity == "error")

        # Harness 4-step metrics
        diag_report: DiagnosticReport = context.shared.get("diagnostic_report")
        verification: dict = context.shared.get("verification_result", {})
        eval_avg = context.shared.get("evaluation_avg_score", 0)

        harness_info = ""
        if self.config.enable_diagnostic and diag_report:
            harness_info = (
                f"\n"
                f"**Harness (Step 1)**: {len(diag_report.failure_modes)} failure modes detected\n"
                f"**Harness Complexity**: {diag_report.estimated_harness_complexity}\n"
            )
        if self.config.enable_verification and verification:
            harness_info += (
                f"**Verification (Step 3)**: score={eval_avg}/100\n"
            )
        if self.config.enable_ablation:
            harness_info += (
                f"**Ablation (Step 4)**: "
                f"{len(self.harness_registry.list_active())} components active\n"
            )

        self.console.print("\n")
        self.console.print(Panel(
            f"[bold green]✅ Development Complete![/bold green]\n\n"
            f"Project: {spec.project_name if spec else 'N/A'}\n"
            f"Type: {spec.project_type if spec else 'N/A'}\n"
            f"Files: {len(context.generated_files)}\n"
            f"Tests: {len(context.test_files)}\n"
            f"Docs: {len(context.doc_files)}\n"
            f"Review: {len(context.review_comments)} issues "
            f"({critical} critical, {errors} errors)\n"
            f"Tokens: {context.total_tokens:,}\n"
            f"Time: {elapsed:.1f}s\n"
            f"Output: {self.config.project_root}"
            f"{harness_info}",
            title="Summary",
            border_style="green",
        ))
