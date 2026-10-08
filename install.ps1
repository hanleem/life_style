# 가족 비서 설치 스크립트 — '설치하기.bat' 이 실행합니다.
# 비밀번호는 이 스크립트가 다루지 않습니다. Tailscale 과 ChatGPT(Codex) 로그인은 브라우저에서 직접 하시고, 끝나면 Enter 를 눌러 주세요.
# 여러 번 실행해도 안전합니다(이미 된 단계는 건너뜁니다).
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$Port = 8010
$TaskName = '가족비서'

function Say($t)  { Write-Host ""; Write-Host "▶ $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  ✓ $t" -ForegroundColor Green }
function Note($t) { Write-Host "  · $t" -ForegroundColor Yellow }
function Fail($t) {
    Write-Host ""
    Write-Host "✗ $t" -ForegroundColor Red
    Write-Host "  이 검은 창을 그대로 사진으로 찍어 보내 주시면 도와드릴 수 있어요."
    exit 1
}
function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    foreach ($extra in @("$env:ProgramFiles\Tailscale", "$env:APPDATA\npm", "$env:USERPROFILE\.local\bin")) {
        if ((Test-Path $extra) -and ($env:Path -notlike "*$extra*")) { $env:Path += ";$extra" }
    }
}
function Wait-Enter($msg) {
    Write-Host ""
    Write-Host "  $msg" -ForegroundColor White
    [void](Read-Host "  (끝났으면 Enter 키를 눌러 주세요)")
}
function Run-Quiet($file, $argList) {
    # 종료 코드만 돌려준다(출력은 숨긴다). 프로그램이 없으면 127.
    try { & $file @argList *> $null; return $LASTEXITCODE } catch { return 127 }
}

Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "   가족 비서 설치를 시작할게요" -ForegroundColor Cyan
Write-Host "   10~20분쯤 걸려요. 창을 닫지 마세요." -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan

# ---- 1. 필요한 프로그램 설치 -------------------------------------------------
Say "1/7  필요한 프로그램을 설치해요"
if (-not (Have 'winget')) {
    Fail "윈도우의 '앱 설치 관리자'(winget)가 없어요. Microsoft Store 에서 '앱 설치 관리자'를 업데이트한 뒤 다시 실행해 주세요."
}
$apps = @(
    @{ Id = 'astral-sh.uv';        Cmd = 'uv';        Name = 'uv (파이썬 도구)' },
    @{ Id = 'OpenJS.NodeJS.LTS';   Cmd = 'node';      Name = 'Node.js' },
    @{ Id = 'Tailscale.Tailscale'; Cmd = 'tailscale'; Name = 'Tailscale (휴대폰 연결)' }
)
Refresh-Path
foreach ($a in $apps) {
    if (Have $a.Cmd) { Ok "$($a.Name): 이미 있어요"; continue }
    Note "$($a.Name) 설치 중..."
    winget install --id $a.Id -e --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (-not (Have $a.Cmd)) { Fail "$($a.Name) 설치를 마치지 못했어요." }
    Ok "$($a.Name): 설치했어요"
}

# ---- 2. 가족 비서 준비 -------------------------------------------------------
Say "2/7  가족 비서를 준비해요"
if (-not (Have 'codex')) {
    Note "AI 프로그램(Codex) 설치 중..."
    npm i -g @openai/codex
    Refresh-Path
    if (-not (Have 'codex')) { Fail "AI 프로그램(Codex) 설치를 마치지 못했어요." }
}
Ok "AI 프로그램(Codex) 준비됐어요"
uv sync
if ($LASTEXITCODE -ne 0) { Fail "가족 비서 프로그램을 준비하지 못했어요(uv sync)." }
Ok "가족 비서 프로그램 준비됐어요"

# ---- 3. 설정 파일 ------------------------------------------------------------
Say "3/7  설정 파일을 만들어요(비밀 키는 자동으로 만들어져요)"
uv run python -m tools.setup_env
if ($LASTEXITCODE -ne 0) { Fail "설정 파일을 만들지 못했어요." }

