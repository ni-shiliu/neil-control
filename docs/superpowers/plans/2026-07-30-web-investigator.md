# Web Investigator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `web-investigator` skill —— 一个证据驱动、跨多网页系统交叉验证、只出排查+修复建议的可视化排查框架(Claude Code skill 形态)。

**Architecture:** 方案 C(D12)。Python 执行器承担**不需要浏览器的确定性逻辑**(配置加载校验、标识流转状态机、SQL 只读硬拦截、报告拼装),全部为纯函数、可 TDD;`SKILL.md` 负责**编排层**——驱动 chrome-devtools MCP(navigate/snapshot/fill)、读线索、交叉验证,并调用 Python 执行器完成确定性部分。安全硬约束(D10 SQL 只读)落在有测试的 Python 代码里,而非 prompt。

**Tech Stack:** Python 3.12(跟随现有 `music-control` skill:纯 `.py`、argparse CLI、无构建链)、PyYAML 6.0(已装,配置解析)、stdlib `unittest`(pytest 未装,不引新依赖)、chrome-devtools MCP(由 SKILL.md 编排层调用,非 Python)。

## Global Constraints

- **不绑定公司**(D1):执行器与 SKILL.md 代码中不得出现任何具体平台名(六翼/绿洲等);具体平台只存在于用户私有 YAML 配置。
- **只读硬约束**(D10):`capability: database` 且 `readonly: true` 的平台,执行器在放行 SQL 前必须解析,非单条 SELECT 一律拒绝(拒绝=抛异常/返回错误,绝不放行)。
- **不自动修复**(D9):产出只含建议,执行器与 SKILL.md 均不得执行任何写/改操作。
- **证据驱动编排**(D6):logs 为默认起点,每步过"证据是否足以定位根因"评估门,够则收尾,不够选最补缺口的平台;固定序仅为倾向。
- **零新依赖**:除已装的 PyYAML 外不引入第三方库;测试用 stdlib `unittest`。
- **语言风格**:Python 脚本 docstring/注释用中文,跟随 `skills/music-control/scripts/` 现有风格。
- **测试运行方式**:`python3 -m unittest discover -s skills/web-investigator/tests -v`(从仓库根运行)。

---

## 文件结构

```
skills/web-investigator/
  SKILL.md                      # 编排层(读线索→抽标识→证据驱动编排→交叉验证→报告)
  scripts/
    config_loader.py            # 加载+校验平台 YAML 配置 → Platform 对象列表
    sql_guard.py                # SQL 只读硬拦截(D10):是否单条 SELECT
    identifier_store.py         # 标识流转状态(D13):inputs/yields 的收集与供给
    report.py                   # 报告拼装(D9 四段:现象/根因/证据链/修复建议)
    investigate_cli.py          # CLI 入口:粘合上述模块,供 SKILL.md 调用
  references/
    config-schema.md            # 平台配置 schema 说明文档
  examples/
    logs-platform.example.yaml       # 脱敏示例:logs
    database-platform.example.yaml   # 脱敏示例:database(readonly)
  tests/
    test_config_loader.py
    test_sql_guard.py
    test_identifier_store.py
    test_report.py
```

各文件单一职责,互相通过明确函数签名交互(见各任务 Interfaces 块)。

---

## 任务清单(概览)

- **Task 1: sql_guard** —— SQL 只读硬拦截(最独立、安全关键,先做)
- **Task 2: config_loader** —— 平台 YAML 加载与校验
- **Task 3: identifier_store** —— 标识流转状态机
- **Task 4: report** —— 报告四段拼装
- **Task 5: investigate_cli** —— CLI 粘合层
- **Task 6: SKILL.md + references + examples** —— 编排层与文档/示例

> 任务详情在下方逐个展开。各任务以"可独立测试的交付物"结尾。

---

### Task 1: sql_guard —— SQL 只读硬拦截

**Files:**
- Create: `skills/web-investigator/scripts/sql_guard.py`
- Test: `skills/web-investigator/tests/test_sql_guard.py`

**Interfaces:**
- Consumes: 无(纯函数,无上游依赖)
- Produces:
  - `class SqlWriteRejected(Exception)` —— 非只读 SQL 被拒时抛出,`str(e)` 含拒绝原因
  - `def assert_readonly(sql: str) -> str` —— 输入 SQL 字符串;是单条只读 SELECT 则原样返回(去首尾空白),否则抛 `SqlWriteRejected`

