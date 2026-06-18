"""
每日简报 Loop。

plan    → 并发抓取：天气 + 今日头条热榜 + GitHub Trending + Hacker News + 36kr
execute → Claude 整理内容，动态决定当天 HTML 风格，生成完整 HTML
verify  → 检查 HTML 长度
fix     → 内容过短时重新生成
report  → Telegram Bot 发送 HTML 文件
"""

import concurrent.futures
import json
import logging
import os
import re
import tempfile
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from claude_client import get_client, get_model

from loops.base import BaseLoop
import notifier

log = logging.getLogger(__name__)

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

    def _call_claude(self, prompt: str, max_tokens: int = 4096) -> str:
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

    def plan(self, goal: dict) -> dict:
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

    def _generate_english_block(self, today: str) -> str:
        """单独生成每日英文模块 HTML 片段，确保不被遗漏。"""
        result = self._call_claude(f"""今天是 {today}。

请生成一个「每日英文」HTML 模块片段（不需要完整 HTML 文档，只需要一个 <section> 或 <div>）。

内容要求，四项缺一不可：
1. 一句实用的英文原句（职场/生活场景）
2. 中文翻译
3. 使用场景说明（什么时候用这句话）
4. 一个完整的英文例句

样式要求：
- 内联样式，背景色用渐变或亮色突出显示
- 每项前加对应 emoji：📝 英文原句  🌐 中文翻译  💡 使用场景  ✏️ 例句
- 移动端友好

只输出 HTML 片段，不要任何解释。""", max_tokens=1024)

        # 清理可能的代码块包裹
        if result.startswith("```"):
            parts = result.split("```")
            result = parts[1].lstrip("html").strip() if len(parts) > 1 else result
        return result

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

    def execute(self, context: dict) -> dict:
        today = context["today"]
        weather = context["weather"]
        toutiao = context["toutiao"]
        github = context["github"]
        hn = context["hackernews"]
        kr = context["36kr"]

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

注意：不需要生成每日英文板块，会单独插入。
HTML 末尾留一个注释占位：<!-- ENGLISH_BLOCK -->

**技术要求**：
- 完整 HTML 文档，含 <!DOCTYPE html>，</body></html> 结尾
- 所有样式写在 <style> 标签内，不依赖外部资源
- 移动端友好，链接颜色醒目
- 所有 <a> 标签加 target="_blank"

只输出 HTML，不要任何解释文字。"""

        html = self._call_claude(prompt, max_tokens=8192)

        if not html.startswith("<!"):
            match = re.search(r"<!DOCTYPE.*", html, re.DOTALL | re.IGNORECASE)
            if match:
                html = match.group(0)

        # ── 第二步：单独生成每日英文，强制插入 ───────────────
        log.info("生成每日英文模块...")
        english_block = self._generate_english_block(today)

        # 插入到 </body> 前，如果有占位注释就替换，否则直接插入
        if "<!-- ENGLISH_BLOCK -->" in html:
            html = html.replace("<!-- ENGLISH_BLOCK -->", english_block)
        else:
            html = html.replace("</body>", f"\n{english_block}\n</body>")

        return {"html": html, "today": today}

    def verify(self, result: dict) -> tuple[bool, str]:
        html = result.get("html", "")
        if len(html) < 3000:
            return False, "HTML 内容过短，需要重新生成"
        return True, ""

    def fix(self, result: dict, issues: str) -> dict:
        log.info(f"HTML 验证不通过（{issues}），重新生成...")
        prompt = f"""上次生成的简报问题：{issues}

请重新生成完整的每日简报 HTML，必须包含全部10个板块：
今日重点、每日一句、今日天气、今日头条热榜、GitHub推荐、HN精选、36kr快讯、今日一问、每日英文。
每日英文必须包含英文原句/中文翻译/使用场景/例句，板块顺序自由决定。

只输出 HTML，不要解释。"""
        html = self._call_claude(prompt, max_tokens=8192)
        result["html"] = html
        return result

    def report(self, result: dict) -> str:
        html = result.get("html", "")
        today = result.get("today", "")

        with tempfile.NamedTemporaryFile(
            suffix=".html",
            prefix=f"briefing_{datetime.now().strftime('%Y%m%d')}_",
            delete=False,
            mode="w",
            encoding="utf-8",
        ) as f:
            f.write(html)
            tmp_path = f.name

        try:
            notifier.notify_telegram_document(
                file_path=tmp_path,
                caption=f"📰 {today} 每日简报",
            )
        finally:
            os.unlink(tmp_path)

        return f"每日简报已发送：{today}"
