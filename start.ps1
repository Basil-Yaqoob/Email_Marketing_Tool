#!/usr/bin/env pwsh
<#
.SYNOPSIS
Start the Email Marketing Tool with all services.
#>

$ErrorActionPreference = "Stop"

Write-Host "🚀 Starting Email Marketing Tool..." -ForegroundColor Green

# Check if .env exists
if (-not (Test-Path .env)) {
    Write-Host "❌ .env file not found. Copy from .env.example:" -ForegroundColor Red
    Write-Host "   Copy-Item .env.example .env"
    exit 1
}

Write-Host "📦 Starting Docker services (PostgreSQL, Redis)..." -ForegroundColor Cyan
docker compose up -d postgres redis

Write-Host "⏳ Waiting for services to be ready..." -ForegroundColor Yellow
Start-Sleep -Seconds 3

Write-Host "🗄️  Running database migrations..." -ForegroundColor Cyan
uv run alembic upgrade head

Write-Host "✅ Setup complete. Starting application..." -ForegroundColor Green
Write-Host ""
Write-Host "🌐 Web UI:  http://localhost:8000" -ForegroundColor Cyan
Write-Host "📡 API:     http://localhost:8000/api/v1" -ForegroundColor Cyan
Write-Host "📚 Docs:    http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "🔌 MCP:     python -m app.mcp.server (separate terminal)" -ForegroundColor Cyan
Write-Host ""

uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
