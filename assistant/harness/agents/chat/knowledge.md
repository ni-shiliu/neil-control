---
read_scopes: shared_rules, conversation, memory.user, personal_config
write_scopes: memory.user
---

## policy

CLI chat Agent 可读取会话记录、用户记忆和个人配置。
它只能通过记忆提案能力更新 user memory；提案仅存在于当前 Run，校验后直接覆盖用户 Markdown 中同 key 的当前值。用户明确要求保存的姓名等稳定事实和偏好可自动写入，不能直接修改项目记忆或个人配置。
