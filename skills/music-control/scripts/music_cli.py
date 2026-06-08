#!/usr/bin/env python3
"""music-cli — macOS 音乐控制 CLI（酷狗 / Spotify）"""
import os
import sys
import json
import re
import threading
import time
import subprocess
import argparse
import urllib.parse
from typing import Optional

from providers import (
    MusicProvider, SpotifyProvider, get_provider,
    MR_CMD_PLAY, MR_CMD_PAUSE, MR_CMD_TOGGLE, MR_CMD_NEXT, MR_CMD_PREV,
)

MOOD_MAP = {
    "focused": ["lofi hip hop", "深夜coding专注", "专注背景音乐"],
    "relaxed": ["轻音乐放松", "咖啡馆背景音乐", "ambient chill"],
    "hyped":   ["运动励志音乐", "电子舞曲", "榜单热歌"],
    "sad":     ["治愈系音乐", "温柔民谣", "安慰系歌曲"],
    "debug":   ["摇滚发泄", "燃烧金属", "punk rock"],
}

# ── 队列持久化 ────────────────────────────────────────────────────────────────
_QUEUE_PATH = os.path.expanduser("~/.config/music-cli/queue.json")


def _load_queue() -> dict:
    try:
        with open(_QUEUE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"songs": [], "current": -1}


