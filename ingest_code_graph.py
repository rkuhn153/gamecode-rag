import os
import sys
import logging
import json
import httpx
import asyncio
from dotenv import load_dotenv
import copy
import re
import argparse  # New import for CLI arguments

# ---
# Path setup (Absolute paths are crucial)
# ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=dotenv_path)

# ---
# Configuration
# ---
# Must match gamecode_rag_server.EMBEDDING_MODEL (or re-ingest after changing).
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "openai/text-embedding-3-small")
OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# 1 token ~ 4 chars. Model limit is 8192 tokens (~32k chars).
CONTENT_CHAR_LIMIT = 18000
BATCH_CHAR_LIMIT = 20000

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("gamecode-rag-ingestor")

# ---
# Regex for finding class context (Unchanged)
# ---
RE_USINGS = re.compile(r"^\s*using\s+[\w\.]+;", re.MULTILINE)
RE_CLASS_VARS = re.compile(
    r"^\s*(public|private|protected|internal)\s+(static\s+)?(readonly\s+)?[\w\<\>\[\]]+\s+[\w]+\s*(\{.*?\s*get;.*?\})?\;",
    re.MULTILINE)


# ---
# Real Embedding Function (Unchanged)
# ---
async def get_real_embeddings_batch(client, texts: list):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"model": EMBEDDING_MODEL, "input": texts}

    try:
        response = await client.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60.0)
        response.raise_for_status()
        data = response.json()

        if "data" in data and data["data"]:
            return [item['embedding'] for item in data['data']]
        else:
            logger.error(f"API returned 200 OK but no 'data' key. Response: {data}")
            return None

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP Error: {e.response.status_code} {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Error getting embeddings: {e}", exc_info=True)
        return None


# ---
# Main Ingestion Logic
# ---
async def main(project_id, source_file_path):
    if not OPENROUTER_API_KEY:
        logger.error("FATAL: OPENROUTER_API_KEY not found in .env file.")
        sys.exit(1)

    # --- 1. Load the C# tool's output ---
    try:
        logger.info(f"Loading source code graph for project '{project_id}' from: {source_file_path}")
        with open(source_file_path, 'r', encoding='utf-8') as f:
            code_graph = json.load(f)
    except FileNotFoundError:
        logger.error(f"FATAL: Source file not found: '{source_file_path}'")
        logger.error("Please ensure the path is correct and the C# RoslynParser tool has been run.")
        sys.exit(1)

    nodes = code_graph.get("Nodes", [])
    if not nodes:
        logger.error("FATAL: Source file has no 'Nodes'.")
        sys.exit(1)

    # --- 2. Build the new "brains" ---
    new_vector_db = {
        "model": EMBEDDING_MODEL,
        "vectors": {},
        "metadata": {}
    }
    new_call_graph = code_graph.get("Edges", [])

    # --- 3. Pre-processing and CONTEXT-AWARE Chunking (Unchanged) ---
    logger.info("Pre-processing nodes and chunking large files...")

    nodes_to_process = []
    total_skipped_for_content = 0
    total_chunked = 0

    for node in nodes:
        node_id = node.get("Id")
        node_content = node.get("Content", "").strip()

        if not node_id or not node_content:
            total_skipped_for_content += 1
            continue

        node_chars = len(node_content)

        if node_chars > BATCH_CHAR_LIMIT and "class" in node.get("Type", ""):
            logger.warning(f"CONTEXT-AWARE chunking 'monster' node {node_id} ({node_chars} chars)...")
            total_chunked += 1

            usings = RE_USINGS.findall(node_content)
            class_vars = RE_CLASS_VARS.findall(node_content)

            context_header = "/* --- CONTEXT HEADER (Class Variables) --- */\n"
            context_header += "\n".join(usings) + "\n\n"
            context_header += "\n".join([match[0] for match in class_vars]) + "\n"
            context_header += "/* --- END OF HEADER --- */\n\n"

            content_body = node_content

            for i in range(0, node_chars, CONTENT_CHAR_LIMIT):
                content_chunk = content_body[i:i + CONTENT_CHAR_LIMIT]
                final_chunk_content = context_header + content_chunk

                chunk_node = copy.deepcopy(node)
                chunk_node["Id"] = f"{node_id}_chunk_{i // CONTENT_CHAR_LIMIT + 1}"
                chunk_node["Content"] = final_chunk_content
                chunk_node["Type"] = f"{node.get('Type', 'unknown')}_chunk"

                nodes_to_process.append(chunk_node)

        elif node_chars > BATCH_CHAR_LIMIT:
            logger.warning(
                f"Skipping monster node {node_id} ({node_chars} chars) because it is not a class and cannot be smart-chunked.")
            total_skipped_for_content += 1
        else:
            nodes_to_process.append(node)

    logger.info(f"Original nodes: {len(nodes)}")
    logger.info(f"Skipped (no content or too large/not-class): {total_skipped_for_content}")
    logger.info(f"Monster class nodes chunked: {total_chunked}")
    logger.info(f"Total nodes to process (after chunking): {len(nodes_to_process)}")

    # --- 4. Process nodes in batches (Unchanged) ---
    total_processed = 0
    total_failed = 0

    current_batch_nodes = []
    current_batch_chars = 0
    batch_num = 1

    async with httpx.AsyncClient(timeout=60.0) as client:

        async def process_batch(batch_nodes_list, batch_num):
            nonlocal total_processed, total_failed

            logger.info(
                f"Processing batch {batch_num} (Size: {len(batch_nodes_list)} nodes, {current_batch_chars} chars)...")
            batch_texts = [n.get("Content", "") for n in batch_nodes_list]
            embeddings = await get_real_embeddings_batch(client, batch_texts)

            if embeddings and len(embeddings) == len(batch_nodes_list):
                for n, vector in zip(batch_nodes_list, embeddings):
                    new_vector_db["vectors"][n["Id"]] = vector
                    new_vector_db["metadata"][n["Id"]] = n
                    total_processed += 1
                return True
            else:
                logger.error(f"Failed to process batch {batch_num}. Skipping.")
                total_failed += len(batch_nodes_list)
                return False

        for node in nodes_to_process:
            node_content = node.get("Content", "")
            node_chars = len(node_content)

            if current_batch_chars + node_chars > BATCH_CHAR_LIMIT and current_batch_nodes:
                await process_batch(current_batch_nodes, batch_num)
                await asyncio.sleep(1)  # Be nice

                current_batch_nodes = [node]
                current_batch_chars = node_chars
                batch_num += 1
            else:
                current_batch_nodes.append(node)
                current_batch_chars += node_chars

        if current_batch_nodes:
            await process_batch(current_batch_nodes, batch_num)

    logger.info(f"Successfully processed and embedded {total_processed} / {len(nodes_to_process)} nodes.")
    if total_failed > 0:
        logger.warning(f"{total_failed} nodes FAILED to process. Check error logs above.")

    # --- 5. Save the new "brains" to the structured directory ---
    output_dir = os.path.join(BASE_DIR, "PROJECT_DATABASES", project_id)
    os.makedirs(output_dir, exist_ok=True)

    db_path = os.path.join(output_dir, "rag_vector_db.json")
    graph_path = os.path.join(output_dir, "rag_call_graph.json")

    try:
        logger.info(f"Saving new Vector DB to {db_path}...")
        with open(db_path, 'w', encoding='utf-8') as f:
            json.dump(new_vector_db, f)

        logger.info(f"Saving Call Graph to {graph_path}...")
        with open(graph_path, 'w', encoding='utf-8') as f:
            json.dump(new_call_graph, f)

    except Exception as e:
        logger.error(f"Error saving database files: {e}", exc_info=True)
        sys.exit(1)

    logger.info("\n--- INGESTION COMPLETE ---")
    logger.info(f"Project '{project_id}' is now indexed and ready for the server.")


if __name__ == "__main__":
    # --- NEW: Argparse logic ---
    parser = argparse.ArgumentParser(description="GameCode RAG Ingestor v12")
    parser.add_argument("--project_id", type=str, required=True,
                        help="A unique ID for this project (e.g., 'another_crabs_treasure')")
    parser.add_argument("--source_file", type=str, required=True,
                        help="The FULL, absolute path to the 'code_graph.json' file generated by the C# RoslynParser.")

    args = parser.parse_args()

    asyncio.run(main(args.project_id, args.source_file))