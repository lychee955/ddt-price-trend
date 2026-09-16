$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$trackerUv = Get-Command uv -ErrorAction SilentlyContinue
if ($trackerUv) {
    & $trackerUv.Source run --locked ddt serve @args
} elseif (Test-Path -LiteralPath "$PSScriptRoot/.tools/uv-package/bin/uv.exe") {
    & "$PSScriptRoot/.tools/uv-package/bin/uv.exe" run --locked --cache-dir "$PSScriptRoot/.tools/uv-cache" ddt serve @args
} else {
    throw '请安装 uv，然后按 README 构建前端并启动项目。'
}
exit $LASTEXITCODE
