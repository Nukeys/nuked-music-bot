# Deploy the bot to the Oracle server (or any Ubuntu box).
# Usage: powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -ServerIp 1.2.3.4 -KeyPath C:\path\to\ssh-key.key
param(
    [Parameter(Mandatory = $true)][string]$ServerIp,
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [string]$User = "ubuntu"
)
$ErrorActionPreference = "Stop"
$botDir = Split-Path $PSScriptRoot -Parent
$dest = "${User}@${ServerIp}"
$sshOpts = @("-i", $KeyPath, "-o", "StrictHostKeyChecking=accept-new")

Write-Host "Copying bot files to $dest..." -ForegroundColor Cyan
ssh @sshOpts $dest "mkdir -p /home/$User/discord-music-bot/deploy /home/$User/discord-music-bot/assets"
$files = @("bot.py", "requirements.txt", ".env", "deploy\musicbot.service", "deploy\remote-setup.sh")
if (Test-Path (Join-Path $botDir "playlists.json")) { $files += "playlists.json" }
foreach ($f in $files) {
    $remote = ($f -replace "\\", "/")
    scp @sshOpts (Join-Path $botDir $f) "${dest}:/home/$User/discord-music-bot/$remote"
}

Write-Host "Running server setup (installs ffmpeg/python, registers 24/7 service)..." -ForegroundColor Cyan
ssh @sshOpts $dest "sed -i 's/\r$//' /home/$User/discord-music-bot/deploy/remote-setup.sh && bash /home/$User/discord-music-bot/deploy/remote-setup.sh"

Write-Host "`nDeployed. The bot now runs 24/7 on the server (auto-restarts on crash or reboot)." -ForegroundColor Green
Write-Host "IMPORTANT: stop the local copy (close the 'Nuked Music Bot' window) so two instances aren't online." -ForegroundColor Yellow
