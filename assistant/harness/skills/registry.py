"""Skill registry：Skill 授权到 Action id 的唯一映射来源。"""

from __future__ import annotations

from collections.abc import Iterable

from harness.skills.definition import AuthorizedToolSet, SkillManifest


class SkillRegistryError(ValueError):
    pass


class SkillRegistry:
    def __init__(self, manifests: Iterable[SkillManifest]):
        self._manifests: dict[str, SkillManifest] = {}
        for manifest in manifests:
            if not manifest.id:
                raise SkillRegistryError("Skill id 不能为空")
            if manifest.id in self._manifests:
                raise SkillRegistryError(f"重复的 Skill id: {manifest.id}")
            self._manifests[manifest.id] = manifest

    def get(self, skill_id: str) -> SkillManifest:
        try:
            return self._manifests[skill_id]
        except KeyError as exc:
            raise SkillRegistryError(f"未知 Skill: {skill_id}") from exc

    def validate_grants(self, skill_ids: Iterable[str]) -> frozenset[str]:
        grants = frozenset(skill_ids)
        for skill_id in grants:
            self.get(skill_id)
        return grants

    def authorize(
        self,
        *,
        agent_id: str,
        skill_ids: Iterable[str],
        available_tool_names: Iterable[str],
    ) -> AuthorizedToolSet:
        grants = self.validate_grants(skill_ids)
        available = frozenset(available_tool_names)
        names: set[str] = set()
        for skill_id in grants:
            names.update(self.get(skill_id).action_ids)
        return AuthorizedToolSet(
            agent_id=agent_id,
            skill_ids=grants,
            action_ids=frozenset(names & available),
        )


SKILL_REGISTRY = SkillRegistry((
    # 这两个 Skill 没有模型工具 schema；它们授权 TaskIntake 提交受限提案。
    SkillManifest("tasks.propose", "1.0.0", frozenset()),
    SkillManifest("plans.propose", "1.0.0", frozenset()),
    SkillManifest("memory.propose", "1.0.0", frozenset({"memory_propose"}), risk_level="low"),
    SkillManifest("memory.forget", "1.0.0", frozenset({"memory_forget"}), risk_level="low"),
    SkillManifest("goals.read", "1.0.0", frozenset({"list_goals", "show_goal"})),
    SkillManifest(
        "goals.manage",
        "1.0.0",
        frozenset({"pause_goal", "resume_goal", "delete_goal", "rerun_goal", "create_goal"}),
        risk_level="medium",
    ),
    SkillManifest(
        "preferences.write",
        "1.0.0",
        frozenset({"update_goal_preferences", "update_loop_preferences", "update_user_preferences"}),
    ),
    SkillManifest(
        "browser.navigate",
        "1.0.0",
        frozenset({"browser_open_url", "browser_observe", "browser_wait"}),
        required_capabilities=frozenset({"browser"}),
        risk_level="medium",
    ),
    SkillManifest(
        "browser.interact",
        "1.0.0",
        frozenset({"browser_click_text", "browser_type"}),
        required_capabilities=frozenset({"browser"}),
        risk_level="medium",
    ),
    SkillManifest(
        "browser.diagnose",
        "1.0.0",
        frozenset({"browser_diagnostic"}),
        required_capabilities=frozenset({"browser"}),
        risk_level="low",
    ),
))
