"""兼容旧导入路径；产品定义已迁入 ``agents.chat`` 包。"""

from harness.agents.chat import CHAT_AGENT

# 旧名称仅供外部代码迁移，registry 不再使用它。
CHAT_ASSISTANT = CHAT_AGENT