**判定规则(实现依据 D10):**
- 去掉行内 `--` 注释与 `/* */` 块注释、去首尾空白后判定。
- 允许:以 `SELECT` 或 `WITH`(CTE,最终必须是 SELECT)开头的**单条**语句。
- 拒绝:含第二条语句(去尾分号后仍有 `;` 分隔的非空语句);首关键词属于写操作集合 `{INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, REPLACE, MERGE, GRANT, REVOKE, CALL, EXEC, EXECUTE, SET, USE}`;空字符串。
- 大小写不敏感。

- [ ] **Step 1: Write the failing test**

```python
# skills/web-investigator/tests/test_sql_guard.py
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sql_guard import assert_readonly, SqlWriteRejected


class TestSqlGuard(unittest.TestCase):
    def test_plain_select_passes(self):
        self.assertEqual(assert_readonly("SELECT * FROM orders"), "SELECT * FROM orders")

    def test_select_case_insensitive(self):
        self.assertEqual(assert_readonly("  select id from t  "), "select id from t")

    def test_with_cte_passes(self):
        sql = "WITH a AS (SELECT 1) SELECT * FROM a"
        self.assertEqual(assert_readonly(sql), sql)

    def test_trailing_semicolon_ok(self):
        self.assertEqual(assert_readonly("SELECT 1;"), "SELECT 1;")

    def test_update_rejected(self):
        with self.assertRaises(SqlWriteRejected):
            assert_readonly("UPDATE orders SET status=1")

    def test_delete_rejected(self):
        with self.assertRaises(SqlWriteRejected):
            assert_readonly("DELETE FROM orders WHERE id=1")

    def test_ddl_rejected(self):
        for s in ["DROP TABLE t", "ALTER TABLE t ADD c INT", "TRUNCATE t", "CREATE TABLE t(id int)"]:
            with self.assertRaises(SqlWriteRejected):
                assert_readonly(s)

    def test_stacked_write_after_select_rejected(self):
        with self.assertRaises(SqlWriteRejected):
            assert_readonly("SELECT 1; DROP TABLE t")

    def test_comment_hiding_write_rejected(self):
        with self.assertRaises(SqlWriteRejected):
            assert_readonly("/* harmless */ DELETE FROM t")

    def test_empty_rejected(self):
        with self.assertRaises(SqlWriteRejected):
            assert_readonly("   ")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest skills.web-investigator.tests.test_sql_guard -v` 或 `cd skills/web-investigator && python3 -m unittest tests.test_sql_guard -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'sql_guard'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/web-investigator/scripts/sql_guard.py
"""SQL 只读硬拦截(D10):仅放行单条只读 SELECT/WITH 查询,其余一律拒绝。"""
import re

WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "MERGE", "GRANT", "REVOKE", "CALL", "EXEC", "EXECUTE", "SET", "USE",
}


class SqlWriteRejected(Exception):
    """非只读 SQL 被拒绝时抛出。"""


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)      # 块注释
    sql = re.sub(r"--[^\n]*", " ", sql)                    # 行注释
    return sql


def assert_readonly(sql: str) -> str:
    """是单条只读查询则原样返回(去首尾空白);否则抛 SqlWriteRejected。"""
    original = sql.strip()
    cleaned = _strip_comments(sql).strip()
    if not cleaned:
        raise SqlWriteRejected("空 SQL,拒绝执行")

    # 拆分语句:去掉唯一结尾分号后,若仍含分号分隔的非空语句 → 多条,拒绝
    body = cleaned[:-1] if cleaned.endswith(";") else cleaned
    statements = [s for s in body.split(";") if s.strip()]
    if len(statements) > 1:
        raise SqlWriteRejected(f"检测到多条语句,拒绝执行:{sql!r}")

    first = re.match(r"[a-zA-Z]+", cleaned)
    head = first.group(0).upper() if first else ""
    if head in WRITE_KEYWORDS:
        raise SqlWriteRejected(f"检测到写操作 {head},只读约束拒绝执行:{sql!r}")
    if head not in ("SELECT", "WITH"):
        raise SqlWriteRejected(f"仅允许 SELECT/WITH 查询,实际首词为 {head or '空'}:{sql!r}")
    return original
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_sql_guard -v`
Expected: PASS(10 tests)

- [ ] **Step 5: Commit**

