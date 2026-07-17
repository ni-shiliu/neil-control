"""chat Agent 的产品声明。"""

from harness.agents.chat.config import (
    CHAT_AGENT_ID,
    CHAT_AGENT_VERSION,
    CHAT_ALLOWED_CHANNELS,
    CHAT_COLLABORATION_ROLES,
    CHAT_RUNTIME_KIND,
    CHAT_SKILL_GRANTS,
)
from pathlib import Path

from harness.agents.definition import AgentDefinition
from harness.agents.identity import load_identity_markdown
from harness.agents.knowledge import load_knowledge_markdown
from harness.agents.workflow import load_workflow_markdown

CHAT_IDENTITY = load_identity_markdown(Path(__file__).with_name("identity.md"))
CHAT_WORKFLOW = load_workflow_markdown(Path(__file__).with_name("workflow.md"))
CHAT_KNOWLEDGE_POLICY = load_knowledge_markdown(Path(__file__).with_name("knowledge.md"))

CHAT_AGENT = AgentDefinition(
    id=CHAT_AGENT_ID,
    version=CHAT_AGENT_VERSION,
    identity=CHAT_IDENTITY,
    workflow_template=CHAT_WORKFLOW,
    knowledge_policy=CHAT_KNOWLEDGE_POLICY,
    skill_grants=CHAT_SKILL_GRANTS,
    allowed_channels=CHAT_ALLOWED_CHANNELS,
    runtime_kind=CHAT_RUNTIME_KIND,
    collaboration_roles=CHAT_COLLABORATION_ROLES,
)
