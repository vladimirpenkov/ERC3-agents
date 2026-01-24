"""Wiki RAG: indexing and search for company wiki."""

from pathlib import Path
from txtai import Embeddings

# Constants
WIKI_ROOT = Path(__file__).parent.parent / "wiki" / "companies"
INDEX_ROOT = Path(__file__).parent.parent / "wiki" / "companies" / "indexes"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
MIN_CHUNK_LENGTH = 40

# Cache for loaded indexes
_cache: dict[str, Embeddings] = {}


def index_wiki(wiki_sha1: str, section_delimiter: str = "##", rebuild: bool = False) -> int:
    """
    Index wiki/<wiki_sha1>/**/*.md → indexes/<wiki_sha1>/
    Returns number of indexed chunks.
    """
    wiki_dir = WIKI_ROOT / wiki_sha1
    index_dir = INDEX_ROOT / wiki_sha1

    if not wiki_dir.is_dir():
        raise ValueError(f"Wiki not found: {wiki_dir}")

    if rebuild and index_dir.exists():
        import shutil
        shutil.rmtree(index_dir)

    index_dir.mkdir(parents=True, exist_ok=True)

    # Collect documents
    rows = []
    doc_id = 0

    for md_file in sorted(wiki_dir.rglob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        rel_path = str(md_file.relative_to(wiki_dir))
        print(f"  {rel_path} ...", end=" ", flush=True)

        # Split by sections
        chunks = 0
        for section_title, body in _split_sections(text, section_delimiter):
            if len(body) >= MIN_CHUNK_LENGTH:
                rows.append((str(doc_id), {
                    "text": body,
                    "file_path": rel_path,
                    "section_title": section_title or "",
                }))
                doc_id += 1
                chunks += 1
        print(f"done ({chunks} chunks)")

    if not rows:
        return 0

    # Index
    emb = Embeddings({"path": EMBEDDING_MODEL, "content": True, "hybrid": True})
    emb.index(rows)
    emb.save(str(index_dir))
    emb.close()

    # Invalidate cache
    _cache.pop(wiki_sha1, None)

    return len(rows)


def index_all_wikis(rebuild: bool = False, only_missing: bool = False) -> dict:
    """
    Index all wikis in WIKI_ROOT (except indexes/).
    Returns: {"processed": int, "indexed": int}
    """
    if not WIKI_ROOT.is_dir():
        return {"processed": 0, "indexed": 0}

    wiki_dirs = sorted([
        p for p in WIKI_ROOT.iterdir()
        if p.is_dir() and p.name != "indexes"
    ])

    processed = indexed = 0
    for wiki_dir in wiki_dirs:
        wiki_sha = wiki_dir.name
        processed += 1

        if only_missing and (INDEX_ROOT / wiki_sha).is_dir():
            continue

        n = index_wiki(wiki_sha, rebuild=rebuild)
        if n > 0:
            indexed += 1

    return {"processed": processed, "indexed": indexed}


def search(wiki_sha1: str, query: str, top_k: int = 5) -> list[dict]:
    """
    Search wiki index.
    Returns list of results: [{score, file_path, section_title, text}, ...]
    """
    if wiki_sha1 not in _cache:
        index_dir = INDEX_ROOT / wiki_sha1
        if not index_dir.exists():
            raise ValueError(f"Index not found: {index_dir}. Run index_wiki() first.")
        emb = Embeddings()
        emb.load(str(index_dir))
        _cache[wiki_sha1] = emb

    # SQL-style query to get all metadata fields
    # Escape double quotes in query to prevent SQL injection
    escaped_query = query.replace('"', '\\"')
    sql = f'SELECT id, text, file_path, section_title, score FROM txtai WHERE similar("{escaped_query}") LIMIT {top_k}'
    return _cache[wiki_sha1].search(sql)


def _split_sections(text: str, delimiter: str) -> list[tuple[str | None, str]]:
    """Split markdown into sections by delimiter (e.g. '##')."""
    # If text doesn't start with delimiter - add virtual one
    if not text.lstrip().startswith(delimiter):
        text = f"{delimiter} Introduction\n{text}"

    sections = []
    current_title = None
    current_lines = []

    for line in text.splitlines():
        if line.startswith(delimiter):
            # Save previous section
            if current_lines:
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = line[len(delimiter):].strip(" #\t")
            current_lines = []
        else:
            current_lines.append(line)

    # Last section
    if current_lines:
        sections.append((current_title, "\n".join(current_lines).strip()))

    return sections
