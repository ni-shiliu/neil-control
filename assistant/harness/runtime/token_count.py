"""模型输入 token 计数端口及安全 fallback。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from harness.runtime.contracts import ContextUsage, ModelMessage


class TokenCounter(Protocol):
    def count(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ModelMessage],
        action_schemas: Sequence[Mapping[str, Any]],
    ) -> ContextUsage: ...


class GatewayTokenCounter:
    """优先调用供应商计数端口；不存在时使用保守字符上界。"""

    def __init__(self, gateway: object):
        self._gateway = gateway

    def count(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ModelMessage],
        action_schemas: Sequence[Mapping[str, Any]],
    ) -> ContextUsage:
        method = getattr(self._gateway, "count_input_tokens", None)
        if callable(method):
            try:
                value = method(
                    system_prompt=system_prompt,
                    messages=messages,
                    action_schemas=action_schemas,
                )
                if isinstance(value, ContextUsage):
                    return value
                return ContextUsage(input_tokens=int(value), estimated=False)
            except Exception:
                # 计数端点临时不可用时宁可高估，也不能绕过⑤硬上限。
                pass
        # 一个 UTF-8 byte 一个 token 是刻意偏保守的上界；它不会因为未接入
        # 供应商 tokenizer 而把超长请求放过⑤硬上限（Unicode 字符可能多字节）。
        payload = {
            "system": system_prompt,
            "messages": [
                {
                    "role": item.role,
                    "content": item.content,
                    "actions": [
                        {"id": action.call_id, "name": action.action_id, "input": dict(action.input)}
                        for action in item.actions
                    ],
                    "observations": [
                        {
                            "id": observation.call_id,
                            "name": observation.action_id,
                            "content": observation.content,
                        }
                        for observation in item.observations
                    ],
                }
                for item in messages
            ],
            "tools": list(action_schemas),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return ContextUsage(input_tokens=len(encoded.encode("utf-8")), estimated=True)