```bash
git add skills/web-investigator/scripts/sql_guard.py skills/web-investigator/tests/test_sql_guard.py
git commit -m "feat(web-investigator): SQL 只读硬拦截 sql_guard (D10)"
```

---

### Task 2: config_loader —— 平台 YAML 加载与校验

**Files:**
- Create: `skills/web-investigator/scripts/config_loader.py`
- Test: `skills/web-investigator/tests/test_config_loader.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class ConfigError(Exception)` —— 配置非法时抛出,含具体原因
  - `class Platform` —— dataclass,字段:`name:str`, `capability:str`, `url:str`, `login_check:dict`(键 `logged_in_when:str`), `query:dict`(键 `intent:str`, `inputs:list[str]`, 可选 `readonly:bool`), `extract:dict`(键 `intent:str`, `yields:list[str]`), `script:str|None`
  - `def load_platform(path: str) -> Platform` —— 读单个 YAML 文件 → 校验 → Platform
  - `def load_platforms(dir_path: str) -> list[Platform]` —— 读目录下所有 `*.yaml`/`*.yml`,各自 `load_platform`,按 capability 优先级排序(logs<trace<metrics<database<other)返回

**校验规则:**
- 必填:`name`, `capability`, `url`, `query.intent`, `query.inputs`, `extract.intent`, `extract.yields`;缺任一 → `ConfigError`。
- `capability` ∈ `{logs, trace, metrics, database, other}`;否则 `ConfigError`。
- `capability == "database"` 时,`query.readonly` 必须为 `True`(D10 兜底:数据库平台强制只读)。
- `login_check` 可缺省(视为无需登录检测);给出时须含 `logged_in_when`。
- `script` 缺省为 `None`。

- [ ] **Step 1: Write the failing test**

```python
# skills/web-investigator/tests/test_config_loader.py
import unittest, tempfile, os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from config_loader import load_platform, load_platforms, Platform, ConfigError

LOGS_YAML = """
name: 测试日志
capability: logs
url: https://example.internal/logs
login_check:
  logged_in_when: "页面出现搜索框"
query:
  intent: "输入 {{trace_id}} 搜索"
  inputs: [trace_id]
extract:
  intent: "提取报错堆栈"
  yields: [error_stack, service_name]
"""

DB_YAML = """
name: 测试库
capability: database
url: https://example.internal/db
query:
  intent: "执行 {{sql}}"
  inputs: [sql]
  readonly: true
extract:
  intent: "读结果"
  yields: [rows]
"""

def _write(tmp, name, content):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


class TestConfigLoader(unittest.TestCase):
    def test_load_valid_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "logs.yaml", LOGS_YAML)
            plat = load_platform(p)
            self.assertEqual(plat.capability, "logs")
            self.assertEqual(plat.query["inputs"], ["trace_id"])
            self.assertIsNone(plat.script)

    def test_missing_required_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "bad.yaml", "name: x\ncapability: logs\nurl: http://x\n")
            with self.assertRaises(ConfigError):
                load_platform(p)

    def test_bad_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "bad.yaml", LOGS_YAML.replace("capability: logs", "capability: wat"))
            with self.assertRaises(ConfigError):
                load_platform(p)

    def test_database_requires_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "db.yaml", DB_YAML.replace("  readonly: true\n", ""))
            with self.assertRaises(ConfigError):
                load_platform(p)

    def test_load_dir_sorted_by_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "db.yaml", DB_YAML)
            _write(tmp, "logs.yaml", LOGS_YAML)
            plats = load_platforms(tmp)
            self.assertEqual([p.capability for p in plats], ["logs", "database"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_config_loader -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'config_loader'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/web-investigator/scripts/config_loader.py
"""平台 YAML 配置加载与校验 → Platform 对象。不含任何具体平台名(D1)。"""
import glob
import os
from dataclasses import dataclass, field
from typing import Optional
import yaml

VALID_CAPABILITIES = {"logs", "trace", "metrics", "database", "other"}
_PRIORITY = {"logs": 0, "trace": 1, "metrics": 2, "database": 3, "other": 4}


class ConfigError(Exception):
    """平台配置非法时抛出。"""


@dataclass
class Platform:
    name: str
    capability: str
    url: str
    query: dict
    extract: dict
    login_check: dict = field(default_factory=dict)
    script: Optional[str] = None


def _require(d: dict, key: str, ctx: str):
    if key not in d or d[key] in (None, "", []):
        raise ConfigError(f"缺少必填字段 {ctx}.{key}")
    return d[key]


def load_platform(path: str) -> Platform:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    name = _require(raw, "name", "根")
    capability = _require(raw, "capability", "根")
    url = _require(raw, "url", "根")
    if capability not in VALID_CAPABILITIES:
        raise ConfigError(f"非法 capability:{capability}(允许 {sorted(VALID_CAPABILITIES)})")

    query = raw.get("query") or {}
    _require(query, "intent", "query")
    _require(query, "inputs", "query")
    extract = raw.get("extract") or {}
    _require(extract, "intent", "extract")
    _require(extract, "yields", "extract")

    if capability == "database" and query.get("readonly") is not True:
        raise ConfigError(f"database 平台 {name} 必须 query.readonly: true(D10 只读约束)")

    login_check = raw.get("login_check") or {}
    if login_check and "logged_in_when" not in login_check:
        raise ConfigError(f"login_check 给出时须含 logged_in_when(平台 {name})")

    return Platform(
        name=name, capability=capability, url=url,
        query=query, extract=extract,
        login_check=login_check, script=raw.get("script"),
    )


def load_platforms(dir_path: str) -> list:
    paths = sorted(glob.glob(os.path.join(dir_path, "*.yaml")) +
                   glob.glob(os.path.join(dir_path, "*.yml")))
    plats = [load_platform(p) for p in paths]
    plats.sort(key=lambda p: _PRIORITY.get(p.capability, 99))
    return plats
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_config_loader -v`
Expected: PASS(5 tests)

