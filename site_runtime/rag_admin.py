"""Admin operations over the same knowledge store used by public chat."""
import sqlite3
import threading
from datetime import datetime, timezone

from .knowledge import EmbeddingCache, KnowledgeStore, KnowledgeValidationError
from .rag_settings import RagSettings, atomic_json


class EmbeddingRebuildBusy(RuntimeError): pass
class EmbeddingRebuildConflict(RuntimeError): pass


class RagAdmin:
    def __init__(self, tools):
        self.tools = tools
        self.lock = threading.RLock()
        self.embedding_rebuild_lock = threading.Lock()

    def embedding_rebuild_preview(self):
        with self.lock:
            store = self.tools.knowledge
            chunks = list(store.chunks)
            try:
                matching = len(store.embedding_cache.get(store.embedding_client.model, chunks))
                persistent = store.embedding_cache.status(store.embedding_client.model)["persistent"]
            except (OSError, sqlite3.Error, ValueError, TypeError):
                matching, persistent = 0, False
            return {"configured": store.embedding_client.configured,
                    "model": store.embedding_client.model or None,
                    "chunk_count": len(chunks), "matching_vectors": matching,
                    "vectors_to_generate": max(0, len(chunks) - matching),
                    "persistent": persistent, "running": self.embedding_rebuild_lock.locked()}

    def rebuild_embeddings(self, expected_model, expected_chunk_count):
        if not self.embedding_rebuild_lock.acquire(blocking=False):
            raise EmbeddingRebuildBusy("Embedding rebuild is already running")
        try:
            with self.lock:
                store = self.tools.knowledge
                client, cache, chunks = store.embedding_client, store.embedding_cache, list(store.chunks)
                signature = [(item["chunk_id"], item["checksum"]) for item in chunks]
                if (not client.configured or client.model != expected_model or
                        len(chunks) != expected_chunk_count or not chunks):
                    raise EmbeddingRebuildConflict("Embedding configuration or chunks changed. Refresh the preview.")
            vectors, batch_count = [], 0
            for start in range(0, len(chunks), 32):
                batch = chunks[start:start + 32]
                produced = client.embed([item["title"] + "\n" + item["content"] for item in batch])
                if (len(produced) != len(batch) or any(not EmbeddingCache._valid(vector) for vector in produced)
                        or len({len(vector) for vector in [*vectors, *produced]}) > 1):
                    raise ValueError("Embedding provider returned invalid vectors")
                vectors.extend(produced); batch_count += 1
            with self.lock:
                current = self.tools.knowledge
                if (current.embedding_client.model != expected_model or
                        [(item["chunk_id"], item["checksum"]) for item in current.chunks] != signature):
                    raise EmbeddingRebuildConflict("Embedding configuration or chunks changed during rebuild.")
                cache.replace(expected_model, chunks, vectors)
            return {"outcome": "complete", "model": expected_model,
                    "chunk_count": len(chunks), "dimensions": len(vectors[0]),
                    "batch_count": batch_count}
        finally:
            self.embedding_rebuild_lock.release()

    def status(self):
        store = self.tools.knowledge
        try:
            cache = store.embedding_cache.status(store.embedding_client.model)
        except (OSError, sqlite3.Error):
            cache = {"vectors_cached": 0, "dimensions": None, "persistent": False}
        return {"settings": store.settings.model_dump(), "settings_warning": store.settings_warning,
                "embedding": {"configured": store.embedding_client.configured,
                              "model": store.embedding_client.model or None,
                              **cache},
                "index": {"status": store.index_status, "generated_at": store.index_generated_at,
                          "active_records": len(store.records)},
                "sources": [{key: record.get(key) for key in (
                    "source_id", "title", "canonical_url", "category", "language", "source_status",
                    "retrieved_at", "checksum", "document_version", "content"
                )} for record in store.source_records]}

    def save(self, settings: RagSettings):
        with self.lock:
            store = self.tools.knowledge
            atomic_json(store.settings_path, settings.model_dump())
            store.settings = settings
            store.settings_warning = None
        return self.status()

    def rebuild(self):
        # Validate a fresh snapshot before writing or changing live references.
        with self.lock:
            previous = self.tools.knowledge
            candidate = KnowledgeStore(previous.source_path, use_index=False)
            if not candidate.records:
                raise KnowledgeValidationError("The approved snapshot has no active sources")
            for contact in self.tools.contacts:
                if contact.get("active") and contact["official_source_url"] not in candidate.approved_urls:
                    raise KnowledgeValidationError("An active contact would lose its approved source")
            candidate.settings = previous.settings
            candidate.settings_warning = previous.settings_warning
            candidate.embedding_client = previous.embedding_client
            candidate.embedding_cache = previous.embedding_cache
            generated_at = datetime.now(timezone.utc).isoformat()
            atomic_json(candidate.index_path, {"generated_at": generated_at,
                                               "records": candidate.records,
                                               "chunks": candidate.chunks})
            candidate.index_status, candidate.index_generated_at = "ready", generated_at
            if candidate.embedding_client.configured:
                try:
                    candidate.embedding_cache.prune(candidate.embedding_client.model, candidate.chunks)
                except (OSError, sqlite3.Error):
                    pass
            self.tools.knowledge = candidate
        return self.status()
