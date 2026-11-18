"""⑤ 层的确定性策略与裁决对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from harness.runtime.contracts import ActionProposal


GovernanceKind = Literal["allow", "deny", "interrupt", "finish", "compact"]


@dataclass(frozen=True)
class RunPolicy:
    max_iterations: int = 8
    max_actions: int = 16
    max_repeated_action: int = 2

    def __post_init__(self) -> None:
        if min(self.max_iterations, self.max_actions, self.max_repeated_action) < 1:
            raise ValueError("RunPolicy 的预算必须为正数")


@dataclass(frozen=True)
class ContextBudget:
    """⑤ 对单次模型输入的上下文窗口防线。"""

    context_window_tokens: int = 1_000_000
    reserved_output_tokens: int = 32_000
    soft_input_tokens: int = 800_000
    compaction_target_tokens: int = 600_000

    @property
    def hard_input_tokens(self) -> int:
        return self.context_window_tokens - self.reserved_output_tokens

    def __post_init__(self) -> None:
        if min(
            self.context_window_tokens,
            self.reserved_output_tokens,
            self.soft_input_tokens,
            self.compaction_target_tokens,
        ) < 1:
            raise ValueError("ContextBudget 的 token 预算必须为正数")
        if self.reserved_output_tokens >= self.context_window_tokens:
            raise ValueError("ContextBudget 输出预留必须小于上下文窗口")
        if not self.compaction_target_tokens < self.soft_input_tokens < self.hard_input_tokens:
            raise ValueError("ContextBudget 必须满足 target < soft < hard")


@dataclass(frozen=True)
class GovernanceProfile:
    id: str
    version: str
    run_policy: RunPolicy = field(default_factory=RunPolicy)
    context_budget: ContextBudget = field(default_factory=ContextBudget)
    auto_approve_readonly: bool = True
    auto_approve_mutations: bool = False
    auto_approve_effects: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.version:
            raise ValueError("GovernanceProfile 必须包含 id 与 version")


@dataclass(frozen=True)
class AuthorizedAction:
    proposal: ActionProposal
    idempotency_key: str


@dataclass(frozen=True)
class GovernanceDecision:
    kind: GovernanceKind
    reason: str = ""
    authorized_actions: tuple[AuthorizedAction, ...] = ()


DEFAULT_GOVERNANCE_PROFILE = GovernanceProfile(id="default", version="1.0.0")
