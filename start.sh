#!/bin/bash
set -e

echo "🚀 Starting Email Marketing Tool..."

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ .env file not found. Copy from .env.example:"
    echo "   cp .env.example .env"
    exit 1
fi

echo "📦 Starting Docker services (PostgreSQL, Redis)..."
docker compose up -d postgres redis

echo "⏳ Waiting for services to be ready..."
sleep 3

echo "🗄️  Running database migrations..."
uv run alembic upgrade head

echo "✅ Setup complete. Starting application..."
echo ""
echo "🌐 Web UI:  http://localhost:8000"
echo "📡 API:     http://localhost:8000/api/v1"
echo "📚 Docs:    http://localhost:8000/docs"
echo "🔌 MCP:     python -m app.mcp.server (separate terminal)"
echo ""

uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