def _save_queue(q: dict):
    os.makedirs(os.path.dirname(_QUEUE_PATH), exist_ok=True)
    with open(_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(q, f, indent=2, ensure_ascii=False)


def out(data: dict):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def err(msg: str, code: str = "error"):
    out({"ok": False, "error": code, "message": msg})


# ── 状态检测（通用兜底）──────────────────────────────────────────────────────
def _nowplaying() -> Optional[dict]:
    try:
        r = subprocess.run(["nowplaying-cli", "get-raw"], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        raw = json.loads(r.stdout)
        title = raw.get("kMRMediaRemoteNowPlayingInfoTitle", "")
        if not title:
            return None
        return {
            "title":   title,
            "artist":  raw.get("kMRMediaRemoteNowPlayingInfoArtist", ""),
            "album":   raw.get("kMRMediaRemoteNowPlayingInfoAlbum", ""),
            "playing": float(raw.get("kMRMediaRemoteNowPlayingInfoPlaybackRate", 0)) > 0,
            "source":  "mediaremote",
        }
    except (json.JSONDecodeError, ValueError):
        return None


def _kugou_window() -> Optional[dict]:
    script = '''
tell application "System Events"
    set procs to every process whose name contains "KuGou"
    if length of procs > 0 then
        set p to item 1 of procs
        if (count of windows of p) > 0 then
            return title of window 1 of p
        end if
    end if
end tell
return ""'''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    title = r.stdout.strip()
    if not title:
        return None
    parts = re.split(r"\s*[-—]\s*", title)
    if len(parts) >= 2:
        song, artist = parts[0].strip(), parts[1].strip()
        if song and song not in ("酷狗音乐", "KuGou Music", "KuGou"):
            return {"title": song, "artist": artist, "album": "", "playing": None, "source": "window_title"}
    return None


def detect_status(provider: Optional[MusicProvider] = None) -> Optional[dict]:
    if provider:
        info = provider.status()
        if info:
            return info
    return _nowplaying() or _kugou_window()


# ── 队列辅助 ─────────────────────────────────────────────────────────────────
def _queue_play_next(provider: MusicProvider, q: dict) -> bool:
    """从队列取出下一首播放，返回是否还有后续。"""
    q["current"] += 1
    if q["current"] >= len(q["songs"]):
        q["current"] = len(q["songs"]) - 1
        _save_queue(q)
        return False
    song = q["songs"][q["current"]]
    _save_queue(q)
    _play_song(provider, song)
    return q["current"] < len(q["songs"]) - 1


def _play_song(provider: MusicProvider, song: dict) -> tuple:
    """播放单首歌曲，返回 (ok, url)。"""
    if provider.name == "spotify":
        q_str = f"{song.get('title', '')} {song.get('artist', '')}".strip()
        song_id = f"spotify:search:{urllib.parse.quote(q_str)}"
    else:
        song_id = song.get("id", "")
    if not song_id:
        return False, ""
    return provider.play_song(song_id)


# ── 自动切歌后台线程 ─────────────────────────────────────────────────────────
_auto_next_thread: threading.Thread | None = None
_auto_next_stop: threading.Event = threading.Event()


def _start_auto_next(provider: MusicProvider, interval: float = 5.0):
    global _auto_next_thread, _auto_next_stop
    _auto_next_stop.clear()
    if _auto_next_thread and _auto_next_thread.is_alive():
        return

    def loop():
        while not _auto_next_stop.wait(interval):
            q = _load_queue()
            if q["current"] < 0 or q["current"] >= len(q["songs"]) - 1:
                return
            # 检查是否播完
            if provider.name == "spotify" and isinstance(provider, SpotifyProvider):
                pos, dur = provider.player_position()
                if pos > 0 and dur > 0 and dur - pos < 3:
                    _queue_play_next(provider, q)
                    return
            else:
                # KuGou：用队列中记录的 duration 计时
                song = q["songs"][q["current"]]
                dur = song.get("_end_time", 0)
                if dur > 0 and time.time() >= dur:
                    _queue_play_next(provider, q)
                    return

    _auto_next_thread = threading.Thread(target=loop, daemon=True)
    _auto_next_thread.start()


# ── 子命令 ────────────────────────────────────────────────────────────────────
def cmd_status(args):
    info = detect_status(args.provider_obj)
    if info:
        out({"ok": True, "provider": args.provider_obj.name, **info})
    else:
        err("未检测到正在播放的音乐", "no_player")
    return 0


def cmd_next(args):
    ok = args.provider_obj.control(MR_CMD_NEXT)
    out({"ok": ok, "provider": args.provider_obj.name, "action": "next"})
    return 0


def cmd_prev(args):
    ok = args.provider_obj.control(MR_CMD_PREV)
    out({"ok": ok, "provider": args.provider_obj.name, "action": "prev"})
    return 0


def cmd_play(args):
    ok = args.provider_obj.control(MR_CMD_TOGGLE)
    out({"ok": ok, "provider": args.provider_obj.name, "action": "toggle_play"})
    return 0


def cmd_search(args):
    provider = args.provider_obj
    query = " ".join(args.query)
    page = getattr(args, "page", 1)
    results = provider.search(query, page_size=10, page=page)
    if results:
        out({"ok": True, "provider": provider.name, "query": query,
             "page": page, "results": results})
    else:
        err(f"搜索无结果：{query}", "no_results")
    return 0


def cmd_recommend(args):
    provider = args.provider_obj
    mood = args.mood.lower()
    results, used_kw = [], ""
    for kw in MOOD_MAP[mood]:
        results = provider.search(kw, page_size=6)
        if results:
            used_kw = kw
            break
    out({"ok": True, "provider": provider.name, "mood": mood,
         "keyword": used_kw, "results": results})
    return 0


def cmd_play_by(args):
    provider = args.provider_obj
    queries = args.title
    # 支持用 ||| 分隔多首歌
    songs_to_play = []
    for q in queries:
        parts = q.split("|||")
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if provider.name == "spotify":
                songs_to_play.append({"title": p, "artist": "", "id": f"spotify:search:{urllib.parse.quote(p)}", "duration": 0})
            else:
                results = provider.search(p, page_size=3)
                if results:
                    songs_to_play.append(results[0])

    if not songs_to_play:
        err(f"未找到任何歌曲", "not_found")
        return 1

    if len(songs_to_play) == 1:
        ok, used = _play_song(provider, songs_to_play[0])
        out({"ok": ok, "provider": provider.name, "song": songs_to_play[0], "url": used})
    else:
        # 多首：写入队列，从第一首开始播
        q = _load_queue()
        q["songs"] = songs_to_play
        q["current"] = 0
        _save_queue(q)
        ok, used = _play_song(provider, songs_to_play[0])
        # 设置自动切歌 end_time（当前歌曲 + duration + 2s）
        if songs_to_play[0].get("duration", 0) > 0:
            q = _load_queue()
            if q["current"] < len(q["songs"]):
                q["songs"][q["current"]]["_end_time"] = time.time() + q["songs"][q["current"]].get("duration", 0) + 2
            _save_queue(q)
        _start_auto_next(provider)
        out({"ok": ok, "provider": provider.name, "action": "queue_play",
             "queue_size": len(songs_to_play), "current": 0,
             "songs": [{"title": s["title"], "artist": s.get("artist", "")} for s in songs_to_play]})
    return 0 if ok else 1


def cmd_auth(args):
    if args.provider_obj.name != "spotify":
        err("auth 命令仅支持 --provider spotify", "unsupported")
        return 1
    return 0 if args.provider_obj.auth_login() else 1


# ── 队列命令 ──────────────────────────────────────────────────────────────────
def cmd_queue_show(args):
    q = _load_queue()
    songs = q.get("songs", [])
    current = q.get("current", -1)
    out({
        "ok": True,
        "provider": args.provider_obj.name,
        "current": current,
        "total": len(songs),
        "songs": [
            {"index": i, "title": s.get("title", ""), "artist": s.get("artist", ""),
             "album": s.get("album", ""), "active": i == current}
            for i, s in enumerate(songs)
        ]
    })
    return 0


def cmd_queue_add(args):
    provider = args.provider_obj
    queries = args.title
    songs_to_add = []
    for q_str in queries:
        parts = q_str.split("|||")
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if provider.name == "spotify":
                songs_to_add.append({"title": p, "artist": "", "id": f"spotify:search:{urllib.parse.quote(p)}", "duration": 0})
            else:
                results = provider.search(p, page_size=3)
                if results:
                    songs_to_add.append(results[0])

    if not songs_to_add:
        err("未找到任何歌曲", "not_found")
        return 1

    q = _load_queue()
    q["songs"].extend(songs_to_add)
    _save_queue(q)
    out({
        "ok": True,
        "provider": provider.name,
        "action": "queue_add",
        "added": len(songs_to_add),
        "queue_total": len(q["songs"]),
        "songs": [{"title": s["title"], "artist": s.get("artist", "")} for s in songs_to_add],
    })
    return 0


def cmd_queue_clear(args):
    _save_queue({"songs": [], "current": -1})
    out({"ok": True, "action": "queue_clear"})


def cmd_queue_play(args):
    provider = args.provider_obj
    q = _load_queue()
    songs = q.get("songs", [])
    if not songs:
        err("队列为空，请先用 queue-add 添加歌曲", "empty_queue")
        return 1

    start_index = args.index if hasattr(args, "index") else (q.get("current", -1) + 1)
    if start_index < 0:
        start_index = 0
    if start_index >= len(songs):
        start_index = 0

    q["current"] = start_index - 1
    _save_queue(q)
    has_next = _queue_play_next(provider, q)

    # 记录当前歌曲结束时间，用于自动切歌
    if songs[q["current"]].get("duration", 0) > 0:
        q = _load_queue()
        if q["current"] < len(q["songs"]):
            q["songs"][q["current"]]["_end_time"] = time.time() + q["songs"][q["current"]].get("duration", 0) + 2
        _save_queue(q)

    _start_auto_next(provider)
    out({
        "ok": True,
        "provider": provider.name,
        "action": "queue_play",
        "current": q["current"],
        "total": len(songs),
        "has_next": has_next,
    })
    return 0


def cmd_queue_next(args):
    provider = args.provider_obj
    q = _load_queue()
    songs = q.get("songs", [])
    if not songs or q.get("current", -1) < 0:
        # 无队列，执行普通 next
        return cmd_next(args)

    q["current"] += 1
    if q["current"] >= len(songs):
        q["current"] = len(songs) - 1
        _save_queue(q)
        out({"ok": True, "provider": provider.name, "action": "queue_end", "message": "队列已播完"})
        return 0

    _save_queue(q)
    ok, _ = _play_song(provider, songs[q["current"]])
    has_next = q["current"] < len(songs) - 1
    out({
        "ok": ok,
        "provider": provider.name,
        "action": "queue_next",
        "current": q["current"],
        "song": {"title": songs[q["current"]].get("title", ""),
                 "artist": songs[q["current"]].get("artist", "")},
        "has_next": has_next,
    })
    return 0


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="music-cli",
        description="macOS 音乐控制 CLI（酷狗 / Spotify）",
    )
    parser.add_argument(
        "--provider", choices=["kugou", "spotify"],
        default=os.environ.get("MUSIC_PROVIDER", "spotify"),
        help="音乐源（默认 spotify；可用 MUSIC_PROVIDER 环境变量覆盖）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth",   help="Spotify 授权登录（仅 --provider spotify）")
    sub.add_parser("status", help="当前播放信息")
    sub.add_parser("next",   help="下一首 / 队列下一首")
    sub.add_parser("prev",   help="上一首")
    sub.add_parser("play",   help="播放 / 暂停")

    rec = sub.add_parser("recommend", help="按心情推荐歌曲")
    rec.add_argument("mood", choices=list(MOOD_MAP), metavar="mood",
                     help="心情：" + " / ".join(MOOD_MAP))

    sch = sub.add_parser("search", help="搜索歌曲")
    sch.add_argument("query", nargs="+")
    sch.add_argument("--page", type=int, default=1, help="分页页码（默认 1）")

    pb = sub.add_parser("play-by", help="搜索并播放（支持多首，用 ||| 分隔）")
    pb.add_argument("title", nargs="+", help="歌名（可加艺术家，多首用 ||| 分隔）")

    op = sub.add_parser("open", help="同 play-by")
    op.add_argument("title", nargs="+")

    # 队列命令
    sub.add_parser("queue-show",  help="显示队列")
    sub.add_parser("queue-clear", help="清空队列")

    qa = sub.add_parser("queue-add", help="添加歌曲到队列（支持多首，用 ||| 分隔）")
    qa.add_argument("title", nargs="+")

    qp = sub.add_parser("queue-play", help="从队列播放")
    qp.add_argument("index", nargs="?", type=int, default=None,
                    help="从第几首开始（0 起，跳过则从当前下一首继续）")

    sub.add_parser("queue-next", help="队列下一首")

    args = parser.parse_args()
    args.provider_obj = get_provider(args.provider)

    fn = {
        "auth":        cmd_auth,
        "status":      cmd_status,
        "next":        cmd_next,
        "prev":        cmd_prev,
        "play":        cmd_play,
        "recommend":   cmd_recommend,
        "search":      cmd_search,
        "play-by":     cmd_play_by,
        "open":        cmd_play_by,
        "queue-show":  cmd_queue_show,
        "queue-add":   cmd_queue_add,
        "queue-clear": cmd_queue_clear,
        "queue-play":  cmd_queue_play,
        "queue-next":  cmd_queue_next,
    }[args.command]
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