- [ ] **Step 5: Commit**

```bash
git add skills/web-investigator/scripts/config_loader.py skills/web-investigator/tests/test_config_loader.py
git commit -m "feat(web-investigator): 平台配置加载与校验 config_loader"
```

---

### Task 3: identifier_store —— 标识流转状态机

**Files:**
- Create: `skills/web-investigator/scripts/identifier_store.py`
- Test: `skills/web-investigator/tests/test_identifier_store.py`

**Interfaces:**
- Consumes: `Platform`(来自 config_loader,用其 `query["inputs"]` / `extract["yields"]`)
- Produces:
  - `class IdentifierStore` —— 持有已知标识 `dict[str, value]`(D13 共享标识流转)
    - `__init__(self, initial: dict | None = None)`
    - `def add(self, key: str, value) -> None` —— 记录/覆盖一个标识
    - `def add_many(self, mapping: dict) -> None`
    - `def has(self, key: str) -> bool`
    - `def get(self, key: str, default=None)`
    - `def can_run(self, platform) -> bool` —— 平台的 `query["inputs"]` 是否都已就绪
    - `def missing_inputs(self, platform) -> list[str]` —— 尚缺的 inputs
    - `def as_dict(self) -> dict` —— 返回标识副本(供报告/模板填充)

- [ ] **Step 1: Write the failing test**

```python
# skills/web-investigator/tests/test_identifier_store.py
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from identifier_store import IdentifierStore
from config_loader import Platform


def _plat(inputs, yields=None):
    return Platform(
        name="p", capability="logs", url="http://x",
        query={"intent": "i", "inputs": inputs},
        extract={"intent": "e", "yields": yields or []},
    )


class TestIdentifierStore(unittest.TestCase):
    def test_add_and_get(self):
        s = IdentifierStore()
        s.add("trace_id", "abc")
        self.assertTrue(s.has("trace_id"))
        self.assertEqual(s.get("trace_id"), "abc")

    def test_initial_seed(self):
        s = IdentifierStore({"order_id": "123"})
        self.assertEqual(s.get("order_id"), "123")

    def test_can_run_true_when_all_inputs_present(self):
        s = IdentifierStore({"trace_id": "abc"})
        self.assertTrue(s.can_run(_plat(["trace_id"])))

    def test_can_run_false_and_missing(self):
        s = IdentifierStore({"trace_id": "abc"})
        p = _plat(["trace_id", "db", "sql"])
        self.assertFalse(s.can_run(p))
        self.assertEqual(s.missing_inputs(p), ["db", "sql"])

    def test_add_many_and_as_dict_is_copy(self):
        s = IdentifierStore()
        s.add_many({"a": 1, "b": 2})
        d = s.as_dict()
        d["a"] = 999
        self.assertEqual(s.get("a"), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_identifier_store -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'identifier_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/web-investigator/scripts/identifier_store.py
"""标识流转状态(D13):跨平台共享的标识集合,决定某平台能否被调用。"""


class IdentifierStore:
    def __init__(self, initial=None):
        self._data = dict(initial or {})

    def add(self, key, value):
        self._data[key] = value

    def add_many(self, mapping):
        self._data.update(mapping)

    def has(self, key):
        return key in self._data and self._data[key] not in (None, "")

    def get(self, key, default=None):
        return self._data.get(key, default)

    def can_run(self, platform):
        return all(self.has(k) for k in platform.query.get("inputs", []))

    def missing_inputs(self, platform):
        return [k for k in platform.query.get("inputs", []) if not self.has(k)]

    def as_dict(self):
        return dict(self._data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_identifier_store -v`
