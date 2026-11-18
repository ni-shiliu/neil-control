import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loops import email_loop as email_loop_module
from loops.email_loop import EmailLoop


def test_email_loop_execute_uses_agentic_step_for_action() -> None:
    calls = []
    original = email_loop_module.run_agentic_step

    def fake_agentic_step(**kwargs):
        calls.append(kwargs)
        kwargs["execute_tool"](
            "save_draft",
            {
                "reply": "收到，我会尽快处理。",
                "reason": "需要用户确认后再发送",
                "confidence": 80,
            },
        )

        class Result:
            final_text = ""
            stop_reason = "direct_tool"
            iterations = 1
            tool_calls = [type("ToolCall", (), {
                "name": "save_draft",
                "input": {},
                "result": "ok",
            })()]

        return Result()

    email_loop_module.run_agentic_step = fake_agentic_step
    try:
        loop = EmailLoop()
        result = loop.execute({
            "memory": {},
            "emails": [{
                "uid": "1",
                "sender": "person@example.com",
                "subject": "会议确认",
                "body": "明天会议是否照常？",
            }],
        })
    finally:
        email_loop_module.run_agentic_step = original

    assert len(calls) == 1
    assert calls[0]["tools"][0]["name"] == "skip_email"
    assert calls[0]["tools"][1]["name"] == "save_draft"
    assert result["drafted"][0]["subject"] == "会议确认"
    assert result["sent"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
