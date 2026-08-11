#!/usr/bin/env python3
"""
GameCode RAG MCP Server (v12) - MULTI-TENANT

This server implements the "Search-Rerank-Graph" (v11) pipeline,
but has been upgraded to be "multi-tenant".

v12:
- On startup, scans './PROJECT_DATABASES/' and loads ALL game DBs.
- Adds a `list_available_projects` tool.
- Adds a mandatory `project_id` argument to all other tools.
"""

import os
import sys
import re
import logging
import json
import httpx
import asyncio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
import textwrap

# ---
# Path setup (Absolute paths are crucial)
# ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=dotenv_path)

# embeddings_client reads env after load_dotenv
from embeddings_client import (  # noqa: E402
    EMBEDDING_MODEL,
    embeddings_ready,
    fetch_embedding,
    log_embedding_config,
)

# ---
# Configuration
# ---
RE_RANKER_MODEL = os.environ.get("RE_RANKER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

BROAD_SEARCH_K = int(os.environ.get("BROAD_SEARCH_K", "25"))
KEYWORD_SEARCH_K = int(os.environ.get("KEYWORD_SEARCH_K", "25"))
RRF_K = 60  # Reciprocal Rank Fusion constant

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("gamecode-rag-server")

mcp = FastMCP("gamecode-rag")

# ---
# NEW: Global State for Multi-Tenant
# ---
# This will hold all loaded game DBs, e.g.:
# GAME_DATABASES = {
#   "another_crabs_treasure": { "db": {...}, "graph": [...] },
#   "lethal_company": { "db": {...}, "graph": [...] }
# }
GAME_DATABASES = {}


def _find_roslyn_parser_exe() -> str | None:
    """Locate RoslynCodeGraph (or legacy RagC#) executable next to this repo."""
    candidates = [
        os.path.join(BASE_DIR, "tools", "roslyn-parser", "bin", "Release", "net9.0", "RoslynCodeGraph.exe"),
        os.path.join(BASE_DIR, "tools", "roslyn-parser", "bin", "Debug", "net9.0", "RoslynCodeGraph.exe"),
        # Legacy sibling layout (Game Modding/RagC#/...)
        os.path.join(os.path.dirname(BASE_DIR), "RagC#", "RagC#", "bin", "Release", "net9.0", "RagC#.exe"),
        os.path.join(os.path.dirname(BASE_DIR), "RagC#", "RagC#", "bin", "Debug", "net9.0", "RagC#.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ---
# Server Startup Event
# ---
def load_all_projects():
    """
    Scans the 'PROJECT_DATABASES' directory and loads all
    valid game 'brains' into the GAME_DATABASES dictionary.
    """
    global GAME_DATABASES
    GAME_DATABASES = {}  # Clear on reload

    projects_dir = os.path.join(BASE_DIR, "PROJECT_DATABASES")
    if not os.path.exists(projects_dir):
        logger.warning(f"'PROJECT_DATABASES' directory not found. No projects will be loaded.")
        return

    logger.info(f"Scanning for projects in: {projects_dir}")
    for project_id in os.listdir(projects_dir):
        project_path = os.path.join(projects_dir, project_id)
        if not os.path.isdir(project_path):
            continue

        db_path = os.path.join(project_path, "rag_vector_db.json")
        graph_path = os.path.join(project_path, "rag_call_graph.json")

        if os.path.exists(db_path) and os.path.exists(graph_path):
            try:
                logger.info(f"Loading project '{project_id}'...")
                with open(db_path, 'r', encoding='utf-8') as f:
                    db_data = json.load(f)
                with open(graph_path, 'r', encoding='utf-8') as f:
                    graph_data = json.load(f)

                # Verify the DB model
                db_model = db_data.get("model", "N/A")
                if db_model != EMBEDDING_MODEL:
                    logger.error(
                        f"Failed to load project '{project_id}': DB model ({db_model}) does not match server model ({EMBEDDING_MODEL}).")
                    logger.error("Please re-ingest this project with the new 'ingest_code_graph.py' script.")
                    continue

                GAME_DATABASES[project_id] = {
                    "db": db_data,
                    "graph": graph_data
                }
                logger.info(f"Successfully loaded project '{project_id}'.")
            except Exception as e:
                logger.error(f"Failed to load project '{project_id}': {e}", exc_info=True)
        else:
            logger.warning(f"Skipping directory '{project_id}' (missing db or graph file).")


# === UTILITY FUNCTIONS ===

async def get_real_embedding(client, text: str = ""):
    try:
        return await fetch_embedding(client, text or "empty query", timeout=30.0)
    except Exception as e:
        logger.error(f"Error getting query embedding: {e}", exc_info=True)
        return None


def cosine_similarity(vec_a: list, vec_b: list):
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = sum(a * a for a in vec_a) ** 0.5
    mag_b = sum(b * b for b in vec_b) ** 0.5
    if mag_a == 0 or mag_b == 0: return 0
    return dot_product / (mag_a * mag_b)


async def real_vector_search(client, question: str = "", vector_db: dict = None, k: int = 25):
    """Dense retrieval: cosine similarity over stored embeddings. Returns list of node dicts."""
    logger.debug(f"Executing vector search for: {question} (k={k})")
    question_vector = await get_real_embedding(client, question)
    if not question_vector:
        return []

    scored_nodes = []
    db_vectors = (vector_db or {}).get("vectors", {})
    db_metadata = (vector_db or {}).get("metadata", {})

    for node_id, vector in db_vectors.items():
        score = cosine_similarity(question_vector, vector)
        scored_nodes.append((score, node_id))

    scored_nodes.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, node_id in scored_nodes[:k]:
        if node_id in db_metadata and score > 0.2:
            results.append(db_metadata[node_id])
    logger.debug(f"Vector search found {len(results)} relevant nodes.")
    return results


def _extract_search_tokens(query: str) -> list[str]:
    """Pull C#-ish identifiers and dotted names from a natural-language query."""
    if not query:
        return []
    # Prefer longer dotted symbols first (Player.TakeDamage)
    dotted = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b", query)
    tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{1,}\b", query)
    # Drop ultra-common English words that pollute keyword match
    stop = {
        "the", "a", "an", "how", "does", "do", "is", "are", "what", "when", "where",
        "who", "why", "find", "code", "related", "to", "for", "and", "or", "of", "in",
        "on", "with", "from", "that", "this", "into", "about", "me", "my", "get", "set",
    }
    out: list[str] = []
    seen: set[str] = set()
    for t in dotted + tokens:
        key = t.lower()
        if key in stop or key in seen:
            continue
        if len(t) < 2:
            continue
        seen.add(key)
        out.append(t)
    return out


def keyword_symbol_search(query: str = "", vector_db: dict = None, k: int = 25) -> list:
    """
    Sparse / symbol retrieval over node Id + content.
    Strong for exact type/method names (TakeDamage, PlayerController).
    Returns list of node dicts ordered by keyword score.
    """
    db_metadata = (vector_db or {}).get("metadata", {})
    if not db_metadata:
        return []

    tokens = _extract_search_tokens(query)
    if not tokens:
        return []

    tokens_lower = [t.lower() for t in tokens]
    scored: list[tuple[float, str]] = []

    for node_id, node in db_metadata.items():
        nid = (node_id or "").lower()
        content = (node.get("Content") or "").lower()
        ntype = (node.get("Type") or "").lower()
        score = 0.0

        for raw, tok in zip(tokens, tokens_lower):
            # Exact / full-id hits
            if nid == tok or nid.endswith("." + tok):
                score += 12.0
            elif tok in nid:
                score += 6.0
            # Method-style: Id contains Token(
            if f"{tok}(" in nid or f".{tok}(" in nid:
                score += 8.0
            # Content / signature mentions
            if tok in content:
                score += 1.5
            # Prefer method nodes slightly for action-y queries
            if ntype == "method" and tok in nid:
                score += 1.0
            # CamelCase boundary bonus: query "damage" vs "TakeDamage"
            if len(tok) >= 4 and tok in re.sub(r"([a-z])([A-Z])", r"\1 \2", node_id or "").lower():
                score += 2.0

        if score > 0:
            scored.append((score, node_id))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, node_id in scored[:k]:
        if node_id in db_metadata:
            results.append(db_metadata[node_id])
    logger.debug(f"Keyword/symbol search found {len(results)} nodes (tokens={tokens[:12]})")
    return results


def _rrf_fuse_node_lists(ranked_lists: list, k: int = RRF_K, limit: int = 25) -> list:
    """Reciprocal Rank Fusion over lists of node dicts (must have 'Id')."""
    scores: dict[str, float] = {}
    node_by_id: dict[str, dict] = {}

    for ranked in ranked_lists:
        if not ranked:
            continue
        for rank, node in enumerate(ranked):
            node_id = node.get("Id")
            if not node_id:
                continue
            node_by_id[node_id] = node
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (k + rank + 1)

    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [node_by_id[nid] for nid, _ in ordered[:limit] if nid in node_by_id]


async def hybrid_search(client, question: str = "", vector_db: dict = None, k: int = 25) -> list:
    """
    Hybrid retrieval: dense vectors + keyword/symbol match, fused with RRF.
    Falls back to whichever side returns results if the other is empty.
    """
    dense = await real_vector_search(client, question, vector_db, k=k)
    sparse = keyword_symbol_search(question, vector_db, k=KEYWORD_SEARCH_K)

    if dense and sparse:
        fused = _rrf_fuse_node_lists([dense, sparse], limit=k)
        logger.info(
            f"Hybrid search: dense={len(dense)} keyword={len(sparse)} fused={len(fused)}"
        )
        return fused
    if dense:
        logger.info(f"Hybrid search: dense-only ({len(dense)})")
        return dense[:k]
    if sparse:
        logger.info(f"Hybrid search: keyword-only ({len(sparse)})")
        return sparse[:k]
    return []


def find_downstream_calls(start_node_id: str = "", call_graph: list = None, depth_str: str = "2"):
    try:
        depth = int(depth_str) if depth_str.strip() else 2
    except ValueError:
        depth = 2
    if call_graph is None: call_graph = []
    downstream_nodes = [];
    visited = set();
    queue = [(start_node_id, 0)]
    while queue:
        current_id, current_depth = queue.pop(0)
        if current_id in visited or current_depth >= depth: continue
        visited.add(current_id)
        for edge in call_graph:
            if edge.get("SourceId") == current_id:
                target_id = edge.get("TargetId")
                if target_id and target_id not in visited:
                    downstream_nodes.append(target_id);
                    queue.append((target_id, current_depth + 1))
    return downstream_nodes


def find_upstream_calls(start_node_id: str = "", call_graph: list = None, depth_str: str = "2"):
    try:
        depth = int(depth_str) if depth_str.strip() else 2
    except ValueError:
        depth = 2
    if call_graph is None: call_graph = []
    upstream_nodes = [];
    visited = set();
    queue = [(start_node_id, 0)]
    while queue:
        current_id, current_depth = queue.pop(0)
        if current_id in visited or current_depth >= depth: continue
        visited.add(current_id)
        for edge in call_graph:
            if edge.get("TargetId") == current_id:
                source_id = edge.get("SourceId")
                if source_id and source_id not in visited:
                    upstream_nodes.append(source_id);
                    queue.append((source_id, current_depth + 1))
    return upstream_nodes


def format_node_to_string(node: dict):
    content = textwrap.indent(node.get('Content', 'N/A'), '    ')
    return f"\nFile: {node.get('FilePath', 'N/A')} (Lines: {node.get('LineStart', 'N/A')}-{node.get('LineEnd', 'N/A')})\nID: {node.get('Id', 'N/A')}\nType: {node.get('Type', 'N/A')}\n```csharp\n{content}\n```\n"


# === MCP TOOLS (NEW v12 MULTI-TENANT) ===

@mcp.tool()
async def list_available_projects() -> str:
    """Lists all game code projects that are loaded and available to be queried."""
    logger.info("Executing list_available_projects...")
    if not GAME_DATABASES:
        return "❌ No projects are loaded. Please run the ingest script."

    project_keys = list(GAME_DATABASES.keys())
    return f"✅ Available projects: {', '.join(project_keys)}"


@mcp.tool()
async def code_search_and_rerank(project_id: str = "", query: str = "") -> str:
    """Hybrid search (dense embeddings + keyword/symbol) then AI re-rank for the top C# snippets in a project."""
    logger.info(f"Executing code_search_and_rerank for project='{project_id}', query='{query}'")
    if not project_id.strip():
        return f"❌ Error: project_id parameter is required. Available projects: {list(GAME_DATABASES.keys())}"
    if not query.strip():
        return "❌ Error: Query parameter is required."

    ok_embed, embed_err = embeddings_ready()
    if not ok_embed:
        return f"❌ Error: embeddings not configured: {embed_err}"

    # --- 1. Get the correct project DB ---
    project_data = GAME_DATABASES.get(project_id)
    if not project_data:
        return f"❌ Error: Project '{project_id}' not found. Available projects: {list(GAME_DATABASES.keys())}"

    vector_db = project_data["db"]

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            # --- 2. Hybrid broad search (dense + keyword/symbol, RRF fuse) ---
            logger.debug(f"Step 1: Running hybrid search on '{project_id}'...")
            broad_search_nodes = await hybrid_search(client, query, vector_db, k=BROAD_SEARCH_K)
            if not broad_search_nodes:
                return "🔍 No relevant code snippets found for that query."

            # --- 3. AI Re-ranking (optional if no OpenRouter key; fall back to hybrid order) ---
            if not OPENROUTER_API_KEY:
                logger.warning("OPENROUTER_API_KEY unset — skipping LLM re-rank, using hybrid order.")
                winning_nodes = broad_search_nodes[:5]
            else:
                logger.debug(f"Step 2: Sending {len(broad_search_nodes)} nodes to Re-ranker AI...")
                reranker_system_prompt = "You are an expert at reading code. Your only job is to score code snippets for relevance to a user query. You MUST return ONLY a valid JSON object."
                reranker_user_prompt = f"User Query: \"{query}\"\n\nHere is a list of {len(broad_search_nodes)} code snippets. Score EACH snippet's relevance from 0.0 to 1.0.\n"
                reranker_user_prompt += "Return a JSON object where each key is the snippet's 'Id' and the value is its score.\n"
                reranker_user_prompt += "Example format: {\"AchievementThrower.CheckAllAchievements()\": 0.9, \"Readme.Section\": 0.1, ...}\n\n"

                snippet_map_for_prompt = {}
                for i, node in enumerate(broad_search_nodes):
                    temp_id = f"snippet_{i + 1}"
                    snippet_map_for_prompt[temp_id] = node['Id']
                    reranker_user_prompt += f"--- {temp_id} (ID: {node['Id']}) ---\n{node['Content'][:1000]}...\n\n"

                payload = {
                    "model": RE_RANKER_MODEL, "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": reranker_system_prompt},
                                 {"role": "user", "content": reranker_user_prompt}]
                }
                headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}

                response = await client.post(OPENROUTER_CHAT_URL, headers=headers, json=payload, timeout=60.0)
                response.raise_for_status()
                scores_str = response.json()['choices'][0]['message']['content']
                logger.debug(f"Re-ranker AI raw response: {scores_str}")

                scores_obj = json.loads(scores_str)
                scored_ids = []
                for temp_id, score in scores_obj.items():
                    if temp_id in snippet_map_for_prompt:
                        real_id = snippet_map_for_prompt[temp_id]
                        scored_ids.append((float(score), real_id))

                scored_ids.sort(key=lambda x: x[0], reverse=True)
                top_5_ids = [node_id for score, node_id in scored_ids[:5] if score > 0.1]

                if not top_5_ids:
                    logger.warning("Re-ranker AI returned 0 relevant IDs. Using hybrid fallback.")
                    top_5_ids = [node['Id'] for node in broad_search_nodes[:5]]

                logger.info(f"Re-ranker culled {len(broad_search_nodes)} snippets down to {len(top_5_ids)}.")
                winning_nodes = [vector_db['metadata'][node_id] for node_id in top_5_ids if
                                 node_id in vector_db['metadata']]

            # --- 4. Format and Return ---
            output = f"✅ Found {len(winning_nodes)} highly relevant code snippets for project '{project_id}':\n"
            for node in winning_nodes:
                output += format_node_to_string(node)
            return output

        except Exception as e:
            logger.error(f"Error in code_search_and_rerank: {e}", exc_info=True)
            return f"❌ Error: {str(e)}"


