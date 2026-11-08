# Jarvis watchdog. Started by Task Scheduler at logon and every 5 minutes; a named mutex keeps one copy.
# Keeps two things alive: jarvisd (python) and the Claude Code Channels session (claude + its bun Telegram poller).
# Designed around anthropics/claude-code#42269: claude.exe dies after 1-2h and leaves orphan bun.exe pollers.
# Only ever touches processes it can prove are Jarvis's: the "JarvisChannels" cmd wrapper tree and bun pollers
# under the Jarvis config dir. Never the Claude desktop app or the user's other Claude Code sessions.
param([switch]$Once)
$ErrorActionPreference = 'Continue'
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$JHome = Join-Path $env:USERPROFILE '.jarvis'
$ClaudeDir = Join-Path $JHome 'claude'
$TgDir = Join-Path $ClaudeDir 'channels\telegram'
$Log = Join-Path $JHome 'logs\watchdog.log'
$StateFile = Join-Path $JHome 'watchdog.state.json'
$ChatId = $env:JARVIS_CHAT_ID
if (-not $ChatId) { throw 'JARVIS_CHAT_ID is not set' }
$Marker = 'title JarvisChannels'
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
Set-Location $Project   # Task Scheduler starts us in System32; `python -m jarvis.bus` needs the project dir

function Log($m) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 1MB) { Move-Item $Log "$Log.old" -Force }
    "$(Get-Date -Format s) $m" | Add-Content -Path $Log -Encoding utf8
}

$mutex = New-Object System.Threading.Mutex($false, 'Global\JarvisWatchdog')
try { $got = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $got = $true }
if (-not $got) { exit 0 }   # another watchdog is running

# @() matters: on a one-line file -match would return $true instead of the line
function Token { (@(Get-Content (Join-Path $TgDir '.env')) -match '^TELEGRAM_BOT_TOKEN=' -replace '^TELEGRAM_BOT_TOKEN=', '').Trim() }
$PendingTg = Join-Path $JHome 'pending_tg.txt'
function Tg($text, [switch]$NoQueue) {
    try { Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$(Token)/sendMessage" -ContentType 'application/json; charset=utf-8' `
            -Body ([Text.Encoding]::UTF8.GetBytes((@{ chat_id = $ChatId; text = $text } | ConvertTo-Json))) -TimeoutSec 15 | Out-Null; $true }
    catch {
        Log 'telegram send failed (queued)'
        if (-not $NoQueue) { "$(Get-Date -Format 'HH:mm') $text" -replace "`r?`n", ' / ' | Add-Content $PendingTg -Encoding utf8 }
        $false
    }
}
function FlushTg {
    # messages that failed during an outage go out as one message when the internet is back
    if (-not (Test-Path $PendingTg)) { return }
    $lines = @(Get-Content $PendingTg -Encoding utf8 | Select-Object -Last 5)
    if (Tg ("הודעות שלא הצלחתי לשלוח בזמן הנתק:`n" + ($lines -join "`n")) -NoQueue) { Remove-Item $PendingTg }
}
function NetUp { try { [System.Net.Dns]::GetHostAddresses('api.anthropic.com') | Out-Null; $true } catch { $false } }
function PendingUpdates {
    # Updates Telegram is holding because nobody fetched them. Read-only; does not compete with the poller.
    try { (Invoke-RestMethod "https://api.telegram.org/bot$(Token)/getWebhookInfo" -TimeoutSec 15).result.pending_update_count } catch { -1 }
}