Expected: PASS(5 tests)

- [ ] **Step 5: Commit**

```bash
git add skills/web-investigator/scripts/identifier_store.py skills/web-investigator/tests/test_identifier_store.py
git commit -m "feat(web-investigator): 标识流转状态机 identifier_store (D13)"
```

---

### Task 4: report —— 报告四段拼装

**Files:**
- Create: `skills/web-investigator/scripts/report.py`
- Test: `skills/web-investigator/tests/test_report.py`

**Interfaces:**
- Consumes: 无(纯函数;输入为普通 dict/list)
- Produces:
  - `def build_report(clue: str, symptom: str, root_cause: str, evidence: list[dict], suggestions: list[str]) -> str`
    - `evidence` 每项:`{"platform": str, "capability": str, "finding": str}`
    - 返回 markdown,含四段标题:`## 现象` / `## 根因定位` / `## 证据链` / `## 修复建议`,并在开头附原始线索,结尾附固定免责声明(D9:只出建议不自动修复)。
    - 证据链渲染为有序列表,每条 `[capability] platform:finding`;suggestions 渲染为有序列表。

- [ ] **Step 1: Write the failing test**

```python
# skills/web-investigator/tests/test_report.py
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from report import build_report


class TestReport(unittest.TestCase):
    def setUp(self):
        self.md = build_report(
            clue="订单 123 支付回调失败",
            symptom="回调 500",
            root_cause="下游超时导致回调重试耗尽",
            evidence=[
                {"platform": "日志平台", "capability": "logs", "finding": "SocketTimeout at line 88"},
                {"platform": "库", "capability": "database", "finding": "order 状态仍为 PENDING"},
            ],
            suggestions=["增大下游超时", "回调改异步重试"],
        )

    def test_has_four_sections(self):
        for h in ["## 现象", "## 根因定位", "## 证据链", "## 修复建议"]:
            self.assertIn(h, self.md)

    def test_includes_clue_and_evidence(self):
        self.assertIn("订单 123 支付回调失败", self.md)
        self.assertIn("SocketTimeout at line 88", self.md)
        self.assertIn("[logs]", self.md)

    def test_includes_suggestions(self):
        self.assertIn("增大下游超时", self.md)

    def test_has_no_autofix_disclaimer(self):
        self.assertIn("不自动", self.md)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_report -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'report'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/web-investigator/scripts/report.py
"""排查报告拼装(D9):现象/根因/证据链/修复建议四段 markdown;只出建议,不自动修复。"""

_DISCLAIMER = "> 本报告仅提供排查结论与修复建议,**不自动修改代码、不自动执行修复**。请人工确认后再操作。"


def build_report(clue, symptom, root_cause, evidence, suggestions):
    lines = []
    lines.append("# 排查报告")
    lines.append("")
    lines.append(f"**原始线索**:{clue}")
    lines.append("")
    lines.append("## 现象")
    lines.append(symptom or "(未描述)")
    lines.append("")
    lines.append("## 根因定位")
    lines.append(root_cause or "(未定位到明确根因)")
    lines.append("")
    lines.append("## 证据链")
    if evidence:
        for i, e in enumerate(evidence, 1):
            lines.append(f"{i}. [{e.get('capability','?')}] {e.get('platform','?')}:{e.get('finding','')}")
    else:
        lines.append("(无)")
    lines.append("")
    lines.append("## 修复建议")
    if suggestions:
        for i, s in enumerate(suggestions, 1):
            lines.append(f"{i}. {s}")
    else:
        lines.append("(无)")
    lines.append("")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_report -v`
Expected: PASS(4 tests)

- [ ] **Step 5: Commit**

