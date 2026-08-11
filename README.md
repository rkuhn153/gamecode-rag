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
- .NET 9 SDK (for the Roslyn code-graph tool)
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

# Build the C# Roslyn parser (required for full ingest)
dotnet build tools/roslyn-parser/RoslynCodeGraph.csproj -c Release
```

### Build an index

**Option A — folder of decompiled `.cs` files**

```bash
# 1) Roslyn → code_graph.json
dotnet run --project tools/roslyn-parser -c Release -- \
  --project-path "D:\path\to\decompiled\Assembly-CSharp" \
  --output "PROJECT_DATABASES\my_game\code_graph.json"

# 2) Embed + write graph
python ingest_code_graph.py --project-id my_game --source PROJECT_DATABASES/my_game/code_graph.json
```

**Option B — MCP one-shot** (`ingest_new_project` with `assembly_path` or `source_code_path`): decompiles via `ilspycmd` when needed, runs Roslyn, then embeds. Needs OpenRouter and a built `RoslynCodeGraph.exe`.

This writes under `PROJECT_DATABASES/my_game/`.
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
  gamecode_rag_server.py      # MCP server
  ingest_code_graph.py        # CLI embed + call-graph save
  tools/roslyn-parser/        # C# Roslyn → code_graph.json
  PROJECT_DATABASES/          # your local indexes (gitignored)
  requirements.txt
  .env.example
  Dockerfile
```

## License

MIT — use at your own risk. You are responsible for how you obtain and index game code.
