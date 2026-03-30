"""
CR-140: Lazy-loaded sentence-transformer embeddings with graceful fallback.

Model: all-MiniLM-L6-v2 (384 dims, ~80 MB, CPU-only, ~10ms per encode)
Storage: raw float32 bytes in SQLite BLOB (384 * 4 = 1,536 bytes per memory)
"""
import logging

import numpy as np

log = logging.getLogger("AIMOS.embeddings")

_model = None       # shared across all agents in the same process
_available = None   # tri-state: None=unchecked, True, False

EMBEDDING_DIM = 384
MODEL_NAME = "all-MiniLM-L6-v2"


def is_available() -> bool:
    global _available
    if _available is None:
        try:
            import sentence_transformers  # noqa: F401
            _available = True
        except ImportError:
            _available = False
            log.info("sentence-transformers not installed — vector search disabled, keyword-only fallback active")
    return _available


def get_model():
    global _model
    if _model is None:
        import os
        os.environ["HF_HUB_OFFLINE"] = "1"  # Don't check HuggingFace for updates
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
        log.info(f"Loaded {MODEL_NAME} embedding model (dim={EMBEDDING_DIM}, offline)")
    return _model


def embed(text: str) -> bytes | None:
    """Return embedding as raw float32 bytes, or None if unavailable."""
    if not is_available() or not text:
        return None
    model = get_model()
    vec = model.encode(text, normalize_embeddings=True)
    return np.array(vec, dtype=np.float32).tobytes()


def cosine_similarity(blob_a: bytes, blob_b: bytes) -> float:
    """Cosine similarity between two pre-normalized embedding blobs (= dot product)."""
    a = np.frombuffer(blob_a, dtype=np.float32)
    b = np.frombuffer(blob_b, dtype=np.float32)
    return float(np.dot(a, b))