```bash
git add skills/web-investigator/scripts/report.py skills/web-investigator/tests/test_report.py
git commit -m "feat(web-investigator): 报告四段拼装 report (D9)"
```

---

### Task 5: investigate_cli —— CLI 粘合层

**Files:**
- Create: `skills/web-investigator/scripts/investigate_cli.py`
- Test: `skills/web-investigator/tests/test_investigate_cli.py`

**Interfaces:**
- Consumes: `config_loader.load_platforms`, `sql_guard.assert_readonly`, `identifier_store.IdentifierStore`, `report.build_report`
- Produces: 一个 argparse CLI,供 SKILL.md 编排层调用。子命令:
  - `platforms --config-dir DIR` —— 打印各平台 `name/capability/inputs/yields`(JSON),供编排层了解可用平台
  - `next --config-dir DIR --known k1=v1 k2=v2` —— 给定已知标识,按优先级返回**下一个可运行**平台的 name(证据驱动编排的候选供给;不做"是否够了"判断——那是 SKILL.md 模型职责),无可运行平台则输出空 JSON `{}`
  - `check-sql --sql "..."` —— 调 `assert_readonly`;通过打印 `OK`,拒绝则 stderr 打印原因并 `exit(2)`(D10 命令行兜底)
  - `report --json PATH` —— 从 JSON 文件读 `{clue,symptom,root_cause,evidence,suggestions}`,打印 markdown 报告

**说明:** CLI 只做确定性粘合;不驱动浏览器(浏览器由 SKILL.md 用 chrome-devtools MCP 操作)。`--known` 形如 `trace_id=abc order_id=123`。

- [ ] **Step 1: Write the failing test**

```python
# skills/web-investigator/tests/test_investigate_cli.py
import unittest, subprocess, sys, os, tempfile, json
from pathlib import Path

CLI = str(Path(__file__).resolve().parents[1] / "scripts" / "investigate_cli.py")

DB_YAML = """
name: 测试库
capability: database
url: https://example.internal/db
query:
  intent: "执行 {{sql}}"
  inputs: [sql]
  readonly: true
extract:
  intent: "读结果"
  yields: [rows]
"""
LOGS_YAML = """
name: 测试日志
capability: logs
url: https://example.internal/logs
query:
  intent: "输入 {{trace_id}} 搜索"
  inputs: [trace_id]
extract:
  intent: "提取"
  yields: [service_name]
"""

def run(*args, **kw):
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, **kw)


class TestCli(unittest.TestCase):
    def _cfg(self, tmp):
        for n, c in [("logs.yaml", LOGS_YAML), ("db.yaml", DB_YAML)]:
            with open(os.path.join(tmp, n), "w", encoding="utf-8") as f:
                f.write(c)
        return tmp

    def test_check_sql_ok(self):
        r = run("check-sql", "--sql", "SELECT 1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("OK", r.stdout)

    def test_check_sql_reject(self):
        r = run("check-sql", "--sql", "DELETE FROM t")
        self.assertEqual(r.returncode, 2)

    def test_platforms_lists_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._cfg(tmp)
            r = run("platforms", "--config-dir", tmp)
            data = json.loads(r.stdout)
            caps = {p["capability"] for p in data}
            self.assertEqual(caps, {"logs", "database"})

    def test_next_picks_runnable_by_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._cfg(tmp)
            r = run("next", "--config-dir", tmp, "--known", "trace_id=abc")
            data = json.loads(r.stdout)
            self.assertEqual(data["capability"], "logs")

    def test_next_empty_when_none_runnable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._cfg(tmp)
            r = run("next", "--config-dir", tmp, "--known", "foo=bar")
            self.assertEqual(json.loads(r.stdout), {})

    def test_report_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"clue": "c", "symptom": "s", "root_cause": "rc",
                       "evidence": [{"platform": "P", "capability": "logs", "finding": "f"}],
                       "suggestions": ["fix it"]}
            jp = os.path.join(tmp, "r.json")
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            r = run("report", "--json", jp)
            self.assertIn("## 根因定位", r.stdout)
            self.assertIn("fix it", r.stdout)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_investigate_cli -v`
Expected: FAIL —— CLI 文件不存在 / 各子命令未实现

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
# skills/web-investigator/scripts/investigate_cli.py
"""web-investigator 确定性粘合 CLI。供 SKILL.md 编排层调用;不驱动浏览器。"""
import argparse
import json
import sys

