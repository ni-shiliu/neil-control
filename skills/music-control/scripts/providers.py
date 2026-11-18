#!/usr/bin/env python3
"""music-cli provider 抽象层：KuGouProvider / SpotifyProvider。"""
import os
import json
import platform
import subprocess
import time
import uuid
import urllib.request
import urllib.parse
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Optional

# ── MediaRemote 命令常量 ──────────────────────────────────────────────────────
MR_CMD_PLAY   = 0
MR_CMD_PAUSE  = 1
MR_CMD_TOGGLE = 2
MR_CMD_NEXT   = 3
MR_CMD_PREV   = 4


# ── 抽象接口 ──────────────────────────────────────────────────────────────────
class MusicProvider(ABC):
    name: str = ""
    proc_name: str = ""

    @abstractmethod
    def search(self, keyword: str, page_size: int = 8, page: int = 1) -> list:
        """返回 [{title, artist, album, id, duration}]，失败返回 []。"""

    @abstractmethod
    def play_song(self, song_id: str) -> tuple:
        """播放指定歌曲，返回 (ok: bool, url: str)。"""

    @abstractmethod
    def control(self, mr_cmd: int) -> bool:
        """执行 next/prev/toggle/play/pause。"""

    def status(self) -> Optional[dict]:
        """返回 {title, artist, album, playing, source}，无则 None。"""
        return None


# ── KuGou：macOS Accessibility 后台桥 ─────────────────────────────────────────
_KUGOU_AX_SOURCE = Path(__file__).with_name("kugou_ax.m")
_KUGOU_AX_BINARY = Path.home() / ".cache" / "music-cli" / "kugou-ax"
_KUGOU_APP = Path("/Applications/KugouMusic.app")


def _ensure_kugou_ax() -> Optional[Path]:
    """按需编译原生 AX 桥；不依赖 PyObjC、私有 MediaRemote 或酷狗网络接口。"""
    try:
        if (
            _KUGOU_AX_BINARY.exists()
            and _KUGOU_AX_BINARY.stat().st_mtime >= _KUGOU_AX_SOURCE.stat().st_mtime
        ):
            return _KUGOU_AX_BINARY
        _KUGOU_AX_BINARY.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "xcrun", "clang", "-fobjc-arc",
                "-framework", "AppKit",
                "-framework", "ApplicationServices",
                str(_KUGOU_AX_SOURCE), "-o", str(_KUGOU_AX_BINARY),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return _KUGOU_AX_BINARY if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _run_kugou_ax(binary: Path, args: tuple[str, ...]) -> dict:
    try:
        result = subprocess.run(
            [str(binary), *args],
            capture_output=True,
            text=True,
            timeout=12,
        )
        return json.loads(result.stdout) if result.stdout.strip() else {
            "ok": False, "error": "ax_helper_no_output",
        }
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {"ok": False, "error": "ax_helper_failed"}


