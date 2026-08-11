# gamecode-rag

MCP server for **semantic search + call-graph** over **decompiled Unity Mono** C# codebases.

Use it as the “library” next to the live bridge. **Mono only** — for IL2CPP use the decompiler, not this.

### Related projects (same suite)

| Repo | Role | When |
|------|------|------|
| **This** — [gamecode-rag](https://github.com/rkuhn153/gamecode-rag) | Semantic search + call graph over dumped **Mono** C# | Game is **Mono** |
| [bepinex-mcp](https://github.com/rkuhn153/bepinex-mcp) | Live Unity bridge (get/set/patch/watch) | Game is running with BepInEx |
| [il2cpp-decompiler](https://github.com/rkuhn153/il2cpp-decompiler) | Static IL2CPP decompile (needs [Il2CppDumper](https://github.com/Perfare/Il2CppDumper)) | Game is **IL2CPP** |

## What it does

| Tool | Purpose |
|------|---------|
| `list_available_projects` | List ingested game indexes |
| `code_search_and_rerank` | Hybrid search (vectors + symbols) → LLM re-rank top snippets |
| `code_graph_search` | Callers / callees for a method id |
| `ingest_new_project` | Build an index from decompiled sources or a Mono assembly path |

Queries for `code_search_and_rerank` should be **full natural-language questions** (not keyword bags). Symbol names (`TakeDamage`, `PlayerController`) still work well thanks to hybrid search.

### How search works

1. **Hybrid retrieval** — dense embeddings + keyword/symbol match over method/class ids, fused with RRF  
2. **LLM re-rank** — scores the broad set down to a short list  
3. **Call graph** — optional follow-up via `code_graph_search`

Indexes under `PROJECT_DATABASES/` are **lazy-loaded**: startup only scans folder names; a project’s vectors enter RAM on the **first** search/graph call for that `project_id`.

### Embedding models

Default (OpenRouter): **`qwen/qwen3-embedding-8b`** — strong on code/retrieval, long context (~32k), and typically **cheaper** than OpenAI `text-embedding-3-small` on OpenRouter.

**Local embeddings** (optional): any OpenAI-compatible `/v1/embeddings` server:

```env
EMBEDDING_BACKEND=openai_compatible
EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1
EMBEDDING_MODEL=qwen3-embedding:0.6b
```

Works with Ollama, LM Studio, vLLM, TEI, etc. Re-ranker still uses OpenRouter by default (`OPENROUTER_API_KEY` + `RE_RANKER_MODEL`); if the key is missing, search skips re-rank and returns hybrid order.

Default re-ranker: **`deepseek/deepseek-v4-flash`** — typically cheaper and stronger at code relevance than `openai/gpt-4o-mini`. Override with `RE_RANKER_MODEL` if you prefer another OpenRouter chat model.

Use the **same** `EMBEDDING_MODEL` for ingest and query. Changing models requires **re-ingesting** every project (old indexes won’t load).

## Requirements

- Python 3.10+
- .NET 9 SDK (for the Roslyn code-graph tool)
- Embeddings: OpenRouter **or** a local OpenAI-compatible embed server
- OpenRouter key recommended for LLM re-rank (optional if you accept hybrid-only ranking)
- Per-game index under `PROJECT_DATABASES/<project_id>/` (you create these; **not** shipped)

## Setup

```bash
cd gamecode-rag   # this repo
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env → OPENROUTER_API_KEY=...  (and optional local EMBEDDING_* — see above)

# Build the C# Roslyn parser (required for full ingest)
dotnet build tools/roslyn-parser/RoslynCodeGraph.csproj -c Release
```

### Build an index

**Expect this to take a while.** Indexing a full game is not a quick script:

| Step | What happens | Rough time |
|------|----------------|------------|
| Decompile (if needed) | `ilspycmd` dumps `.cs` from `Assembly-CSharp` | Often **minutes**; large Unity games can be slow or need retries |
| Roslyn graph | Walks every file, builds methods + call edges → `code_graph.json` | Often **several minutes** |
| Embed + ingest | OpenRouter embeddings for each method/chunk | Often **many minutes to tens of minutes** (size + API rate limits) |

You only do this **once per game** (or when you re-ingest). After that, search is fast. Leave the process running and don’t cancel mid-embed unless you mean to restart from that step.

**Option A — folder of decompiled `.cs` files**

```bash
# 1) Roslyn → code_graph.json  (can take several minutes)
dotnet run --project tools/roslyn-parser -c Release -- \
  --project-path "D:\path\to\decompiled\Assembly-CSharp" \
  --output "PROJECT_DATABASES\my_game\code_graph.json"

# 2) Embed + call graph  (usually the slowest step)
python ingest_code_graph.py --project-id my_game --source PROJECT_DATABASES/my_game/code_graph.json
```

**Option B — MCP one-shot** (`ingest_new_project` with `assembly_path` or `source_code_path`): decompiles via `ilspycmd` when needed, runs Roslyn, then embeds. Same long pipeline in one tool call — keep the MCP client open until it finishes. Needs OpenRouter and a built `RoslynCodeGraph.exe`.

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

## Pair with the suite

| Need | Server |
|------|--------|
| Read **Mono** game code (offline index) | **gamecode-rag** (this repo) |
| Change the **running** game | [bepinex-mcp](https://github.com/rkuhn153/bepinex-mcp) |
| Read **IL2CPP** methods (static decompile) | [il2cpp-decompiler](https://github.com/rkuhn153/il2cpp-decompiler) |

Typical Mono flow: search here → find `Player.TakeDamage` → live patch with bepinex-mcp.

## Layout

```
gamecode-rag/
  gamecode_rag_server.py      # MCP server
  ingest_code_graph.py        # CLI embed + call-graph save
  embeddings_client.py        # OpenRouter or local OpenAI-compatible embeds
  tools/roslyn-parser/        # C# Roslyn → code_graph.json
  PROJECT_DATABASES/          # your local indexes (gitignored)
  requirements.txt
  .env.example
  Dockerfile
```

## License

MIT — use at your own risk. You are responsible for how you obtain and index game code.