from config_loader import load_platforms
from sql_guard import assert_readonly, SqlWriteRejected
from identifier_store import IdentifierStore
from report import build_report


def _parse_known(pairs):
    known = {}
    for p in pairs or []:
        if "=" in p:
            k, v = p.split("=", 1)
            known[k] = v
    return known


def cmd_platforms(args):
    plats = load_platforms(args.config_dir)
    out = [{"name": p.name, "capability": p.capability,
            "inputs": p.query.get("inputs", []), "yields": p.extract.get("yields", [])}
           for p in plats]
    print(json.dumps(out, ensure_ascii=False))


def cmd_next(args):
    plats = load_platforms(args.config_dir)          # 已按优先级排序
    store = IdentifierStore(_parse_known(args.known))
    for p in plats:
        if store.can_run(p):
            print(json.dumps({"name": p.name, "capability": p.capability,
                              "url": p.url, "intent": p.query.get("intent")},
                             ensure_ascii=False))
            return
    print(json.dumps({}, ensure_ascii=False))


def cmd_check_sql(args):
    try:
        assert_readonly(args.sql)
    except SqlWriteRejected as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    print("OK")


def cmd_report(args):
    with open(args.json, "r", encoding="utf-8") as f:
        d = json.load(f)
    print(build_report(d.get("clue", ""), d.get("symptom", ""), d.get("root_cause", ""),
                       d.get("evidence", []), d.get("suggestions", [])))


