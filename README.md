# music-control — Claude Code Skill

A Claude Code skill that gives your AI agent the ability to control music
playback on macOS — proactively and responsively.

**The agent will play music without being asked.** It reads the vibe of your
conversation and picks a matching track automatically.

## Features

- Proactive music: agent detects coding mood and plays matching music
- Explicit requests: "play Jay Chou", "something relaxing", etc.
- Supports **KuGou Music** and **Spotify** desktop apps
- Search with queue, mood recommendations, playback control
- No Premium required for Spotify (search via KuGou API, playback via AppleScript)

## Install

```bash
git clone https://github.com/ni-shiliu/music-cli
cp -r music-cli/skill ~/.claude/skills/music-control
```

Claude Code will automatically load the skill on next startup.

## Spotify Setup (optional)

1. Create an app at [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   - Add Redirect URI: `http://127.0.0.1:8888/callback`
2. Add your Client ID:
   ```bash
   mkdir -p ~/.config/music-cli
   echo '{"spotify":{"client_id":"YOUR_CLIENT_ID"}}' > ~/.config/music-cli/config.json
   ```
3. Authorize (one-time):
   ```bash
   python3 ~/.claude/skills/music-control/scripts/music_cli.py --provider spotify auth
   ```

## Requirements

- macOS
- Python 3.10+
- KuGou Music or Spotify desktop app

## License

MIT
