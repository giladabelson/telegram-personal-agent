# Register the limited ("external") Jarvis tools in all Claude Code sessions (user scope).
# Exposes only: tasks, reminders, tell_jarvis. Undo with: claude mcp remove --scope user jarvis-ext
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
# name goes before -e: -e takes several values and would swallow it
claude mcp add --scope user --transport stdio jarvis-ext `
    -e JARVIS_MCP_PROFILE=external -e "PYTHONPATH=$root" `
    '--' "$root\.venv\Scripts\python.exe" -m jarvis.mcp_server  # quoted: bare -- is eaten by PowerShell
claude mcp get jarvis-ext