# ---- 4. 직접 하실 일: 로그인 두 가지 -----------------------------------------
Say "4/7  로그인 두 가지는 직접 해 주세요 (비밀번호는 저한테 알려 주지 않아도 돼요)"
function Tailscale-LoggedIn {
    try { $s = (& tailscale status --json 2>$null | Out-String | ConvertFrom-Json); return $s.BackendState -eq 'Running' } catch { return $false }
}
for ($i = 0; $i -lt 3 -and -not (Tailscale-LoggedIn); $i++) {
    Write-Host ""
    Write-Host "  [휴대폰 연결 · Tailscale]" -ForegroundColor White
    Write-Host "  곧 브라우저가 열려요. 구글이나 애플 계정으로 로그인해 주세요."
    Write-Host "  (아이폰에서도 '같은 계정'으로 로그인할 거예요.)"
    & tailscale up --timeout=180s
    if (-not (Tailscale-LoggedIn)) { Wait-Enter "로그인이 끝나면 Enter 를 눌러 주세요." }
}
if (-not (Tailscale-LoggedIn)) { Fail "Tailscale 로그인이 확인되지 않았어요. 화면 오른쪽 아래 Tailscale 아이콘으로 로그인한 뒤 '설치하기'를 다시 실행해 주세요." }
Ok "Tailscale 로그인 확인했어요"

for ($i = 0; $i -lt 3 -and ((Run-Quiet 'codex' @('login', 'status')) -ne 0); $i++) {
    Write-Host ""
    Write-Host "  [AI 연결 · ChatGPT]" -ForegroundColor White
    Write-Host "  곧 브라우저가 열려요. 사용 중인 ChatGPT 계정으로 로그인해 주세요."
    & codex login
    if ((Run-Quiet 'codex' @('login', 'status')) -ne 0) { Wait-Enter "로그인이 끝나면 Enter 를 눌러 주세요." }
}
if ((Run-Quiet 'codex' @('login', 'status')) -ne 0) { Fail "ChatGPT 로그인이 확인되지 않았어요. 검은 창에 'codex login' 을 입력해 로그인한 뒤 '설치하기'를 다시 실행해 주세요." }
Ok "ChatGPT 로그인 확인했어요"

# ---- 5. 휴대폰 접속 주소 -----------------------------------------------------
Say "5/7  휴대폰이 접속할 주소를 열어요"
$served = $false
for ($i = 0; $i -lt 3 -and -not $served; $i++) {
    & tailscale serve --bg --https=443 "http://127.0.0.1:$Port"
    if ($LASTEXITCODE -eq 0) { $served = $true; break }
    Write-Host ""
    Write-Host "  Tailscale 에서 'HTTPS 인증서'를 켜야 할 수 있어요." -ForegroundColor White
    Write-Host "  브라우저에서 https://login.tailscale.com/admin/dns 를 열고 'HTTPS Certificates' 의 Enable 을 눌러 주세요."
    Wait-Enter "켰으면 Enter 를 눌러 주세요."
}
if (-not $served) { Fail "휴대폰 접속 주소를 열지 못했어요. Tailscale 의 'HTTPS 인증서'를 켠 뒤 '설치하기'를 다시 실행해 주세요." }
uv run python -m tools.setup_env --tailnet
Ok "휴대폰 접속 주소를 열었어요"

# ---- 6. 자동 실행 + 바탕화면 바로가기 -----------------------------------------
Say "6/7  컴퓨터를 켜면 자동으로 켜지게 하고, 바탕화면에 바로가기를 만들어요"
$action   = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c start `"`" /min `"$root\run_pa.bat`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Ok "자동 실행을 등록했어요"
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = Join-Path $desktop '가족비서 열기.url'
Set-Content -Path $shortcut -Value "[InternetShortcut]`r`nURL=http://localhost:$Port/" -Encoding ASCII
Ok "바탕화면에 '가족비서 열기'를 만들었어요"

# ---- 7. 켜기 -----------------------------------------------------------------
Say "7/7  가족 비서를 켜요"
function Server-Up {
    try { return (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3).StatusCode -eq 200 } catch { return $false }
}
if (-not (Server-Up)) {
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'start', '""', '/min', "`"$root\run_pa.bat`""
    for ($i = 0; $i -lt 45 -and -not (Server-Up); $i++) { Start-Sleep -Seconds 2 }
}
if (-not (Server-Up)) { Fail "가족 비서가 켜지지 않았어요. '문제해결'을 실행해 보세요." }
Ok "가족 비서가 켜졌어요"
Start-Process "http://localhost:$Port/welcome"

Write-Host ""
Write-Host "=====================================" -ForegroundColor Green
Write-Host "   설치가 끝났어요! 🎉" -ForegroundColor Green
Write-Host "   브라우저에서 질문에 답해 주세요(5분)." -ForegroundColor Green
Write-Host "   마지막에 '휴대폰 연결하기'로 QR 을 찍으면 끝나요." -ForegroundColor Green
Write-Host "   자세한 안내는 '시작하기.md' 를 보세요." -ForegroundColor Green
Write-Host "=====================================" -ForegroundColor Green