function Procs { Get-CimInstance Win32_Process }
function Tree($all, $rootId) {
    # all descendants of a pid
    $out = @(); $q = @([int]$rootId)
    while ($q.Count) { $id = $q[0]; $q = @($q | Select-Object -Skip 1)
        foreach ($c in $all | Where-Object { [int]$_.ParentProcessId -eq $id }) { $out += $c; $q += [int]$c.ProcessId } }
    $out
}
function Wrapper($all) { $all | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match [regex]::Escape($Marker) } }
function SessionHealthy($all) {
    # wrapper alive, a claude.exe under it, and a bun server.ts poller under that
    foreach ($w in Wrapper $all) {
        $t = Tree $all $w.ProcessId
        if (($t | Where-Object Name -eq 'claude.exe') -and ($t | Where-Object { $_.Name -eq 'bun.exe' -and $_.CommandLine -match 'server\.ts' })) { return $true }
    }
    $false
}
function StopSession($all) {
    foreach ($w in Wrapper $all) { Log "stopping session tree pid=$($w.ProcessId)"; taskkill /T /F /PID $w.ProcessId 2>&1 | Out-Null }
}
function BusUp {
    # cheap TCP probe of the fakechat bus (no model turn): is anything listening on 127.0.0.1:8787?
    $c = New-Object Net.Sockets.TcpClient
    try { $t = $c.ConnectAsync('127.0.0.1', 8787); ($t.Wait(1500) -and $c.Connected) } catch { $false } finally { $c.Close() }
}
function ConfigStamp {
    # newest mtime of the files a running session reads only at start
    $files = @(Join-Path $Project 'CLAUDE.md'; Join-Path $Project '.mcp.json') +
             @(Get-ChildItem (Join-Path $Project '.claude') -Filter 'settings*.json' -ErrorAction SilentlyContinue | ForEach-Object FullName)
    ($files | Where-Object { Test-Path $_ } | ForEach-Object { (Get-Item $_).LastWriteTime } | Sort-Object -Descending | Select-Object -First 1)
}
function TranscriptStatus {
    # Reads the newest session transcript (python -m jarvis.bus transcript-status). queue-operation lines (a message that
    # was merely enqueued, e.g. behind a stuck permission prompt) do NOT count as activity; only real user/assistant turns.
    # Returns hung / idle / unprocessed (count of enqueued messages no turn picked up) / notice (Hebrew text about lost Telegram messages).
    # only transcripts written since this session started: a fresh session has none yet, and the previous (maybe stuck) one must not count
    $since = ([DateTimeOffset]$state.lastStart).ToUnixTimeSeconds() - 5
    try { $j = & (Join-Path $Project '.venv\Scripts\python.exe') -m jarvis.bus transcript-status $since 2>$null; if ($j) { $j | ConvertFrom-Json } else { $null } } catch { $null }
}
function SendLostNotice {
    # one Telegram message about Telegram messages the old session never handled; kept on disk until it is actually sent
    $f = Join-Path $JHome 'lost_notice.txt'
    if (-not (Test-Path $f)) { return }
    $t = (Get-Content $f -Raw -Encoding utf8).Trim()
    if ($t -and (Tg $t -NoQueue)) { Log 'sent lost-message notice'; Remove-Item $f -ErrorAction SilentlyContinue }
    elseif (-not $t) { Remove-Item $f -ErrorAction SilentlyContinue }
}
function Daemon($all) { $all | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'jarvis\.daemon' } }