@mcp.tool()
async def code_graph_search(project_id: str = "", node_id: str = "", depth: str = "2") -> str:
    """Finds functions that call (upstream) or are called by (downstream) a specific function ID in a *specific project*."""
    logger.info(f"Executing code_graph_search for project='{project_id}', node_id='{node_id}'")
    if not project_id.strip():
        return f"❌ Error: project_id parameter is required. Available projects: {list(GAME_DATABASES.keys())}"
    if not node_id.strip():
        return "❌ Error: node_id parameter is required."

    project_data = GAME_DATABASES.get(project_id)
    if not project_data:
        return f"❌ Error: Project '{project_id}' not found. Available projects: {list(GAME_DATABASES.keys())}"

    call_graph = project_data["graph"]
    db_metadata = project_data["db"].get("metadata", {})

    try:
        downstream_ids = find_downstream_calls(node_id, call_graph, depth)
        upstream_ids = find_upstream_calls(node_id, call_graph, depth)

        downstream_nodes = [db_metadata[nid] for nid in downstream_ids if nid in db_metadata]
        upstream_nodes = [db_metadata[nid] for nid in upstream_ids if nid in db_metadata]

        if not downstream_nodes and not upstream_nodes:
            return f"🔍 No calls found to or from '{node_id}' (or node not found in DB)."

        output = f"✅ Graph search results for '{node_id}' in project '{project_id}':\n"
        if upstream_nodes:
            output += "\n--- UPSTREAM (Functions that call this) ---\n"
            output += "\n".join([format_node_to_string(node) for node in upstream_nodes])
        if downstream_nodes:
            output += "\n--- DOWNSTREAM (Functions this calls) ---\n"
            output += "\n".join([format_node_to_string(node) for node in downstream_nodes])

        return output

    except Exception as e:
        logger.error(f"Error in code_graph_search: {e}", exc_info=True)
        return f"❌ Error: {str(e)}"


