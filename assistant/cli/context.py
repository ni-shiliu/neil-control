"""CliContext：CLI 层的依赖容器。

替代原先散落在 main.py 的模块级单例（_recorder / _memory /
_chat_harness）与路径常量，改为显式注入。
CLI 只注入 Harness 门面，不直接引用 Agent、路由或 Runtime。
让 cli/ 各 handler 不再直接引用 main 的全局状态。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from harness.channels import RequestIdentity

from harness.facade import Harness

if TYPE_CHECKING:
    from engine.memory import MemoryStore
    from engine.records import RunRecorder


@dataclass
class CliContext:
    recorder: "RunRecorder"
    memory: "MemoryStore"
    harness: Harness
    request_identity: RequestIdentity
    assistant_dir: Path
    channel_id: str
    runtime_doc: Path
    loops_dir: Path
    tests_dir: Path

    @classmethod
    def build(
        cls,
        *,
        channel_id: str,
        assistant_dir: Path | None = None,
    ) -> "CliContext":
        """按 main.py 原有方式组装单例与路径，供启动入口调用一次。"""
        from engine.memory import MemoryStore
        from engine.records import RunRecorder

        if not channel_id.strip():
            raise ValueError("channel_id 不能为空")
        base = assistant_dir or Path(__file__).resolve().parent.parent
        memory = MemoryStore()
        # CLI 是渠道适配器：local 身份只在此处定义，不进入通用 Harness 默认值。
        request_identity = RequestIdentity(tenant_id="local", user_id="local", thread_id=uuid.uuid4().hex)
        return cls(
            recorder=RunRecorder(),
            memory=memory,
            harness=Harness.build_default(),
            request_identity=request_identity,
            assistant_dir=base,
            channel_id=channel_id.strip(),
            runtime_doc=base / "RUNTIME.md",
            loops_dir=base / "loops",
            tests_dir=base / "tests",
        )
