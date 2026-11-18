---
name: music-control
description: "Autonomously control music playback on macOS, using KuGou by default and Spotify only when explicitly requested. PROACTIVELY plays music matching the conversation vibe — focused, relaxed, hyped or debug. Responds to requests like play Jay Chou or play something relaxing. Always prefer taking action over asking."
---

# music-control

Controls KuGou Music or Spotify on macOS via CLI and background app automation.
Use it to enhance the user's work session with music — proactively and
responsively.

KuGou search/play uses the `computer-use` skill plus a native Accessibility
helper. It operates the existing KuGou window without raising it.

## Setup

Requirements: macOS, Python 3.10+, KuGou Music or Spotify desktop app installed.

No Spotify API key or OAuth setup needed — search uses the anonymous Web Player API.

## CLI

```bash
MUSIC="python3 ~/.claude/skills/music-control/scripts/music_cli.py"
$MUSIC [--provider kugou|spotify] <command>
```

| Command | Description |
|---------|-------------|
| `status` | Current track info (JSON) |
| `play` | Toggle play/pause |
| `next` | Skip to next |
| `prev` | Previous track |
| `play-by <title>...` | Spotify search/play; multi-song with `\|\|\|` |
| `search <query> [--page N]` | Spotify search; KuGou reads its current result page |
| `prepare-search <query>` | Internal: prepare a background KuGou search |
| `recommend <mood>` | Pick by mood and play |
| `queue-show` | Display current queue |
| `queue-add <title>...` | Add songs to queue; multi-song with `\|\|\|` |
| `queue-play [index]` | Start playing from queue |
| `queue-next` | Skip to next in queue |
| `queue-clear` | Clear the queue |

`--provider` defaults to `kugou`. Use Spotify only when the user explicitly
requests it or sets `MUSIC_PROVIDER=spotify`.

## KuGou Background Workflow

For KuGou search/play, use the `computer-use` skill. Do not use KuGou HTTP
search APIs, guessed URL schemes, AppleScript menu automation, or private
MediaRemote.

1. If KuGou is not running, open `/Applications/KugouMusic.app` normally and
   wait for its main window to finish loading. A visible launch is expected.
   Once running, call `get_app_state` for bundle ID `com.kugou.mac.Music`;
   subsequent operations must not raise its window.
2. Run:

   ```bash
   python3 scripts/music_cli.py --provider kugou prepare-search "<title> <artist>"
   ```

   The native helper writes Unicode into the background WebView and opens
   KuGou's own autocomplete list.
3. Call `get_app_state` once to let autocomplete settle, then call `press_key`
   with `Return` for `com.kugou.mac.Music`.
4. Call `get_app_state` again. Retry while the result area says `加载中`.
   Prefer an exact title/artist match; avoid Live, DJ, instrumental, or excerpt
   versions unless requested.
5. Click the selected row once to expose its inline controls, then click the
   triangle play control shown on that row in the current screenshot. Do not
   click the adjacent heart or download controls.
6. Verify the bottom player title changed. If a download dialog appears, close
   it and retry the row's triangle play control.

For play/pause, next, previous, and status, use the CLI directly. These commands
call the native Accessibility helper and do not raise KuGou:

```bash
python3 scripts/music_cli.py --provider kugou status
python3 scripts/music_cli.py --provider kugou play
python3 scripts/music_cli.py --provider kugou next
python3 scripts/music_cli.py --provider kugou prev
```

## Mood Keywords

| Mood | When to use |
|------|-------------|
| `focused` | Deep work, coding in flow, reading |
| `relaxed` | Casual chat, breaks, light tasks |
| `hyped` | Feature shipped, bug fixed, wins |
| `sad` | User venting, stuck on a problem |
| `debug` | Frustrated, chasing a tricky bug |

## Proactive Trigger Rules

**Always act without being asked when you detect:**

| Signal in conversation | Action |
|------------------------|--------|
| User starts a new coding session | `recommend focused` → play top result |
| User says they fixed a bug / shipped | `recommend hyped` → play top result |
| User is frustrated / stuck | `recommend debug` |
| User mentions needing to relax | `recommend relaxed` |
| User mentions a specific song or artist | Use the KuGou background workflow |
| Long silence / idle in conversation | `recommend relaxed` |
| User asks "play something" with no detail | Infer from recent conversation mood |

## Queue & Multi-Song

Multi-song separator: `|||`

```bash
$MUSIC play-by 逆战 ||| 天下 ||| 这就是爱
$MUSIC queue-add 逆战 ||| 天下
$MUSIC queue-play
$MUSIC queue-next
$MUSIC search 周杰伦 --page 2
```

## Response Format

```json
{"ok": true, "title": "晴天", "artist": "周杰伦", "playing": true}
{"ok": true, "results": [{"title":"...", "artist":"...", "id":"..."}]}
{"ok": true, "action": "next"}
{"ok": true, "action": "queue_next", "current": 1, "has_next": true}
{"ok": false, "error": "no_player", "message": "..."}
```

## Notes

- Spotify: uses anonymous Web Player API for search — no login or API key needed
- KuGou: requires KuGou Music macOS app installed and Accessibility permission
- KuGou is opened normally when absent; only subsequent operations are silent
- KuGou search changes its internal page but does not raise the window
- If `status` returns `no_player`, the app may still be loading — wait 3s and retry
- Queue is persisted to `~/.config/music-cli/queue.json`