function KillOrphanBun($all) {
    $byId = @{}; $all | ForEach-Object { $byId[[int]$_.ProcessId] = $_ }
    $keep = @{}; foreach ($w in Wrapper $all) { Tree $all $w.ProcessId | ForEach-Object { $keep[[int]$_.ProcessId] = $true } }
    foreach ($b in $all | Where-Object { $_.Name -eq 'bun.exe' }) {
        $par = $byId[[int]$b.ParentProcessId]
        # only Jarvis's TELEGRAM poller: the fakechat test session and other bun apps are left alone.
        # A server.ts whose parent died is identified through the plugin's own bot.pid file.
        $tgRun = '\.jarvis[\\/]claude[\\/]plugins[\\/].*telegram'
        $botPid = try { [int](Get-Content (Join-Path $TgDir 'bot.pid') -ErrorAction Stop) } catch { 0 }
        $mine = $b.CommandLine -match $tgRun -or
                ($b.CommandLine -match 'server\.ts' -and (($par -and $par.CommandLine -match $tgRun) -or [int]$b.ProcessId -eq $botPid))
        if ($mine -and -not $keep[[int]$b.ProcessId]) {
            Log "killing orphan bun pid=$($b.ProcessId)"; Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

# R1: usage must come from the Max login only. Drop anything inherited from a parent Claude session
# (API keys, base-URL proxy, host auth, child-session markers) so Jarvis is a clean standalone session.
Get-ChildItem env: | Where-Object { $_.Name -match '^(ANTHROPIC_|CLAUDE|MCP_)' } |
    ForEach-Object { Remove-Item "Env:$($_.Name)" -ErrorAction SilentlyContinue }
$env:CLAUDE_CONFIG_DIR = $ClaudeDir
$env:DISABLE_AUTOUPDATER = '1'
$env:ENABLE_CLAUDEAI_MCP_SERVERS = 'false'
$env:CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION = 'false'   # nobody sees terminal suggestions; they re-read the whole context each turn

function StartDaemon {
    Log 'starting jarvisd'
    Start-Process -FilePath (Join-Path $Project '.venv\Scripts\python.exe') -ArgumentList '-m', 'jarvis.daemon' `
        -WorkingDirectory $Project -WindowStyle Hidden
}
function StartClaude {
    Log 'starting claude channels session'
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "$Marker && claude --channels plugin:telegram@claude-plugins-official plugin:fakechat@claude-plugins-official" `
        -WorkingDirectory $Project -WindowStyle Minimized
}

$state = @{ fails = 0; lastStart = (Get-Date).AddYears(-1); daemonStarts = @() }
if (Test-Path $StateFile) {
    try { $s = Get-Content $StateFile -Raw | ConvertFrom-Json; $state.fails = [int]$s.fails; $state.lastStart = [datetime]$s.lastStart } catch {}
}
$firstPass = $true; $hungPasses = 0; $unhealthy = 0; $lastPing = Get-Date; $pingFails = 0; $tgStuck = 0
$lastLoop = Get-Date; $wakeAt = $null; $busDownSince = $null; $cfgRestartAt = (Get-Date).AddYears(-1)
# Called by the Task Scheduler "resume from sleep" trigger as well: a fresh start counts as a possible wake.

while ($true) {
    # wake detection: the loop sleeps 30 s, so a much longer gap means the PC was asleep (Modern Standby).
    if (((Get-Date) - $lastLoop).TotalSeconds -gt 90) {
        $wakeAt = Get-Date; $state.fails = 0; $unhealthy = 0
        Log "wake detected (gap $([int]((Get-Date) - $lastLoop).TotalSeconds)s)"
        Start-Sleep -Seconds 15   # let Windows bring the network and processes back before judging anything
    }
    $lastLoop = Get-Date
    $afterWake = $wakeAt -and (((Get-Date) - $wakeAt).TotalMinutes -lt 10)
    $all = Procs

    # daemon, with a simple crash-loop brake: at most 5 starts per 10 minutes
    if (-not (Daemon $all)) {
        $state.daemonStarts = @($state.daemonStarts | Where-Object { ((Get-Date) - $_).TotalMinutes -lt 10 })
        if ($state.daemonStarts.Count -lt 5) { StartDaemon; $state.daemonStarts += Get-Date }
        elseif ($state.daemonStarts.Count -eq 5) { Log 'jarvisd crash loop'; Tg 'יש לי תקלה בשירות התזכורות. אני ממשיך לנסות, אבל אולי צריך לבדוק במחשב.'; $state.daemonStarts += Get-Date }
    }

    KillOrphanBun $all
    $net = NetUp
    if ($net) { FlushTg; if (SessionHealthy $all) { SendLostNotice } }
    if (SessionHealthy $all) {
        $unhealthy = 0
        # Telegram poller stuck (seen 15.10 after a sleep: bun alive, 6 messages waiting at Telegram for hours).
        # The long poll fetches new messages within seconds, so anything still waiting after ~90 s means nobody polls.
        if ($net) {
            $pend = PendingUpdates
            if ($pend -gt 0) { $tgStuck++ } elseif ($pend -eq 0) { $tgStuck = 0 }
            if ($tgStuck -ge 3) {
                Log "telegram poller stuck ($pend messages waiting): restarting"
                New-Item -ItemType File -Force (Join-Path $JHome 'planned_restart') | Out-Null   # messages arrive after it; no crash notice
                $unhealthy = 2; $tgStuck = 0
            }
        }
        # end-to-end ping through the model every 4 h by day (catches a hung session). Each ping is a full model
        # turn, so not hourly and not at night; and never while the internet is down (that isn't a hang).
        $h = (Get-Date).Hour
        $settled = (((Get-Date) - $state.lastStart).TotalMinutes -ge 5) -and -not ($wakeAt -and (((Get-Date) - $wakeAt).TotalMinutes -lt 3))
        if ($settled) {
            # bus (port 8787) down while the processes look fine = the fakechat plugin died; the session can't receive
            # jobs/work sessions. A model ping can't tell us that, so check the port every pass and restart after 10 min.
            if (BusUp) { if ($busDownSince) { Log 'bus back'; $busDownSince = $null } }
            else {
                if (-not $busDownSince) { $busDownSince = Get-Date; Log 'bus (8787) not answering while session processes are alive' }
                elseif (((Get-Date) - $busDownSince).TotalMinutes -ge 10) { Log 'bus down for 10 min: restarting session'; $busDownSince = $null; $unhealthy = 2 }
            }
        }
        # transcript-based hang check every pass: a message enqueued > 5 min ago that no turn ever picked up (stuck on a
        # permission prompt, etc.). Two passes in a row (~1 min) before acting; the restart below keeps its own back-off.
        $ts = $null
        if ($settled) {
            $ts = TranscriptStatus
            if ($ts -and $ts.hung) {
                $hungPasses++
                if ($hungPasses -eq 1) { Log "transcript shows a hung session: $($ts.reason)" }
                if ($hungPasses -ge 2) { Log 'session hung (queued message never handled): restarting'; $unhealthy = 2; $hungPasses = 0 }
            } else { $hungPasses = 0 }
        }
        if ($net -and $settled -and -not $busDownSince -and $h -ge 7 -and ((Get-Date) - $lastPing).TotalHours -ge 4) {
            $lastPing = Get-Date
            & (Join-Path $Project '.venv\Scripts\python.exe') -m jarvis.bus ping 2>$null
            # exit 0 answered, 1 bus up but no answer (hung model), 2 bus unreachable (not a model problem, handled above)
            if ($LASTEXITCODE -eq 0) { $pingFails = 0 }
            elseif ($LASTEXITCODE -eq 1 -and $ts -and -not $ts.hung -and $null -ne $ts.last_turn_age_min -and $ts.last_turn_age_min -lt 5) { Log 'ping unanswered but the session is busy with a turn (transcript active): not counted'; $lastPing = (Get-Date).AddHours(-3.9) }
            elseif ($LASTEXITCODE -eq 1 -and (NetUp)) { $pingFails++; Log "ping failed: no answer ($pingFails)" }
            else { Log "ping skipped: bus unreachable (exit $LASTEXITCODE)" }
            if ($pingFails -ge 2) { Log 'session hung (2 failed pings): restarting'; $unhealthy = 2; $pingFails = 0 }
            if ($pingFails -eq 1) { $lastPing = (Get-Date).AddHours(-3.75) }   # re-check in 15 min, not 4 h
        }
        # CLAUDE.md / settings edited after this session started: restart, but only when idle (nothing in/out for 2 min,
        # file itself quiet for 1 min), and at most once per 30 min, so a half-finished edit or a live chat isn't cut off.
        $wrap = Wrapper $all | Select-Object -First 1
        $cfg = ConfigStamp
        if ($wrap -and $cfg -and $cfg -gt $wrap.CreationDate -and (((Get-Date) - $cfgRestartAt).TotalMinutes -ge 30)) {
            # idle = no real turn for 2 min AND no enqueued message waiting (those would be lost by a restart). A hung session is handled above.
            $st = TranscriptStatus
            if (((Get-Date) - $cfg).TotalMinutes -ge 1 -and $st -and $st.idle) {
                Log "config changed ($($cfg.ToString('s'))) after session start: restarting while idle"
                New-Item -ItemType File -Force (Join-Path $JHome 'planned_restart') | Out-Null
                $cfgRestartAt = Get-Date; $unhealthy = 2
            }
        }
        # daily fresh start at 04:00: the session's context only grows, and every turn re-reads all of it
        if ($h -eq 4 -and ((Get-Date) - $state.lastStart).TotalHours -ge 12) {
            Log 'daily planned restart'; New-Item -ItemType File -Force (Join-Path $JHome 'planned_restart') | Out-Null; $unhealthy = 2
        }
        if (((Get-Date) - $state.lastStart).TotalMinutes -gt 30) { $state.fails = 0 }
    } else {
        if ($unhealthy -eq 0) {
            $wake = (Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-Power';Id=507} -MaxEvents 1 -ErrorAction SilentlyContinue).TimeCreated
            Log "session missing (wrapper=$([bool](Wrapper $all)) lastStart=$($state.lastStart.ToString('s')) lastWake=$wake)"
        }
        $unhealthy++
        if ($afterWake) { $unhealthy = 2 }   # just woke up and the session is gone: no 1-minute doubt period
    }

    # restart after two unhealthy passes in a row (~1 min), so a session that is still starting isn't killed
    if ($unhealthy -ge 2 -or ($firstPass -and -not (Wrapper $all))) {
        $crashed = -not $firstPass -or (Wrapper $all)
        # before the old session goes away: which Telegram messages did it never handle?
        $pre = TranscriptStatus
        if ($pre -and $pre.notice) { Set-Content (Join-Path $JHome 'lost_notice.txt') $pre.notice -Encoding utf8; Log "unhandled telegram messages at restart: $($pre.unprocessed) queued" }
        StopSession $all
        Start-Sleep -Seconds 3
        KillOrphanBun (Procs)
        $wait = @(10, 30, 60, 120, 300)[[Math]::Min($state.fails, 4)]
        $since = ((Get-Date) - $state.lastStart).TotalSeconds
        if ($since -lt $wait) { Start-Sleep -Seconds ([int]($wait - $since)) }
        StartClaude
        $state.lastStart = Get-Date; $state.fails++
        @{ fails = $state.fails; lastStart = $state.lastStart.ToString('o') } | ConvertTo-Json | Set-Content $StateFile -Encoding ascii
        $ok = $false
        for ($i = 0; $i -lt 18 -and -not $ok; $i++) { Start-Sleep -Seconds 5; $ok = SessionHealthy (Procs) }
        $busDownSince = $null; $hungPasses = 0
        if ($ok) {
            $unhealthy = 0
            SendLostNotice
            $planned = Join-Path $JHome 'planned_restart'   # touched by maintenance, so no false "crash" message
            if ($crashed -and -not (Test-Path $planned)) { Log 'recovered'; Tg 'חזרתי לפעול אחרי תקלה קטנה.' }
            Remove-Item $planned -ErrorAction SilentlyContinue
        } else { Log 'claude failed to start within 90s' }
        if ($state.fails -eq 4) { Tg 'אני נופל שוב ושוב ומנסה לקום. אם זה ממשיך, צריך לבדוק במחשב.' }
    }
    $firstPass = $false
    if ($Once) { break }
    Start-Sleep -Seconds 30
}
