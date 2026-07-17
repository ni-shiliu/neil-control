---
id: chat_turn
version: 1.0.0
summary: 接收请求，按授权调用工具，并交付简洁回复。
---

## steps

- `understand`: 理解本回合请求和可用上下文。
- `act`: 在授权范围内按需调用工具。
- `respond`: 基于工具结果交付回复。
