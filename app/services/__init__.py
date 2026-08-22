"""Application service layer — the one place business logic lives.

`app/api`, `app/web` and `app/mcp/server` are transports: they translate a
request into a call here and a result back out. Nothing above this layer
touches a repository, a resolver, or an LLM directly (CLAUDE.md §6).
"""
