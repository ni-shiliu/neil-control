"""
每日简报 Loop。

plan    → 并发抓取：天气 + 今日头条热榜 + GitHub Trending + Hacker News + 36kr
execute → Claude 整理内容，动态决定当天 HTML 风格，生成完整 HTML
verify  → 检查 HTML 长度
fix     → 内容过短时重新生成
report  → 返回摘要，由 Engine 统一分发 Telegram 文件通知
"""

import concurrent.futures
import json
import logging
import os
import re
from html import unescape
from datetime import datetime
from typing import TYPE_CHECKING

import requests
from bs4 import BeautifulSoup

from loops.base import BaseLoop

if TYPE_CHECKING:
    from engine.context import RunContext

log = logging.getLogger(__name__)

DEFAULT_BANNED_ENGLISH_PHRASES = [
    "Let's circle back on this later.",
    "Let's touch base next week.",
    "Keep me posted.",
    "Feel free to reach out.",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


class DailyBriefingLoop(BaseLoop):

    name = "daily_briefing_loop"
    description = "每日简报：抓取天气+头条+GitHub+HN+36kr，Claude 生成动态 HTML 简报，Telegram 发送"
    required_tools = ["claude", "telegram"]
    supported_trigger_modes = ("cron",)

    @staticmethod
    def _output_name(today: str) -> str:
        safe_today = today.replace(" ", "_")
        return f"daily_briefing_{safe_today}.html"

    @staticmethod
    def _delivery_effect_key(today: str, run_id: str) -> str:
        safe_today = today.replace(" ", "_")
        return f"briefing:{safe_today}:{run_id}:telegram_document"

    def _set_output(self, result: dict, html: str, today: str, ctx: "RunContext | None" = None) -> None:
        output = {
            "output_type": "briefing_html",
            "name": self._output_name(today),
            "content": html,
            "meta": {
                "today": today,
                "delivery_channel": "telegram_document",
                "mime_type": "text/html",
            },
        }
        result["outputs"] = [output]

    def _queue_delivery_effect(self, result: dict, today: str, ctx: "RunContext | None" = None) -> None:
        if not ctx:
            return
        preferences = self._resolved_preferences(ctx)
        delivery_channels = preferences.get("delivery", {}).get("channels", [])
        if isinstance(delivery_channels, list) and delivery_channels:
            normalized_channels = {str(channel).strip().lower() for channel in delivery_channels if str(channel).strip()}
            if normalized_channels.isdisjoint({"telegram", "telegram_document"}):
                return
        output = next(
            (a for a in result.get("outputs", []) if a.get("output_type") == "briefing_html"),
            None,
        )
        if not output:
            return

        ctx.effects.add(
            "send_telegram_document",
            {
                "content": output["content"],
                "suffix": ".html",
                "prefix": f"briefing_{datetime.now().strftime('%Y%m%d')}_",
                "caption": f"📰 {today} 每日简报",
            },
            {
                "success_bucket": "deliveries",
                "success_item": {"channel": "telegram_document", "today": today},
                "failure_item": {"channel": "telegram_document", "today": today},
            },
            idempotency_key=self._delivery_effect_key(today, ctx.run_id),
        )

    def extract_memory(self, result: dict, old_memory: dict) -> dict:
        from datetime import datetime
        totals = dict(old_memory.get("totals", {}))
        totals["runs"] = int(totals.get("runs", 0)) + 1
        totals["outputs"] = int(totals.get("outputs", 0)) + len(result.get("outputs", []))
        totals["deliveries"] = int(totals.get("deliveries", 0)) + len(result.get("deliveries", []))
        return {
            **old_memory,
            "totals": totals,
            "last_updated_at": datetime.now().isoformat(),
        }

    def extract_goal_memory(self, result: dict, old_memory: dict) -> dict:
        from datetime import datetime
        today = result.get("today", "")
        outputs = result.get("outputs", [])
        deliveries = result.get("deliveries", [])
        english_phrase = result.get("english_phrase", "")
        recent_briefings = list(old_memory.get("recent_briefings", []))
        recent_entry = {
            "today": today,
            "output_count": len(outputs),
            "delivery_count": len(deliveries),
            "updated_at": datetime.now().isoformat(),
        }
        recent_briefings.append(recent_entry)
        last_output = next(iter(outputs), {})
        last_summary = (
            f"today={today or '-'} "
            f"outputs={len(outputs)} "
            f"deliveries={len(deliveries)}"
        )
        recent_english_phrases = [
            phrase
            for phrase in (
                list(old_memory.get("recent_english_phrases", [])) + [english_phrase]
            )
            if isinstance(phrase, str) and phrase.strip()
        ]
        return {
            **old_memory,
            "last_today": today,
            "last_output_name": last_output.get("name", ""),
            "last_delivery_count": len(deliveries),
            "last_summary": last_summary,
            "recent_briefings": recent_briefings[-5:],
            "last_english_phrase": english_phrase,
            "recent_english_phrases": recent_english_phrases[-5:],
        }

    def _call_claude(
        self,
        prompt: str,
        max_tokens: int = 4096,
        ctx: "RunContext | None" = None,
    ) -> str:
        if ctx and ctx.tools.claude:
            return ctx.tools.claude.complete(prompt, max_tokens=max_tokens)

        from claude_client import get_client, get_model

        msg = get_client().messages.create(
            model=get_model(),
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    # ── 数据抓取 ─────────────────────────────────────────

    def _fetch_weather(self) -> dict:
        """和风天气免费 API。"""
        try:
            key = os.environ.get("QWEATHER_API_KEY", "")
            city = os.environ.get("WEATHER_CITY_ID", "101010100")
            if not key:
                return {"error": "未配置 QWEATHER_API_KEY"}
            api_host = os.environ.get("QWEATHER_API_HOST", "devapi.qweather.com")
            url = f"https://{api_host}/v7/weather/now?location={city}&key={key}&lang=zh"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if data.get("code") != "200":
                return {"error": f"天气API错误: code={data.get('code')}"}
            now = data.get("now", {})
            return {
                "temp": now.get("temp", "--"),
                "feels_like": now.get("feelsLike", "--"),
                "text": now.get("text", "--"),
                "humidity": now.get("humidity", "--"),
                "wind": f"{now.get('windDir', '')} {now.get('windScale', '')}级",
                "city": city,
            }
        except Exception as e:
            log.warning(f"天气抓取失败: {e}")
            return {"error": str(e)}

    def _fetch_toutiao(self) -> list[dict]:
        """今日头条热榜 — 抓取头条热榜页面。"""
        try:
            url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
            resp = requests.get(url, headers={
                **HEADERS,
                "Referer": "https://www.toutiao.com/",
            }, timeout=10)
            data = resp.json()
            items = data.get("data", [])[:10]
            return [
                {
                    "title": item.get("Title", ""),
                    "hot": str(item.get("HotValue", "")),
                    "url": item.get("Url", ""),
                }
                for item in items if item.get("Title")
            ]
        except Exception as e:
            log.warning(f"今日头条热榜抓取失败: {e}")
            return []

    def _fetch_github_trending(self) -> list[dict]:
        """GitHub Trending 今日榜单。"""
        try:
            url = "https://github.com/trending?since=daily"
            resp = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            repos = []
            for article in soup.select("article.Box-row")[:8]:
                name_tag = article.select_one("h2 a")
                desc_tag = article.select_one("p")
                stars_tag = article.select_one("a[href$='/stargazers']")
                lang_tag = article.select_one("[itemprop='programmingLanguage']")
                if not name_tag:
                    continue
                path = name_tag.get("href", "").strip("/")
                repos.append({
                    "name": name_tag.get_text(strip=True).replace("\n", "").replace(" ", ""),
                    "url": f"https://github.com/{path}",
                    "description": desc_tag.get_text(strip=True) if desc_tag else "",
                    "stars": stars_tag.get_text(strip=True) if stars_tag else "",
                    "language": lang_tag.get_text(strip=True) if lang_tag else "",
                })
            return repos
        except Exception as e:
            log.warning(f"GitHub Trending 抓取失败: {e}")
            return []

    def _fetch_hackernews(self) -> list[dict]:
        """Hacker News Top Stories — 并发抓取。"""
        try:
            ids_resp = requests.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15
            )
            ids = ids_resp.json()[:8]

            def fetch_one(sid):
                try:
                    return requests.get(
                        f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                        timeout=10,
                    ).json()
                except Exception:
                    return {}

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                items = list(ex.map(fetch_one, ids))

            return [
                {
                    "title": item.get("title", ""),
                    "score": item.get("score", 0),
                    "url": item.get("url", ""),
                    "comments": item.get("descendants", 0),
                }
                for item in items if item.get("title")
            ]
        except Exception as e:
            log.warning(f"Hacker News 抓取失败: {e}")
            return []

    def _fetch_36kr(self) -> list[dict]:
        """36kr 快讯 RSS。"""
        try:
            resp = requests.get("https://36kr.com/feed", headers=HEADERS, timeout=10)
            soup = BeautifulSoup(resp.text, "xml")
            items = soup.find_all("item")[:6]
            return [
                {
                    "title": item.find("title").get_text() if item.find("title") else "",
                    "desc": re.sub(r"<[^>]+>", "", item.find("description").get_text())[:100]
                    if item.find("description") else "",
                    "url": item.find("link").get_text() if item.find("link") else "",
                }
                for item in items
            ]
        except Exception as e:
            log.warning(f"36kr 抓取失败: {e}")
            return []

    # ── BaseLoop 实现 ────────────────────────────────────

    def plan(self, goal: dict, ctx=None) -> dict:
        log.info("并发抓取数据源...")
        today = datetime.now().strftime("%Y年%m月%d日 %A")

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                "weather":    executor.submit(self._fetch_weather),
                "toutiao":    executor.submit(self._fetch_toutiao),
                "github":     executor.submit(self._fetch_github_trending),
                "hackernews": executor.submit(self._fetch_hackernews),
                "36kr":       executor.submit(self._fetch_36kr),
            }
            results = {k: f.result() for k, f in futures.items()}

        log.info(
            f"数据抓取完成 | 头条:{len(results['toutiao'])} "
            f"GitHub:{len(results['github'])} HN:{len(results['hackernews'])} "
            f"36kr:{len(results['36kr'])}"
        )
        return {"today": today, **results}

    def _generate_english_block(self, today: str, ctx: "RunContext | None" = None) -> str:
        """单独生成每日英文模块 HTML 片段，确保不被遗漏。"""
        banned_phrases = self._collect_recent_english_phrases(ctx)
        banned_lines = "\n".join(f"- {phrase}" for phrase in banned_phrases[:20])
        preferences = self._resolved_preferences(ctx)
        english_style = preferences.get("format", {}).get("english_phrase_style", "")
        english_extra = f"\n补充风格偏好：{english_style}" if english_style else ""
        result = self._call_claude(f"""今天是 {today}。

请生成一个「每日英文」HTML 模块片段（不需要完整 HTML 文档，只需要一个 <section> 或 <div>）。

内容要求，四项缺一不可：
1. 一句实用的英文原句（职场/生活场景）
2. 中文翻译
3. 使用场景说明（什么时候用这句话）
4. 一个完整的英文例句

去重和风格要求：
- 不要重复使用最近已经出现过的英文原句
- 避免高频商务黑话和套话，尤其不要使用下面这些表达
{banned_lines}
- 优先选择更自然、具体、像真人会说的话
- 职场和生活场景都可以，但不要总是会议/跟进类表达
{english_extra}

样式要求：
- 内联样式，背景色用渐变或亮色突出显示
- 每项前加对应 emoji：📝 英文原句  🌐 中文翻译  💡 使用场景  ✏️ 例句
- 移动端友好

只输出 HTML 片段，不要任何解释。""", max_tokens=1024, ctx=ctx)

        # 清理可能的代码块包裹
        if result.startswith("```"):
            parts = result.split("```")
            result = parts[1].lstrip("html").strip() if len(parts) > 1 else result
        return result

    @staticmethod
    def _merge_preferences(base: dict, overrides: dict) -> dict:
        merged = dict(base)
        for key, value in overrides.items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = DailyBriefingLoop._merge_preferences(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _resolved_preferences(self, ctx: "RunContext | None" = None) -> dict:
        if not ctx:
            return {}
        loop_preferences = ctx.memory.get("preferences", {})
        goal_preferences = ctx.goal_memory.get("preferences", {})
        if not isinstance(loop_preferences, dict):
            loop_preferences = {}
        if not isinstance(goal_preferences, dict):
            goal_preferences = {}
        return self._merge_preferences(loop_preferences, goal_preferences)

    def _collect_recent_english_phrases(self, ctx: "RunContext | None" = None) -> list[str]:
        phrases: list[str] = []
        seen: set[str] = set()

        def _push(value: str) -> None:
            if not isinstance(value, str):
                return
            normalized = value.strip()
            if not normalized:
                return
            key = normalized.casefold()
            if key in seen:
                return
            seen.add(key)
            phrases.append(normalized)

        for phrase in DEFAULT_BANNED_ENGLISH_PHRASES:
            _push(phrase)

        if not ctx:
            return phrases

        for phrase in ctx.goal_memory.get("recent_english_phrases", []):
            _push(phrase)

        for bucket in ("goal_recent_runs", "loop_recent_runs"):
            for run in ctx.recent_runs.get(bucket, []):
                if not isinstance(run, dict):
                    continue
                result = run.get("result", {})
                if isinstance(result, dict):
                    _push(result.get("english_phrase", ""))

        return phrases

    @staticmethod
    def _extract_english_phrase(block_html: str) -> str:
        text = unescape(re.sub(r"<[^>]+>", "\n", block_html))
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            normalized = line.replace("📝", "").replace("英文原句", "").strip("：: ").strip()
            if normalized and any(ch.isalpha() for ch in normalized):
                return normalized
        return ""

    def _find_top_story_url(self, toutiao: list, hn: list, github: list) -> str:
        """从数据中找今日重点的 URL。"""
        for item in hn:
            if item.get("url") and item.get("score", 0) > 100:
                return item["url"]
        for item in toutiao:
            if item.get("url"):
                return item["url"]
        for item in github:
            if item.get("url"):
                return item["url"]
        return ""

    def execute(self, context: dict, ctx=None) -> dict:
        today = context["today"]
        weather = context["weather"]
        toutiao = context["toutiao"]
        github = context["github"]
        hn = context["hackernews"]
        kr = context["36kr"]
        preferences = self._resolved_preferences(ctx)
        topic_bias = preferences.get("content", {}).get("topic_bias", [])
        extra_sections = preferences.get("content", {}).get("extra_sections", [])
        title_lang = preferences.get("format", {}).get("title_lang", "")
        body_lang = preferences.get("format", {}).get("body_lang", "")
        include_english_phrase = preferences.get("content", {}).get("include_english_phrase", True)

        # 找今日重点的 URL，传给 Claude 强制使用
        top_url = self._find_top_story_url(toutiao, hn, github)

        # ── 第一步：生成主体 HTML ───────────────────────────
        prompt = f"""今天是 {today}。

你是一位有品味的内容编辑，请基于以下数据生成一份今日个人简报 HTML。

---
【天气数据】
{json.dumps(weather, ensure_ascii=False)}

【今日头条热榜】
{json.dumps(toutiao, ensure_ascii=False, indent=2)}

【GitHub Trending】
{json.dumps(github, ensure_ascii=False, indent=2)}

【Hacker News Top】
{json.dumps(hn, ensure_ascii=False, indent=2)}

【36kr 快讯】
{json.dumps(kr, ensure_ascii=False, indent=2)}

【用户偏好】
topic_bias={json.dumps(topic_bias, ensure_ascii=False)}
extra_sections={json.dumps(extra_sections, ensure_ascii=False)}
title_lang={title_lang or "auto"}
body_lang={body_lang or "auto"}
include_english_phrase={json.dumps(include_english_phrase, ensure_ascii=False)}

---

生成要求：

**风格**：根据今天内容基调动态决定视觉风格，不要每天都一样。
技术热点多 → 深色代码风；社会新闻多 → 报纸头版风；平静的一天 → 简洁清爽风。

**必须包含以下板块**（顺序版式完全自由发挥）：

1. 今日重点 — 从所有来源提炼最值得关注的一件事，放最显眼位置。
   标题必须是可点击链接：<a href="{top_url}" target="_blank">标题文字</a>
2. 每日一句 — 根据今天基调写一句话（励志/幽默/神评皆可）
3. 今日天气 — 含穿衣建议，有温度体感描述
4. 今日头条热榜 — 选5-8条，每条标题用 <a href="url" target="_blank"> 链接
5. GitHub 今日推荐 — 挑2-3个项目，多写推荐理由，标题加 <a href="url" target="_blank"> 链接
6. Hacker News 精选 — 选3-5条，每条标题加 <a href="url" target="_blank"> 链接
7. 36kr 快讯 — 选3-5条，每条标题加 <a href="url" target="_blank"> 链接
8. 今日一问 — 根据热点提一个有意思的问题

偏好要求：
- 如果 topic_bias 非空，优先提高这些主题的权重，但不要脱离当天真实数据乱编内容
- 如果 title_lang=英文，则大标题和主要板块标题优先英文或中英混排
- 如果 body_lang=中文，则正文保持中文为主
- 如果 extra_sections 非空，可以择优增加 1 个附加板块

注意：不需要生成每日英文板块，会单独插入。
HTML 末尾留一个注释占位：<!-- ENGLISH_BLOCK -->

**技术要求**：
- 完整 HTML 文档，含 <!DOCTYPE html>，</body></html> 结尾
- 所有样式写在 <style> 标签内，不依赖外部资源
- 移动端友好，链接颜色醒目
- 所有 <a> 标签加 target="_blank"

只输出 HTML，不要任何解释文字。"""

        html = self._call_claude(prompt, max_tokens=8192, ctx=ctx)

        if not html.startswith("<!"):
            match = re.search(r"<!DOCTYPE.*", html, re.DOTALL | re.IGNORECASE)
            if match:
                html = match.group(0)

        # ── 第二步：单独生成每日英文，强制插入 ───────────────
        log.info("生成每日英文模块...")
        english_block = ""
        english_phrase = ""
        if include_english_phrase is not False:
            english_block = self._generate_english_block(today, ctx=ctx)
            english_phrase = self._extract_english_phrase(english_block)

        # 插入到 </body> 前，如果有占位注释就替换，否则直接插入
        if "<!-- ENGLISH_BLOCK -->" in html:
            html = html.replace("<!-- ENGLISH_BLOCK -->", english_block)
        else:
            html = html.replace("</body>", f"\n{english_block}\n</body>")

        result = {
            "html": html,
            "today": today,
            "deliveries": [],
            "english_phrase": english_phrase,
        }
        self._set_output(result, html, today, ctx=ctx)
        self._queue_delivery_effect(result, today, ctx=ctx)
        return result

    def verify(self, result: dict) -> tuple[bool, str]:
        html = result.get("html", "")
        if len(html) < 3000:
            return False, "HTML 内容过短，需要重新生成"
        return True, ""

    def fix(self, result: dict, issues: str, ctx=None) -> dict:
        log.info(f"HTML 验证不通过（{issues}），重新生成...")
        prompt = f"""上次生成的简报问题：{issues}

请重新生成完整的每日简报 HTML，必须包含全部10个板块：
今日重点、每日一句、今日天气、今日头条热榜、GitHub推荐、HN精选、36kr快讯、今日一问、每日英文。
每日英文必须包含英文原句/中文翻译/使用场景/例句，板块顺序自由决定。

只输出 HTML，不要解释。"""
        html = self._call_claude(prompt, max_tokens=8192, ctx=ctx)
        result["html"] = html
        result.setdefault("deliveries", [])
        result.setdefault("english_phrase", "")
        self._set_output(result, html, result.get("today", ""), ctx=ctx)
        self._queue_delivery_effect(result, result.get("today", ""), ctx=ctx)
        return result

    def report(self, result: dict) -> str:
        today = result.get("today", "")
        if result.get("deliveries"):
            return f"每日简报已发送：{today}"
        return f"每日简报已生成：{today}"

    def build_notifications(
        self,
        result: dict,
        summary: str,
        ctx: "RunContext | None" = None,
    ) -> list[dict]:
        return []
