# Jarvis: architecture

## The pieces

```
 User's phone (Telegram)
        │  text / voice
        ▼
 ┌─────────────────────────────── this PC ────────────────────────────────┐
 │ Task Scheduler "Jarvis Watchdog" (logon + every 5 min, mutex = 1 copy)  │
 │   └ watchdog.ps1 ── keeps alive ──┬─ jarvisd (python -m jarvis.daemon) │
 │                                   └─ cmd "JarvisChannels"               │
 │                                        └ claude --channels telegram     │
 │                                             ├ bun server.ts (Telegram)  │
 │                                             ├ MCP jarvis  ─┐ HTTP+key   │
 │                                             ├ MCP google (workspace-mcp)│
 │                                             └ hook guard.py (every tool)│
 │ jarvisd 127.0.0.1:8765 ◄───────────────────────────────────┘           │
 │   speech-to-text, reminders/scheduled sends, briefs, nudges (plain code)│
 └─────────────────────────────────────────────────────────────────────────┘
```

| Where | What |
|---|---|
| `CLAUDE.md` | Jarvis's rules (persona, tasks protocol, security, escalation) |
| `.claude/agents/` | runtime: `email-reader`, `web-reader`, `email-labeler` (Haiku, no send tools), `opus-advisor`, `sonnet-escalation`; build-time: `dev-*` |
| `.mcp.json` (from `.mcp.example.json`) | MCP servers `jarvis` (ours) and `google` (taylorwilsdon/google_workspace_mcp, send tool disabled) |
| `jarvis/` | `daemon.py` service, `mcp_server.py` tools, `guard.py` hook, `hedate.py` Hebrew dates, `proactive.py` briefs/nudges/sends, `tg.py` outbound Telegram, `gapi.py` Google client |
| `watchdog.ps1` | always-on supervisor |
| `%USERPROFILE%\.jarvis\` | everything secret or stateful, outside the project: Jarvis's own Claude config dir (`claude\`: login, settings + permissions, Telegram token/allowlist), Google token, daemon key, jobs, logs, speech models |
| `memory/` | editable memory: people, preferences, ongoing, outbox (git-ignored; empty templates in `memory.example/`) |
| `permissions/learned.json` | narrow rules the user approved (git-ignored; see `permissions/learned.example.json`) |

## Decisions log

| # | Decision | Why | Rejected |
|---|---|---|---|
| D1 | Jarvis runs with its own `CLAUDE_CONFIG_DIR` (`~/.jarvis/claude`) | The Telegram plugin at user scope ran in *every* Claude session and stole the bot (one poller per token). Isolation also keeps the user's other plugins/connectors (a Gmail connector with send, synced plugins) out of Jarvis (R12). | Plugin in his main config; project-scoped enable |
| D2 | Login via `/login` (Max OAuth) in that dir, not `claude setup-token` | setup-token disables Chrome integration (needed for R6/R8). Both are Max, not API. | setup-token |
| D3 | Watchdog = Task Scheduler + PowerShell, not NSSM | The Channels session is an interactive TUI; it needs the user's logged-on desktop session. A service (NSSM, session 0) can't host it or Chrome. Task Scheduler is built in. | NSSM service |
| D4 | Watchdog identifies Jarvis only by its own `cmd` wrapper title and the Telegram poller path/bot.pid | The Claude desktop app is also `claude.exe`; a fakechat test session also runs bun. Killing by name would hit them. | Kill all bun.exe |
| D5 | Health = wrapper + claude + bun `server.ts` all alive, two passes in a row | `pending_update_count` stays 0 while the poller consumes updates, so it can't detect a hung claude. | Telegram pending-count check |
| D6 | Proactive messages are sent by jarvisd in plain code via the Bot API (sendMessage only) | R4: no tokens for "is anything due?". Only getUpdates conflicts with the plugin. Custom channels need `--dangerously-load-development-channels` + an interactive dialog, impossible for unattended restarts. | A second custom channel; `claude -p` per brief |
| D7 | Briefs/check-ins are templates, no model at all | Cheapest possible (0 tokens); data is structured anyway. | Haiku-written briefs |
| D8 | Untrusted mail/web only via `email-reader` / `web-reader` subagents (Haiku); `guard.py` denies those tools to everyone else using the hook's `agent_type` | R12. Red-team showed the main session could be steered into exfiltration via WebFetch/drafts. | Advice-only CLAUDE.md rule |
| D9 | `guard.py` PreToolUse hook enforces hard rules (no task delete, labels only add, calendar events with no attendees and send_updates=none, file/attachment paths, Opus cap, learned narrow rules) and fails closed | Claude Code permission rules can't match tool arguments; narrow rules ("this one recipient") need argument matching. | Settings-only |
| D10 | Scheduled email = Gmail draft + `schedule_email` MCP tool whose call needs approval (permission relay to Telegram); jarvisd sends with `drafts.send` and re-checks to/subject | Gmail API has no scheduled send. The permission prompt is the single "approve once" with the exact text and time. | Model sends at the time |
| D11 | Speech: whisper.cpp (ivrit-ai turbo ggml, BLAS) with encoder window shrunk to the clip (`-ac`) for clips ≤ 25 s; faster-whisper (ivrit-ai turbo ct2 int8) kept loaded for longer clips | No NVIDIA GPU. Measured on a 4-core laptop CPU: 30 s clip 27-44 s either way; 3.7 s clip 19 s → 9.5 s with `-ac`. | whisper.cpp only; paid API |
| D12 | TTS: edge-tts, fallback to the built-in Windows Hebrew voice (Asaf) | edge-tts is currently 403-blocked from this region (rany2/edge-tts#490). Voice notes are sent with Bot API `sendVoice` (OGG/Opus) so they're playable, not documents. | Plugin reply with a file (arrives as a document) |
| D13 | Google app stays in OAuth "Testing"; weekly re-login reminder + desktop shortcut | Publishing with Gmail scopes requires a verified homepage/privacy policy. Testing tokens expire after 7 days. | Publishing + verification |
| D14 | Main model Sonnet (pending R10 evaluation) | | |
| D15 | Claude Code updated 2.1.170 → 2.1.296, auto-update off for Jarvis | 2.1.296 kills stdio MCP trees on Windows (helps #42269); updates are done by hand to avoid surprise restarts. | |