def _launch_kugou() -> bool:
    """正常打开酷狗；仅首次启动可见，后续操作仍保持后台。"""
    if not _KUGOU_APP.exists():
        return False
    try:
        result = subprocess.run(
            ["open", "-a", str(_KUGOU_APP)],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _kugou_ax(*args: str) -> dict:
    binary = _ensure_kugou_ax()
    if not binary:
        return {"ok": False, "error": "ax_helper_build_failed"}

    data = _run_kugou_ax(binary, args)
    if data.get("error") != "kugou_not_running":
        return data
    if not _launch_kugou():
        return data

    transient_errors = {
        "kugou_not_running",
        "search_field_not_found",
        "control_not_found",
        "no_player",
    }
    for _ in range(24):
        time.sleep(0.5)
        data = _run_kugou_ax(binary, args)
        if data.get("ok") or data.get("error") not in transient_errors:
            return data
    return data


class KuGouProvider(MusicProvider):
    name = "kugou"
    proc_name = "KugouMusic"

    def prepare_search(self, keyword: str) -> dict:
        """后台写入搜索词并触发联想；最终提交由 Computer Use 发送 Return。"""
        return _kugou_ax("search", keyword)

    def search(self, keyword: str, page_size: int = 8, page: int = 1) -> list:
        # KuGou's search WebView needs a real UI key event. The skill handles
        # that through Computer Use, then this provider can read the page.
        data = _kugou_ax("results")
        if not data.get("ok"):
            return []
        results = []
        for item in data.get("results", [])[:page_size]:
            parts = [p for p in item.get("parts", []) if p.lower() != "mv"]
            if len(parts) < 3:
                continue
            duration_text = parts[0]
            try:
                minutes, seconds = duration_text.split(":", 1)
                duration = int(minutes) * 60 + int(seconds)
            except (ValueError, TypeError):
                duration = 0
            if len(parts) >= 4:
                album, artist, title = parts[1], parts[2], "".join(parts[3:])
            else:
                album, artist, title = "", parts[1], parts[2]
            results.append({
                "title": title,
                "artist": artist,
                "album": album,
                "id": f"ax-result:{item.get('index', len(results))}",
                "duration": duration,
            })
        return results

    def play_song(self, song_id: str) -> tuple:
        # Result-row playback is intentionally handled by Computer Use in the
        # skill. KuGou reports successful AXPress calls that do not play.
        return False, song_id

    def control(self, mr_cmd: int) -> bool:
        action = {
            MR_CMD_NEXT: "next",
            MR_CMD_PREV: "prev",
            MR_CMD_TOGGLE: "toggle",
            MR_CMD_PLAY: "play",
            MR_CMD_PAUSE: "pause",
        }.get(mr_cmd)
        return bool(action and _kugou_ax("control", action).get("ok"))

    def status(self) -> Optional[dict]:
        data = _kugou_ax("status")
        if not data.get("ok"):
            return None
        return {
            "title": data.get("title", ""),
            "artist": data.get("artist", ""),
            "album": data.get("album", ""),
            "playing": data.get("playing", False),
            "source": "kugou_accessibility",
        }


# ── Spotify ───────────────────────────────────────────────────────────────────
_CFG_PATH  = os.path.expanduser("~/.config/music-cli/config.json")

_SP_VERBS = {
    MR_CMD_NEXT:   "next track",
    MR_CMD_PREV:   "previous track",
    MR_CMD_TOGGLE: "playpause",
    MR_CMD_PLAY:   "play",
    MR_CMD_PAUSE:  "pause",
}

# ── 匿名 Token（Web Player API，无需 OAuth）─────────────────────────────────────
_ANON_TOKEN_CACHE = {}

_WEB_PLAYER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# ── Spotify TOTP（从 web-player JS 逆向）────────────────────────────────────
_SP_TOTP_SECRETS = [
    {'raw': ',7/*F("rLJ2oxaKL^f+E1xvP@N', 'version': 61},
    {'raw': 'OmE{ZA.J^":0FG\\Uz?[@WW',    'version': 60},
    {'raw': '{iOFn;4}<1PFYKPV?5{%u14]M>/V0hDH', 'version': 59},
]


def _sp_totp_key(raw: str) -> bytes:
    xored = [ord(c) ^ (i % 33 + 9) for i, c in enumerate(raw)]
    return "".join(str(x) for x in xored).encode('utf-8')


def _sp_totp(raw: str, t_ms: int = None) -> int:
    import hmac as _hmac, hashlib as _hashlib, struct as _struct
    if t_ms is None:
        t_ms = int(time.time() * 1000)
    key = _sp_totp_key(raw)
    msg = _struct.pack('>Q', t_ms // 30000)
    h = _hmac.new(key, msg, _hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = _struct.unpack('>I', h[offset:offset+4])[0] & 0x7FFFFFFF
    return code % 1_000_000


def _fetch_client_token(client_id: str = "d8a5ed958d274c2e8ee717e6a4b0971d") -> dict:
    device_id = str(uuid.uuid4())
    body = json.dumps({
        "client_data": {
            "client_version": "1.2.92.139.gabc3400e",
            "client_id": client_id,
            "js_sdk_data": {
                "device_brand": "Apple",
                "device_model": "unknown",
                "os": "macos",
                "os_version": platform.mac_ver()[0] or "15.0",
                "device_id": device_id,
                "device_type": "computer",
            },
        },
    }).encode()
    req = urllib.request.Request(
        "https://clienttoken.spotify.com/v1/clienttoken",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://open.spotify.com",
            "User-Agent": _WEB_PLAYER_UA,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _fetch_anon_token() -> dict:
    sp_t = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    for entry in _SP_TOTP_SECRETS:
        tv = _sp_totp(entry['raw'], now_ms)
        ver = entry['version']
        url = (
            "https://open.spotify.com/api/token"
            f"?reason=init&productType=web-player"
            f"&totp={tv}&totpServer={tv}&totpVer={ver}"
        )
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Cookie": f"sp_t={sp_t}; sp_new=1",
            "User-Agent": _WEB_PLAYER_UA,
            "Referer": "https://open.spotify.com/",
            "Origin": "https://open.spotify.com",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            if "accessToken" in data:
                return data
        except Exception:
            continue
    raise RuntimeError("所有 TOTP 版本均失败")


_TOKEN_CACHE_FILE = os.path.join(os.path.dirname(_CFG_PATH), "token_cache.json")


def _load_token_cache() -> dict:
    try:
        with open(_TOKEN_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_token_cache(data: dict):
    try:
        os.makedirs(os.path.dirname(_TOKEN_CACHE_FILE), exist_ok=True)
        with open(_TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _refresh_anon_tokens() -> tuple:
    client_id = "d8a5ed958d274c2e8ee717e6a4b0971d"
    now = time.time()

    data = _fetch_anon_token()
    access_token = data.get("accessToken", "")
    expires_ms = data.get("accessTokenExpirationTimestampMs", 0)
    expires_at = expires_ms / 1000.0 if expires_ms else now + 3500
    client_id = data.get("clientId", client_id)

    ct_data = _fetch_client_token(client_id)
    client_token = (
        ct_data.get("granted_token", {}).get("token", "")
        or ct_data.get("response", {}).get("granted_token", {}).get("token", "")
    )

    if not access_token or not client_token:
        raise RuntimeError("无法获取 Spotify 匿名 token，请检查网络连接")

    cache = {"access_token": access_token, "client_token": client_token, "expires_at": expires_at - 300}
    _ANON_TOKEN_CACHE.update(cache)
    _save_token_cache(cache)
    return access_token, client_token


def _ensure_anon_tokens() -> tuple:
    """返回 (access_token, client_token)，内存 → 文件 → 网络 三级缓存。"""
    now = time.time()

    if _ANON_TOKEN_CACHE and _ANON_TOKEN_CACHE.get("expires_at", 0) > now + 300:
        return _ANON_TOKEN_CACHE["access_token"], _ANON_TOKEN_CACHE["client_token"]

    fc = _load_token_cache()
    if fc and fc.get("expires_at", 0) > now + 300:
        _ANON_TOKEN_CACHE.update(fc)
        return fc["access_token"], fc["client_token"]

    return _refresh_anon_tokens()


def _parse_search_results(data: dict) -> list:
    sv = data.get("data", {}).get("searchV2", {})
    results = []

    top_items = sv.get("topResults", {}).get("itemsV2", [])
    for wrapper in top_items:
        item = (
            wrapper.get("item", {}).get("data", {})
            or wrapper.get("data", {})
        )
        if item.get("__typename") != "Track":
            continue
        track = _extract_track(item)
        if track:
            results.append(track)

    track_items = sv.get("tracksV2", {}).get("items", [])
    for wrapper in track_items:
        item = (
            wrapper.get("item", {}).get("data", {})
            or wrapper.get("data", {})
        )
        track = _extract_track(item)
        if track:
            results.append(track)

    return results


def _extract_track(item: dict) -> Optional[dict]:
    uri = item.get("uri", "")
    if not uri or not item.get("name"):
        return None
    artists = [a.get("profile", {}).get("name", "") for a in item.get("artists", {}).get("items", [])]
    artist = artists[0] if artists else ""
    album = item.get("albumOfTrack", {}) or {}
    album_name = album.get("name", "")
    duration_ms = int(
        (item.get("duration") or item.get("trackDuration") or {}).get("totalMilliseconds", 0)
    )
    return {
        "title":    item.get("name", ""),
        "artist":   artist,
        "album":    album_name,
        "id":       uri,
        "duration": duration_ms // 1000,
    }


class SpotifyProvider(MusicProvider):
    name = "spotify"
    proc_name = "Spotify"

    def __init__(self):
        self._focus_app = "iTerm2"

    def search(self, keyword: str, page_size: int = 8, page: int = 1) -> list:
        results = self._search_partner(keyword, page_size, page)
        if results:
            return results
        return self._search_oauth(keyword, page_size)

    def _search_partner(self, keyword: str, page_size: int, page: int = 1) -> list:
        try:
            access_token, client_token = _ensure_anon_tokens()
        except Exception:
            return []

        offset = (page - 1) * page_size
        body = json.dumps({
            "variables": {
                "searchTerm": keyword,
                "offset": offset,
                "limit": page_size,
                "numberOfTopResults": 5,
                "includeAudiobooks": True,
                "includePreReleases": False,
                "includeAlbumPreReleases": False,
                "includeAuthors": False,
                "includeEpisodeContentRatingsV2": False,
            },
            "operationName": "searchTracks",
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": (
                        "59ee4a659c32e9ad894a71308207594a65ba67bb"
                        "6b632b183abe97303a51fa55"
                    ),
                },
            },
        }).encode()

        def _do_request(at, ct):
            r = urllib.request.Request(
                "https://api-partner.spotify.com/pathfinder/v2/query",
                data=body,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "zh-CN",
                    "App-Platform": "WebPlayer",
                    "Authorization": f"Bearer {at}",
                    "Client-Token": ct,
                    "Content-Type": "application/json;charset=UTF-8",
                    "Origin": "https://open.spotify.com",
                    "Referer": "https://open.spotify.com/",
                    "Spotify-App-Version": "1.2.92.139.gabc3400e",
                    "User-Agent": _WEB_PLAYER_UA,
                },
            )
            with urllib.request.urlopen(r, timeout=10) as resp:
                return json.loads(resp.read().decode())

        try:
            return _parse_search_results(_do_request(access_token, client_token))
        except urllib.request.HTTPError as e:
            if e.code in (401, 403):
                try:
                    access_token, client_token = _refresh_anon_tokens()
                    return _parse_search_results(_do_request(access_token, client_token))
                except Exception:
                    return []
            return []
        except Exception:
            return []

    def _search_oauth(self, keyword: str, page_size: int) -> list:
        """通过官方 Web API 搜索（需要 OAuth 授权，作为兜底）。"""
        try:
            cfg = json.load(open(_CFG_PATH, encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        token = cfg.get("spotify", {}).get("token", {}).get("access_token", "")
        if not token:
            return []
        params = urllib.parse.urlencode({
            "q": keyword, "type": "track", "limit": page_size,
        })
        req = urllib.request.Request(
            f"https://api.spotify.com/v1/search?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            return [
                {
                    "title":    it.get("name", ""),
                    "artist":   it["artists"][0]["name"] if it.get("artists") else "",
                    "album":    it.get("album", {}).get("name", ""),
                    "id":       it.get("uri", ""),
                    "duration": it.get("duration_ms", 0) // 1000,
                }
                for it in data.get("tracks", {}).get("items", [])
            ]
        except Exception:
            return []

    def capture_focus(self):
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events"\n'
             f'  set p to first process where (frontmost is true) and (name is not "{self.proc_name}")\n'
             '  return name of p\n'
             'end tell'],
            capture_output=True, text=True,
        )
        app = r.stdout.strip()
        if r.returncode == 0 and app and app != self.proc_name:
            self._focus_app = app

    def _ensure_running(self) -> bool:
        r = subprocess.run(["pgrep", "-x", self.proc_name], capture_output=True)
        if r.returncode == 0:
            return True
        subprocess.run(["open", "-a", self.proc_name], capture_output=True)
        for _ in range(20):
            time.sleep(0.5)
            r = subprocess.run(["pgrep", "-x", self.proc_name], capture_output=True)
            if r.returncode == 0:
                time.sleep(2)
                return True
        return False

    def play_song(self, song_id: str) -> tuple:
        if not song_id:
            return False, ""
        if not self._ensure_running():
            return False, ""
        script = (
            f'tell application "{self.proc_name}" to play track "{song_id}"\n'
            f'tell application "System Events" to set visible of process "{self.proc_name}" to false\n'
            f'tell application "{self._focus_app}" to activate\n'
        )
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        return r.returncode == 0, song_id

    def control(self, mr_cmd: int) -> bool:
        verb = _SP_VERBS.get(mr_cmd)
        if not verb:
            return False
        return subprocess.run(
            ["osascript", "-e", f'tell application "{self.proc_name}" to {verb}'],
            capture_output=True,
        ).returncode == 0

    def status(self) -> Optional[dict]:
        r = subprocess.run([
            "osascript",
            "-e", f'tell application "{self.proc_name}"',
            "-e", "if it is running then",
            "-e", 'return (player state as string) & "|" & (name of current track) & "|" & (artist of current track) & "|" & (album of current track)',
            "-e", "end if",
            "-e", "end tell",
        ], capture_output=True, text=True)
        out = r.stdout.strip()
        if not out:
            return None
        parts = out.split("|")
        if len(parts) < 4 or not parts[1].strip():
            return None
        return {
            "title":   parts[1].strip(),
            "artist":  parts[2].strip(),
            "album":   parts[3].strip(),
            "playing": parts[0].strip() == "playing",
            "source":  "spotify_applescript",
        }

    def player_position(self) -> tuple:
        """返回 (position_seconds, duration_seconds)，失败返回 (0, 0)。"""
        r = subprocess.run([
            "osascript",
            "-e", f'tell application "{self.proc_name}"',
            "-e", "if it is running then",
            "-e", "return (player position as string) & \"|\" & (duration of current track as string)",
            "-e", "end if",
            "-e", "end tell",
        ], capture_output=True, text=True)
        out = r.stdout.strip()
        if not out:
            return 0, 0
        try:
            pos_s, dur_ms = out.split("|")
            return float(pos_s.strip()), float(dur_ms.strip()) / 1000
        except (ValueError, IndexError):
            return 0, 0


# ── 注册表 ────────────────────────────────────────────────────────────────────
def get_provider(name: str) -> MusicProvider:
    return SpotifyProvider() if (name or "").lower() == "spotify" else KuGouProvider()