def main(argv=None):
    ap = argparse.ArgumentParser(description="web-investigator 确定性 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("platforms"); sp.add_argument("--config-dir", required=True); sp.set_defaults(fn=cmd_platforms)
    sn = sub.add_parser("next"); sn.add_argument("--config-dir", required=True)
    sn.add_argument("--known", nargs="*", default=[]); sn.set_defaults(fn=cmd_next)
    sc = sub.add_parser("check-sql"); sc.add_argument("--sql", required=True); sc.set_defaults(fn=cmd_check_sql)
    sr = sub.add_parser("report"); sr.add_argument("--json", required=True); sr.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/web-investigator && python3 -m unittest tests.test_investigate_cli -v`
Expected: PASS(6 tests)

- [ ] **Step 5: Run the full suite + commit**

```bash
cd skills/web-investigator && python3 -m unittest discover -s tests -v
git add skills/web-investigator/scripts/investigate_cli.py skills/web-investigator/tests/test_investigate_cli.py
git commit -m "feat(web-investigator): 确定性粘合 CLI investigate_cli"
```

---

### Task 6: SKILL.md + references + examples —— 编排层与文档

**Files:**
- Create: `skills/web-investigator/SKILL.md`
- Create: `skills/web-investigator/references/config-schema.md`
- Create: `skills/web-investigator/examples/logs-platform.example.yaml`
- Create: `skills/web-investigator/examples/database-platform.example.yaml`

**说明:** 本任务无单元测试(内容为 prompt 与文档);交付验证 = 人工通读 + 用 examples 跑一次 `investigate_cli platforms/next`。

- [ ] **Step 1: 写 examples(两份脱敏示例配置)**

`examples/logs-platform.example.yaml`:
```yaml
name: 示例-日志平台
capability: logs
url: https://logs.example.internal/search
login_check:
  logged_in_when: "页面出现搜索输入框"
query:
  intent: "在搜索框输入 {{trace_id}},回车搜索,打开最相关的报错日志条目"
  inputs: [trace_id]
extract:
  intent: "提取报错堆栈、发生时间、涉及服务名;若日志含 traceId 一并提取"
  yields: [error_stack, timestamp, service_name, trace_id]
script: null
```

`examples/database-platform.example.yaml`:
```yaml
name: 示例-数据库平台
capability: database
url: https://db.example.internal/console
login_check:
  logged_in_when: "页面出现 SQL 编辑器"
query:
  intent: "选中库 {{db}},在 SQL 框执行 {{sql}}"
  inputs: [db, sql]
  readonly: true
extract:
  intent: "读取结果表格的行"
  yields: [rows]
script: null
```

- [ ] **Step 2: 写 references/config-schema.md**

内容需覆盖:每个字段含义(name/capability/url/login_check/query{intent,inputs,readonly}/extract{intent,yields}/script);capability 五类说明;database 必须 readonly:true(D10);`{{标识}}` 占位符如何被引擎填充(取自 IdentifierStore);script 逃生舱何时用(粗粒度 intent 表达不了时,D14);私有配置不进仓库的约定(D1)。以 examples 两份为范例逐字段讲解。

- [ ] **Step 3: 写 SKILL.md(编排层)**

SKILL.md frontmatter:
```markdown
---
name: web-investigator
description: Use when investigating a production/webapp issue across multiple internal web systems (logs, trace, metrics, database consoles) from a free-text clue — drives the browser via chrome-devtools MCP, cross-validates evidence, and produces a root-cause + fix-suggestion report. Read-only; never auto-fixes.
---
```

正文须包含以下小节(用中文写,自然语言编排指令):
1. **前提**:依赖 chrome-devtools MCP;需本机已登录目标系统的 Chrome(D2)。指向用户私有配置目录(默认 `~/.config/web-investigator/platforms/`,可由用户指定)。
2. **流程**(证据驱动循环 D6):
   - ① 读线索 → 抽标识(traceId/订单号/时间段/报错关键词/服务名/用户ID),存为 known 键值。
   - ② 调 `python3 scripts/investigate_cli.py platforms --config-dir <DIR>` 了解可用平台。
   - ③ **循环**:调 `... next --config-dir <DIR> --known k=v ...` 取下一个可运行平台 → 用 chrome-devtools MCP 按该平台 `intent` 操作(navigate/snapshot/fill/click/wait_for)→ 按 `extract.intent` 从 snapshot 读取信息 → 把新得到的 `yields` 标识加入 known。
   - ④ **每步过评估门**(D6):已有证据是否足以定位根因?够 → 跳出循环出报告(哪怕只查了日志);不够 → 回 ③。无更多可运行平台也跳出。
   - ⑤ 交叉验证(D13 T3):不同平台说法是否一致?数据库真实状态与日志说法是否矛盾?矛盾点写进证据链。
   - ⑥ 出报告:把 clue/symptom/root_cause/evidence/suggestions 写成 JSON,调 `... report --json <PATH>` 生成 markdown 给用户。
3. **安全硬约束**:
   - database 平台执行任何 SQL 前,**必须**先 `python3 scripts/investigate_cli.py check-sql --sql "<SQL>"`;返回非 0 则**停止**,不得在页面 fill 该 SQL(D10)。
   - 只出建议,**绝不**在任何平台执行写/改操作(D9)。
   - 遇未登录(`login_check.logged_in_when` 不满足)/验证码 → 停下,请用户在浏览器手动登录后继续(T6)。
4. **逃生舱**:平台配置带 `script:` 时,按该脚本说明操作而非 `intent`(D14)。

- [ ] **Step 4: 冒烟验证**

```bash
cd skills/web-investigator
python3 scripts/investigate_cli.py platforms --config-dir examples
python3 scripts/investigate_cli.py next --config-dir examples --known trace_id=abc
python3 scripts/investigate_cli.py check-sql --sql "DELETE FROM t"; echo "exit=$?"
```
Expected: platforms 列出两平台;next 返回 logs 平台;check-sql 退出码 2。
(注:examples 文件名为 `*.example.yaml`,glob `*.yaml` 能匹配。)

- [ ] **Step 5: Commit**

```bash
git add skills/web-investigator/SKILL.md skills/web-investigator/references skills/web-investigator/examples
git commit -m "feat(web-investigator): 编排层 SKILL.md + schema 文档 + 示例配置"
```

---

## Self-Review(计划自审结论)

**Spec 覆盖:** D1→全局约束+config_loader 无平台名;D2→SKILL.md 前提;D6→SKILL.md 流程②+`next`;D9→report+SKILL.md 安全;D10→sql_guard+config_loader+check-sql 三层;D11→证据驱动循环(按需);D12→Python 确定性 vs SKILL.md 智能分工;D13→identifier_store;D14→SKILL.md 逃生舱+config `script`。全部有对应任务。

**类型一致性:** `Platform` 字段在 Task 2 定义,Task 3/5 消费一致;`assert_readonly`/`SqlWriteRejected`(T1)在 T5 一致;`build_report` 签名(T4)与 T5 `cmd_report` 调用一致;`IdentifierStore.can_run`(T3)在 T5 `cmd_next` 一致。

**无占位符:** 各代码步骤均为完整可运行代码;Task 6 文档步骤给了明确内容清单与 frontmatter 实体。