def find_unity_assembly(search_path: str) -> str:
    """Helper to locate Assembly-CSharp.dll or Assembly-UnityScript.dll in a game directory."""
    if not os.path.exists(search_path):
        return None
        
    if os.path.isfile(search_path):
        if search_path.lower().endswith(".dll"):
            return search_path
        return None

    # Walk the directory tree to find the correct assembly
    for root, dirs, files in os.walk(search_path):
        for file in files:
            if file.lower() in ("assembly-csharp.dll", "assembly-unityscript.dll"):
                return os.path.join(root, file)
    return None


@mcp.tool()
async def ingest_new_project(project_id: str = "", source_code_path: str = "", assembly_path: str = "") -> str:
    """Automates the full RAG ingestion pipeline for a new Mono game.
    Can decompile a Mono assembly automatically if assembly_path (DLL path or game directory) is provided.
    
    Arguments:
    - project_id: Unique ID for the project (e.g. 'my_game').
    - source_code_path: Folder where C# files live, or where decompiled files should be saved.
    - assembly_path: Optional path to the game's Assembly-CSharp.dll or the game's installation directory.
    """
    import subprocess

    logger.info(f"Executing ingest_new_project: project='{project_id}', source='{source_code_path}', assembly='{assembly_path}'")

    # --- Validation ---
    if not project_id.strip():
        return "❌ Error: project_id is required (e.g. 'another_crabs_treasure', 'my_game')."
    
    status_parts = []
    
    # --- Step 0: Decompilation (Optional) ---
    if assembly_path.strip():
        resolved_dll = find_unity_assembly(assembly_path)
        if not resolved_dll:
            return f"❌ Error: Could not find 'Assembly-CSharp.dll' or 'Assembly-UnityScript.dll' in assembly path: {assembly_path}"
        
        assembly_path = resolved_dll
        
        # If source_code_path is not specified, default to game modding folder
        if not source_code_path.strip():
            source_code_path = os.path.join(
                os.path.dirname(BASE_DIR),
                f"{project_id}_decompiled"
            )
            
        logger.info(f"Step 0/3: Decompiling {assembly_path} to {source_code_path}...")
        status_parts.append(f"📦 Assembly: {assembly_path}")
        status_parts.append(f"📂 Decompiling to: {source_code_path}")
        
        os.makedirs(source_code_path, exist_ok=True)
        
        try:
            # Unity-safe decompile: avoid `ilspycmd -p` first — ICSharpCode 9.x often
            # stack-overflows on Unity games in DirectBaseTypes / conversion operators.
            # Pass -r Managed so UnityEngine refs resolve.
            managed_dir = os.path.dirname(assembly_path)
            os.makedirs(source_code_path, exist_ok=True)

            strategies = [
                # 1) Nested + Managed refs + C# 9 (no project mode)
                ["ilspycmd", "--disable-updatecheck", "--nested-directories",
                 "-lv", "CSharp9_0", "-r", managed_dir, "-o", source_code_path, assembly_path],
                # 2) Flat + Managed + older language
                ["ilspycmd", "--disable-updatecheck", "-lv", "CSharp7_3",
                 "-r", managed_dir, "-o", source_code_path, assembly_path],
                # 3) Minimal with refs
                ["ilspycmd", "--disable-updatecheck", "-r", managed_dir,
                 "-o", source_code_path, assembly_path],
                # 4) Nested without refs
                ["ilspycmd", "--disable-updatecheck", "--nested-directories",
                 "-lv", "CSharp8_0", "-o", source_code_path, assembly_path],
                # 5) Last resort: project mode (often SO on Unity)
                ["ilspycmd", "--disable-updatecheck", "--nested-directories", "-p",
                 "-r", managed_dir, "-o", source_code_path, assembly_path],
            ]

            last_err = "Unknown error"
            decompile_ok = False
            for cmd in strategies:
                # Wipe partial outputs between attempts
                for root, dirs, files in os.walk(source_code_path, topdown=False):
                    for f in files:
                        try:
                            os.remove(os.path.join(root, f))
                        except OSError:
                            pass
                    for d in dirs:
                        try:
                            os.rmdir(os.path.join(root, d))
                        except OSError:
                            pass

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                # Accept any .cs produced (partial success after SO mid-run)
                has_cs = any(
                    f.endswith(".cs")
                    for _, _, files in os.walk(source_code_path)
                    for f in files
                )
                if has_cs:
                    decompile_ok = True
                    status_parts.append(
                        f"✅ Step 0/3: Decompilation complete via: {' '.join(cmd[1:6])}…"
                    )
                    break
                last_err = (result.stderr or result.stdout or "no .cs output").strip()

            if not decompile_ok:
                return (
                    f"❌ Decompilation failed for Unity assembly (ilspycmd often stack-overflows "
                    f"in project mode).\nLast error:\n{last_err}\n\n"
                    "Try: provide source_code_path with existing decompiled .cs, or decompile "
                    "with dnSpyEx / ilspycmd without -p."
                )
                
        except Exception as e:
            return f"❌ Failed to run ilspycmd: {str(e)}. Please ensure ilspycmd is installed via: dotnet tool install -g ilspycmd"
    else:
        if not source_code_path.strip():
            return "❌ Error: Either source_code_path or assembly_path must be specified."
        if not os.path.isdir(source_code_path):
            return f"❌ Error: source_code_path does not exist or is not a directory: {source_code_path}"

    # Check for .cs files
    cs_files = [f for f in os.listdir(source_code_path) if f.endswith('.cs')]
    if not cs_files:
        # Check subdirectories too
        has_cs = False
        for root, dirs, files in os.walk(source_code_path):
            if any(f.endswith('.cs') for f in files):
                has_cs = True
                break
        if not has_cs:
            return f"❌ Error: No .cs files found in '{source_code_path}' or its subdirectories."

    # --- Paths ---
    roslyn_parser_exe = _find_roslyn_parser_exe()
    project_db_dir = os.path.join(BASE_DIR, "PROJECT_DATABASES", project_id)
    code_graph_output = os.path.join(project_db_dir, "code_graph.json")
    ingest_script = os.path.join(BASE_DIR, "ingest_code_graph.py")

    if not roslyn_parser_exe:
        return (
            "❌ Error: Roslyn parser not found. Build it with:\n"
            "  dotnet build tools/roslyn-parser/RoslynCodeGraph.csproj -c Release\n"
            "Expected: tools/roslyn-parser/bin/Release/net9.0/RoslynCodeGraph.exe"
        )
    if not os.path.isfile(ingest_script):
        return f"❌ Error: Ingestion script not found at: {ingest_script}"

    os.makedirs(project_db_dir, exist_ok=True)

    # --- Step 1: Run Roslyn Parser ---
    logger.info(f"Step 1/3: Running Roslyn parser on '{source_code_path}'...")
    status_parts.append(f"📂 Source: {source_code_path}")

    try:
        result = subprocess.run(
            [roslyn_parser_exe, "--project-path", source_code_path, "--output", code_graph_output],
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            return f"❌ Roslyn parser failed (exit code {result.returncode}):\n{error_msg}"

        if not os.path.isfile(code_graph_output):
            return f"❌ Roslyn parser completed but code_graph.json was not created at: {code_graph_output}"

        graph_size_mb = os.path.getsize(code_graph_output) / (1024 * 1024)
        status_parts.append(f"✅ Step 1/3: Roslyn parser complete - code_graph.json ({graph_size_mb:.1f} MB)")
        logger.info(f"Roslyn parser output: {result.stdout.strip()}")

    except subprocess.TimeoutExpired:
        return "❌ Roslyn parser timed out after 5 minutes. The codebase may be too large."
    except Exception as e:
        return f"❌ Failed to run Roslyn parser: {str(e)}"

    # --- Step 2: Run Python Ingestion ---
    logger.info(f"Step 2/3: Running embedding ingestion for project '{project_id}'...")
    status_parts.append("⏳ Step 2/3: Running embedding ingestion (this may take a few minutes)...")

    try:
        result = subprocess.run(
            [sys.executable, ingest_script,
             "--project_id", project_id,
             "--source_file", code_graph_output],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout for large codebases
            cwd=BASE_DIR
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            return f"❌ Ingestion failed (exit code {result.returncode}):\n{error_msg}\n\n" + "\n".join(status_parts)

        # Check output files exist
        db_path = os.path.join(project_db_dir, "rag_vector_db.json")
        graph_path = os.path.join(project_db_dir, "rag_call_graph.json")

        if not os.path.isfile(db_path) or not os.path.isfile(graph_path):
            return f"❌ Ingestion completed but database files were not created.\n\n" + "\n".join(status_parts)

        db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
        status_parts[-1] = f"✅ Step 2/3: Embedding ingestion complete - vector DB ({db_size_mb:.1f} MB)"

    except subprocess.TimeoutExpired:
        return "❌ Ingestion timed out after 10 minutes. The codebase may be too large."
    except Exception as e:
        return f"❌ Failed to run ingestion: {str(e)}\n\n" + "\n".join(status_parts)

    # --- Step 3: Hot-reload into memory ---
    logger.info(f"Step 3/3: Hot-reloading project '{project_id}' into memory...")

    try:
        db_path = os.path.join(project_db_dir, "rag_vector_db.json")
        graph_path = os.path.join(project_db_dir, "rag_call_graph.json")

        with open(db_path, 'r', encoding='utf-8') as f:
            db_data = json.load(f)
        with open(graph_path, 'r', encoding='utf-8') as f:
            graph_data = json.load(f)

        # Verify the DB model
        db_model = db_data.get("model", "N/A")
        if db_model != EMBEDDING_MODEL:
            return f"❌ Hot-reload failed: DB model mismatch ({db_model} vs {EMBEDDING_MODEL}).\n\n" + "\n".join(status_parts)

        GAME_DATABASES[project_id] = {
            "db": db_data,
            "graph": graph_data
        }

        num_nodes = len(db_data.get("metadata", {}))
        num_edges = len(graph_data) if isinstance(graph_data, list) else 0
        status_parts.append(f"✅ Step 3/3: Project '{project_id}' loaded into memory ({num_nodes} code nodes, {num_edges} call graph edges)")

    except Exception as e:
        status_parts.append(f"⚠️ Step 3/3: Hot-reload failed ({str(e)}). Restart the RAG server to load the project.")

    # --- Final Summary ---
    status_parts.append(f"\n🎉 Project '{project_id}' is now ready! You can search it with code_search_and_rerank and code_graph_search.")
    return "\n".join(status_parts)


# === SERVER STARTUP ===
if __name__ == "__main__":
    logger.info("Starting GameCode RAG MCP server (v12 - MULTI-TENANT)...")
    try:
        log_embedding_config()
    except Exception as e:
        logger.warning("Embedding config: %s", e)

    # Load all project DBs into memory
    load_all_projects()

    if not GAME_DATABASES:
        logger.warning("No projects found in './PROJECT_DATABASES/'. Server will start but tools will fail.")

    ok_embed, embed_err = embeddings_ready()
    if not ok_embed:
        logger.warning("Embeddings not ready: %s", embed_err)
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY is not set — LLM re-rank disabled (hybrid order only).")

    logger.info("Server loaded. Running transport...")
    try:
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"Server runtime error: {e}", exc_info=True)
        sys.exit(1)