import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from conversation_records import ConversationRecorder


def test_conversation_recorder_rolls_and_reads_recent(tmp_path: Path) -> None:
    recorder = ConversationRecorder(base_dir=tmp_path)

    for idx in range(12):
        record = recorder.build_record(
            user_input=f"user-{idx}",
            assistant_response=f"assistant-{idx}",
            route="ai",
            ai_result={"kind": "chat", "message": f"assistant-{idx}"},
            execution={"executed": True, "kind": "chat"},
        )
        recorder.save(record)

    recent = recorder.list_recent(limit=10)

    assert len(recent) == 10
    assert recent[0]["user_input"] == "user-11"
    assert recent[-1]["user_input"] == "user-2"


def test_conversation_recorder_cleanup_old_files(tmp_path: Path) -> None:
    recorder = ConversationRecorder(base_dir=tmp_path)
    old_path = tmp_path / "conversation_records_2026-06-19_001.json"
    new_path = tmp_path / "conversation_records_2026-06-23_001.json"
    payload = {"date": "2026-06-19", "records": []}

    old_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    new_path.write_text(json.dumps({"date": "2026-06-23", "records": []}, ensure_ascii=False), encoding="utf-8")

    deleted = recorder.cleanup_old_files()

    assert deleted == 1
    assert not old_path.exists()
    assert new_path.exists()
