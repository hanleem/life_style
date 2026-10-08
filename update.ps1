# 가족 비서 업데이트 — '업데이트.bat' 이 실행합니다. 새 버전을 받고, 필요한 것을 맞추고, 다시 켭니다. 데이터(data 폴더)는 건드리지 않아요.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($t)  { Write-Host ""; Write-Host "▶ $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  ✓ $t" -ForegroundColor Green }
function Fail($t) { Write-Host ""; Write-Host "✗ $t" -ForegroundColor Red; exit 1 }
function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }
foreach ($extra in @("$env:ProgramFiles\Tailscale", "$env:APPDATA\npm", "$env:USERPROFILE\.local\bin")) {
    if ((Test-Path $extra) -and ($env:Path -notlike "*$extra*")) { $env:Path += ";$extra" }
}

Say "1/3  새 버전을 받아요"
if (-not (Test-Path (Join-Path $root '.git'))) { Fail "이 폴더는 Git 으로 받은 폴더가 아니에요. 처음 받았던 방법으로 새 버전을 받아 주세요." }
if (-not (Have 'git')) { Fail "Git 이 설치되어 있지 않아요. 검은 창에 'winget install Git.Git' 를 입력해 설치한 뒤 다시 실행해 주세요." }
git pull --ff-only
if ($LASTEXITCODE -ne 0) { Fail "새 버전을 받지 못했어요. 인터넷 연결을 확인하고, 파일을 직접 고친 적이 있다면 알려 주세요." }
Ok "새 버전을 받았어요"

Say "2/3  필요한 것을 맞춰요"
if (-not (Have 'uv')) { Fail "uv 가 없어요. '설치하기'를 다시 실행해 주세요." }
uv sync
if ($LASTEXITCODE -ne 0) { Fail "필요한 것을 맞추지 못했어요(uv sync)." }
Ok "맞췄어요"

Say "3/3  다시 켜요"
uv run python -m tools.doctor --restart
Start-Sleep -Seconds 15
try {
    $up = (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8010/health' -TimeoutSec 5).StatusCode -eq 200
} catch { $up = $false }
if ($up) { Ok "새 버전으로 켜졌어요" } else { Write-Host "  · 아직 켜지는 중일 수 있어요. 1분 뒤 '문제해결'을 실행해 확인해 보세요." -ForegroundColor Yellow }
Write-Host ""
Write-Host "업데이트가 끝났어요." -ForegroundColor Green
