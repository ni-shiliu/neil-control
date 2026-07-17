"""ClaudeTool — 封装 Claude API 调用。"""

import json
import logging

from claude_client import get_client, get_model

log = logging.getLogger(__name__)


class ClaudeTool:

    def __init__(self, model: str | None = None, max_tokens: int = 1024):
        self.model = model or get_model()
        self.default_max_tokens = max_tokens

    def complete(self, prompt: str, max_tokens: int | None = None) -> str:
        """发送单轮 prompt，返回文本响应。"""
        msg = get_client().messages.create(
            model=self.model,
            max_tokens=max_tokens or self.default_max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    def complete_json(self, prompt: str, max_tokens: int | None = None) -> dict:
        """发送 prompt，解析 JSON 响应，自动处理 ```json 代码块和首个 {...} 块。"""
        raw = self.complete(prompt, max_tokens)
        # 去掉 ```json ... ``` 包裹
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
        # 尝试直接解析
        try:
            result = json.loads(raw)
            return self._coerce_bools(result)
        except json.JSONDecodeError:
            pass
        # 提取第一个完整的 {...} 块
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
            return self._coerce_bools(result)
        raise ValueError(f"无法从响应中提取 JSON: {raw[:200]}")

    @staticmethod
    def _coerce_bools(d: dict) -> dict:
        """Claude 经常把 true/false 输出成字符串，统一转成 Python bool。"""
        for k, v in list(d.items()):
            if isinstance(v, str) and v.lower() in ("true", "false"):
                d[k] = v.lower() == "true"
        return d
