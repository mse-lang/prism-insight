$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$tsExe = (Get-Command tailscale.exe -ErrorAction Stop).Source
$tsState = & $tsExe status --json | ConvertFrom-Json
if ($tsState.BackendState -ne 'Running' -or -not $tsState.Self.DNSName) {
    throw 'Tailscale에 로그인한 후 다시 실행해주세요.'
}
$mobileOrigin = 'https://' + $tsState.Self.DNSName.TrimEnd('.')
$serveText = & $tsExe serve status --json
if ($LASTEXITCODE -ne 0) { throw 'Tailscale 연결 상태를 확인하지 못했습니다.' }
$serveConfig = $serveText | ConvertFrom-Json
if ($serveConfig.TCP.'443' -and $serveText -notmatch '127\.0\.0\.1:8868') {
    throw '이미 사용 중인 Tailscale HTTPS 연결이 있습니다. 기존 서비스를 보존하기 위해 시작하지 않았습니다.'
}
& $tsExe serve --bg --https=443 http://127.0.0.1:8868
if ($LASTEXITCODE -ne 0) { throw 'Tailscale 보안 연결을 활성화해주세요.' }
$env:PYTHONUTF8 = '1'
$taskPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
Write-Host ('Android 보안 주소: ' + $mobileOrigin)
Write-Host 'PC 매매 제안 화면에서 휴대폰 연결 코드를 발급하세요. 자동매매는 정지 상태로 시작합니다.'
if (Test-Path -LiteralPath $taskPython) {
    & $taskPython -X utf8 -m personal.server --mobile-origin $mobileOrigin @args
} else {
    py -3 -X utf8 -m personal.server --mobile-origin $mobileOrigin @args
}
