# Securely store your Discord bot token in .env without it appearing on screen
# or in shell history. Run:  powershell -ExecutionPolicy Bypass -File .\set-token.ps1
$secure = Read-Host "Paste your Discord bot token (input is hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$token = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "No token entered, nothing changed." -ForegroundColor Yellow
    exit 1
}

$envPath = Join-Path $PSScriptRoot ".env"
$content = Get-Content $envPath -Raw
$content = $content -replace "DISCORD_TOKEN=.*", "DISCORD_TOKEN=$token"
Set-Content -Path $envPath -Value $content -Encoding utf8 -NoNewline
Write-Host "Token saved to .env. You can now run start.bat" -ForegroundColor Green
