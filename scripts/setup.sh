#!/bin/bash
# Setup script for ERC32 agent
# Run after: pip install -r requirements.txt

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "=== ERC32 Agent Setup ==="

# Pre-download embedding model for wiki search
echo "1. Downloading embedding model (BAAI/bge-small-en-v1.5)..."
python -c "from txtai import Embeddings; Embeddings({'path': 'BAAI/bge-small-en-v1.5'}); print('   Model cached successfully')"

# Build wiki indexes if not exist
echo "2. Building wiki indexes..."
python -c "
from infra.wiki_rag import index_all_wikis, INDEX_ROOT
if not INDEX_ROOT.exists() or not any(INDEX_ROOT.iterdir()):
    result = index_all_wikis()
    print(f'   Indexed {result[\"indexed\"]} wikis')
else:
    print('   Indexes already exist, skipping')
"

echo "=== Setup complete ==="
