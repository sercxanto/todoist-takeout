# Scheduling

How to run todoist-takeout every day without you. For installation and a quick start, see the [README](../README.md).

The tool needs no terminal and never asks for input, so it runs unattended. The examples run [`backup-and-sqlite`](viewing.md#backup-and-database-in-one-directory), which keeps the backup and the database current; replace it with [`backup`](backup.md) to keep only the backup. `--quiet` keeps the output to warnings and errors, and the [exit status](backup.md#exit-status) tells the scheduler whether the run succeeded.

Keep the token in a file that only you can read, for example `~/.config/todoist-takeout/token` with mode `600`, and set the variable from it.

## cron (Linux, macOS)

Daily at 03:00, with `crontab -e`:

```
0 3 * * * TODOIST_API_TOKEN="$(cat "$HOME/.config/todoist-takeout/token")" "$HOME/.local/bin/todoist-takeout" backup-and-sqlite --quiet "$HOME/Backups/todoist"
```

## launchd (macOS)

Save as `~/Library/LaunchAgents/com.example.todoist-takeout.plist`, replace `/Users/me`, and load it with `launchctl load ~/Library/LaunchAgents/com.example.todoist-takeout.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.todoist-takeout</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>TODOIST_API_TOKEN="$(cat /Users/me/.config/todoist-takeout/token)" exec /Users/me/.local/bin/todoist-takeout backup-and-sqlite --quiet /Users/me/Backups/todoist</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>StandardErrorPath</key>
  <string>/Users/me/Library/Logs/todoist-takeout.log</string>
</dict>
</plist>
```

## Task Scheduler (Windows)

Save a script such as `C:\Users\me\todoist-takeout.ps1`:

```powershell
$env:TODOIST_API_TOKEN = Get-Content "$env:USERPROFILE\.config\todoist-takeout\token" -Raw
todoist-takeout backup-and-sqlite --quiet "$env:USERPROFILE\Backups\todoist"
exit $LASTEXITCODE
```

and register it to run daily at 03:00:

```powershell
schtasks /Create /SC DAILY /ST 03:00 /TN "todoist-takeout" /TR "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\Users\me\todoist-takeout.ps1"
```
