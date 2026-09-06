# 启动网页控制面板 (PowerShell)
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  抖音自动发送 · 网页控制面板" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "正在启动 Web 服务..." -ForegroundColor Yellow
Write-Host "访问地址: http://127.0.0.1:5000" -ForegroundColor Green
Write-Host ""
Write-Host "按 Ctrl+C 停止服务" -ForegroundColor Gray
Write-Host ""

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
python web\app.py
