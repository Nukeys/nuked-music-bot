# Store Spotify API credentials in .env (enables Spotify album/playlist links).
# Run:  powershell -ExecutionPolicy Bypass -File .\set-spotify.ps1
$clientId = Read-Host "Paste your Spotify Client ID"
$secure = Read-Host "Paste your Spotify Client Secret (input is hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$secret = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

if ([string]::IsNullOrWhiteSpace($clientId) -or [string]::IsNullOrWhiteSpace($secret)) {
    Write-Host "Missing ID or secret, nothing changed." -ForegroundColor Yellow
    exit 1
}

$envPath = Join-Path $PSScriptRoot ".env"
$content = Get-Content $envPath -Raw
$content = $content -replace "SPOTIFY_CLIENT_ID=.*", "SPOTIFY_CLIENT_ID=$clientId"
$content = $content -replace "SPOTIFY_CLIENT_SECRET=.*", "SPOTIFY_CLIENT_SECRET=$secret"
Set-Content -Path $envPath -Value $content -Encoding utf8 -NoNewline
Write-Host "Spotify credentials saved. Restart the bot to enable album/playlist support." -ForegroundColor Green
