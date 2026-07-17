"""chat Agent 的不可变产品配置。"""

CHAT_AGENT_ID = "chat"
CHAT_AGENT_VERSION = "0.1.0"
CHAT_ALLOWED_CHANNELS = frozenset({"cli"})
CHAT_RUNTIME_KIND = "chat"
CHAT_COLLABORATION_ROLES = frozenset({"planner", "worker"})
CHAT_SKILL_GRANTS = frozenset({
    "tasks.propose",
    "plans.propose",
    "memory.propose",
    "memory.forget",
    "goals.read",
    "goals.manage",
    "preferences.write",
    "browser.navigate",
    "browser.interact",
    "browser.diagnose",
})
