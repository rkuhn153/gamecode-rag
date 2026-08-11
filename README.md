# gamecode-rag

MCP server for **semantic search + call-graph** over **decompiled Unity Mono** C# codebases.

Use it as the “library” next to **[bepinex-mcp](https://github.com/rkuhn153/bepinex-mcp)** (the live game bridge).  
**Mono only** — for IL2CPP binaries use a decompiler MCP, not this.

## What it does

| Tool | Purpose |
|------|---------|
| `list_available_projects` | List ingested game indexes |
| `code_search_and_rerank` | Embed query → vector search → LLM re-rank top snippets |
| `code_graph_search` | Callers / callees for a method id |
| `ingest_new_project` | Build an index from decompiled sources or a Mono assembly path |

Queries for `code_search_and_rerank` should be **full natural-language questions** (not keyword bags).

## Requirements

- Python 3.10+
- [OpenRouter](https://openrouter.ai/) API key (embeddings + re-ranker)
- Per-game index under `PROJECT_DATABASES/<project_id>/` (you create these; **not** shipped)

## Setup

```bash
cd gamecode-rag   # this repo
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env → OPENROUTER_API_KEY=...
```

### Build an index

1. Produce a `code_graph.json` for a game (Roslyn / your decompile pipeline — nodes + edges of methods).
2. Ingest:

```bash
python ingest_code_graph.py --project-id my_game --source path/to/code_graph.json
```

This writes embeddings under `PROJECT_DATABASES/my_game/`.

Or use the MCP tool `ingest_new_project` once the server is running (can take a while; needs OpenRouter).

### Run the MCP server

```bash
python gamecode_rag_server.py
```

**Cursor** (`mcp.json`):

```json
"gamecode-rag": {
  "command": "C:/Python313/python.exe",
  "args": [
    "C:/path/to/gamecode-rag/gamecode_rag_server.py",
    "--transport=stdio"
  ]
}
```

**Grok** (`config.toml`):

```toml
[mcp_servers.gamecode-rag]
command = 'C:\Python313\python.exe'
args = ['C:\path\to\gamecode-rag\gamecode_rag_server.py', "--transport=stdio"]
enabled = true
```

## Pair with bepinex-mcp

| Need | Server |
|------|--------|
| Read Mono game code (offline index) | **gamecode-rag** (this repo) |
| Change the **running** game | [bepinex-mcp](https://github.com/rkuhn153/bepinex-mcp) |

Typical flow: search here → find `Player.TakeDamage` → live patch / set value with bepinex-mcp.

## Layout

```
gamecode-rag/
  gamecode_rag_server.py   # MCP server
  ingest_code_graph.py     # CLI ingest
  PROJECT_DATABASES/       # your local indexes (gitignored)
  requirements.txt
  .env.example
  Dockerfile
```

## License

MIT — use at your own risk. You are responsible for how you obtain and index game code.
