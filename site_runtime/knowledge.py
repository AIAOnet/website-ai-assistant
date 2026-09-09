from __future__ import annotations
from .configuration import setting

import hashlib, json, math, os, re, sqlite3, urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from .rag_settings import RagSettings
from .database_maintenance import Migration, apply_migrations, open_database, require_columns

INJECTION_PATTERNS = re.compile(r"ignore (?:all |any )?(?:previous|prior|system) instructions|system prompt|developer message|act as (?:an? )?|reveal (?:your|the) (?:prompt|instructions)|jailbreak|do not cite", re.I)
TOKEN = re.compile(r"[a-zåäö0-9][a-zåäö0-9_-]{1,}", re.I)
STOPWORDS = {"vilka", "vilken", "vad", "hur", "erbjuder", "finns", "har", "och", "eller", "för", "med", "the", "what", "which", "offer", "offers", "your", "you", "about"}
STOPWORDS |= {"does", "do", "is", "are", "can", "of", "to", "and", "in", "on", "a", "an", "det", "den", "är", "kan", "att", "om", "en", "ett"}
STOPWORDS |= {"for", "with", "our", "us", "we", "their", "it", "its", "at", "by", "from"}


def sentences(content):
    return [part.strip() for part in re.split(r'(?<=[.!?])\s+|\n+', content) if part.strip()]


def passage_score(query, content):
    """Prefer direct topical statements to incidental mentions and headings."""
    focus = [term for term in terms(query) if term not in {
        'rules', 'rule', 'posting', 'regler', 'regel', 'describe', 'explain', 'beskriv'}]
    focus = focus or terms(query)
    definition = bool(re.match(r'\s*(?:what is|what are|vad är)\b', query, re.I))
    best = 0.0
    for sentence in sentences(content):
        words = terms(sentence)
        if not words:
            continue
        score = relevance(focus, [], words)
        # A direct subject statement is stronger than a word buried in a list.
        subject = any(related(term, words[0]) for term in focus)
        if subject and len(sentence.split()) >= 4:
            score += .15
        if definition and subject and re.search(
                r'\b(?:is|are|helps?|provides?|enables?|är|hjälper|erbjuder)\b', sentence, re.I):
            score += .25
        if len(sentence.split()) < 4 and not sentence.endswith(('.', '!', '?')):
            score *= .5
        best = max(best, score)
    return best

# An explicit empty-content notice plus little text is a weak evidence signal.
# A category URL or short page alone is not: both can contain useful answers.
PLACEHOLDER_NOTICE = re.compile(
    r"\b(?:content coming soon|no (?:articles|posts|resources) (?:yet|available)|"
    r"this (?:page|section) is under construction|"
    r"innehåll kommer snart|inga (?:artiklar|inlägg|resurser) (?:ännu|tillgängliga))\b",
    re.I,
)


def page_quality(content: str, query: str) -> float:
    """Demote explicit thin placeholders, never blacklist them or entire sites."""
    if (PLACEHOLDER_NOTICE.search(content) and len(content.split()) <= 80
            and not PLACEHOLDER_NOTICE.search(query)):
        return .45
    return 1.0


def relevance(query_terms, title, body, weights=None):
    """Bounded query coverage; repeated words cannot saturate the score."""
    unique = set(query_terms)
    if not unique:
        return 0.0
    weights = weights or {token: 1.0 for token in unique}
    total = sum(weights[token] for token in unique)
    coverage = sum(weights[token] * (
        .7 * any(related(token, item) for item in body)
        + .3 * any(related(token, item) for item in title)
    ) for token in unique) / total
    pairs = list(zip(query_terms, query_terms[1:]))
    phrase = sum(any(related(a, x) and related(b, y) for x, y in zip(title, title[1:]))
                 for a, b in pairs) / len(pairs) if pairs else 0.0
    return min(1.0, coverage + .1 * phrase)

class KnowledgeValidationError(ValueError): pass

def terms(text: str) -> list[str]:
    return [token.lower() for token in TOKEN.findall(text) if token.lower() not in STOPWORDS]

def related(left: str, right: str) -> bool:
    return left == right or (len(left) >= 5 and len(right) >= 5 and (left in right or right in left))

def content_checksum(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

def validate_record(record: dict) -> dict:
    required = {"source_id", "title", "canonical_url", "category", "language", "retrieved_at", "checksum", "source_status", "content"}
    if not isinstance(record, dict):
        raise KnowledgeValidationError("Source record must be an object")
    missing = required - record.keys()
    if missing: raise KnowledgeValidationError(f"Missing metadata: {', '.join(sorted(missing))}")
    if any(not isinstance(record[key], str) or not record[key].strip() for key in required):
        raise KnowledgeValidationError("Required source metadata must be nonempty text")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", record["source_id"]):
        raise KnowledgeValidationError("Source ID must support source-ID citations")
    url = str(record["canonical_url"])
    parsed = urlsplit(url)
    from website_assistant.site_settings import web_url
    try:
        web_url(url)
    except ValueError as error:
        raise KnowledgeValidationError(str(error)) from error
    if record["language"] not in {"sv", "en"}: raise KnowledgeValidationError("Language must be sv or en")
    if record["source_status"] not in {"active", "archived"}: raise KnowledgeValidationError("Invalid source status")
    content = str(record["content"]).strip()
    if INJECTION_PATTERNS.search(content): raise KnowledgeValidationError(f"Potential prompt injection in {record['source_id']}")
    if record["checksum"] != content_checksum(content): raise KnowledgeValidationError(f"Checksum mismatch in {record['source_id']}")
    clean = dict(record)
    clean.update(id=clean["source_id"], source_url=clean["canonical_url"], retrieval_date=clean["retrieved_at"][:10], version=clean.get("document_version") or "unversioned")
    return clean

class EmbeddingClient:
    def __init__(self, endpoint=None, key=None, model=None, timeout_seconds=None) -> None:
        environment = tuple(setting(name, "").strip() for name in ("WEBSITE_ASSISTANT_EMBEDDING_ENDPOINT", "WEBSITE_ASSISTANT_EMBEDDING_API_KEY", "WEBSITE_ASSISTANT_EMBEDDING_MODEL"))
        self.endpoint, self.key, self.model = environment if endpoint is None and key is None and model is None else (endpoint or "", key or "", model or "")
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else 15.0
        self.usage_budget = None
    @property
    def configured(self) -> bool: return bool(self.endpoint and self.key and self.model)
    def embed(self, inputs: list[str]) -> list[list[float]]:
        if self.usage_budget is not None: self.usage_budget.reserve('embedding', len(inputs))
        request = urllib.request.Request(self.endpoint, data=json.dumps({"model": self.model, "input": inputs}).encode(), headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response: payload = json.loads(response.read())
        return [item["embedding"] for item in sorted(payload["data"], key=lambda item: item["index"])]

class EmbeddingCache:
    """Persistent source vectors. Query vectors are deliberately never retained."""
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        with self._connect() as db:
            def schema_v1(database):
                database.execute("""CREATE TABLE IF NOT EXISTS embedding_vectors (
                model TEXT NOT NULL,
                source_id TEXT NOT NULL,
                checksum TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK (dimensions > 0),
                vector TEXT NOT NULL,
                PRIMARY KEY(model, source_id, checksum)
            )""")
            apply_migrations(db, "embedding_cache", (
                Migration(1, "create embedding vectors", schema_v1),))
            require_columns(db, "embedding_vectors", {"model", "source_id", "checksum",
                                                       "dimensions", "vector"})

    @contextmanager
    def _connect(self):
        connection = open_database(self.path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _valid(vector) -> bool:
        return (isinstance(vector, list) and bool(vector) and
                all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) for value in vector) and
                any(value != 0 for value in vector))

    def get(self, model: str, records: list[dict]) -> dict[str, list[float]]:
        if not model or not records:
            return {}
        expected = {(item.get("chunk_id", item["source_id"]), item["checksum"]) for item in records}
        placeholders = ",".join("?" for _ in records)
        with self.lock, self._connect() as db:
            rows = db.execute(
                f"SELECT source_id, checksum, dimensions, vector FROM embedding_vectors "
                f"WHERE model=? AND source_id IN ({placeholders})",
                (model, *(item.get("chunk_id", item["source_id"]) for item in records)),
            ).fetchall()
        found = {}
        for source_id, checksum, dimensions, payload in rows:
            if (source_id, checksum) not in expected:
                continue
            try:
                vector = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if self._valid(vector) and len(vector) == dimensions:
                found[source_id] = vector
        return found

    def put(self, model: str, records: list[dict], vectors: list[list[float]]) -> None:
        if not model or not records:
            return
        rows = [(model, record.get("chunk_id", record["source_id"]), record["checksum"], len(vector),
                 json.dumps(vector, allow_nan=False, separators=(",", ":")))
                for record, vector in zip(records, vectors) if self._valid(vector)]
        with self.lock, self._connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO embedding_vectors"
                "(model, source_id, checksum, dimensions, vector) VALUES(?, ?, ?, ?, ?)", rows
            )

    def replace(self, model: str, records: list[dict], vectors: list[list[float]]) -> None:
        if (not model or not records or len(records) != len(vectors) or
                any(not self._valid(vector) for vector in vectors) or
                len({len(vector) for vector in vectors}) != 1):
            raise ValueError("Complete valid embedding vectors are required")
        rows = [(model, record.get("chunk_id", record["source_id"]), record["checksum"],
                 len(vector), json.dumps(vector, allow_nan=False, separators=(",", ":")))
                for record, vector in zip(records, vectors)]
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM embedding_vectors")
            db.executemany(
                "INSERT INTO embedding_vectors"
                "(model, source_id, checksum, dimensions, vector) VALUES(?, ?, ?, ?, ?)", rows
            )

    def prune(self, model: str, records: list[dict]) -> None:
        if not model:
            return
        active = {(item.get("chunk_id", item["source_id"]), item["checksum"]) for item in records}
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM embedding_vectors WHERE model<>?", (model,))
            rows = db.execute(
                "SELECT source_id, checksum FROM embedding_vectors WHERE model=?", (model,)
            ).fetchall()
            db.executemany(
                "DELETE FROM embedding_vectors WHERE model=? AND source_id=? AND checksum=?",
                [(model, source_id, checksum) for source_id, checksum in rows
                 if (source_id, checksum) not in active],
            )

    def status(self, model: str) -> dict:
        if not model:
            return {"vectors_cached": 0, "dimensions": None, "persistent": True}
        with self.lock, self._connect() as db:
            count, minimum, maximum = db.execute(
                "SELECT COUNT(*), MIN(dimensions), MAX(dimensions) "
                "FROM embedding_vectors WHERE model=?", (model,)
            ).fetchone()
        return {"vectors_cached": count,
                "dimensions": minimum if minimum == maximum else None,
                "persistent": True}

class UnavailableEmbeddingCache:
    """No-op boundary that keeps semantic retrieval usable when local storage fails."""
    def get(self, model: str, records: list[dict]) -> dict[str, list[float]]: return {}
    def put(self, model: str, records: list[dict], vectors: list[list[float]]) -> None: pass
    def replace(self, model: str, records: list[dict], vectors: list[list[float]]) -> None:
        raise OSError("Embedding cache is unavailable")
    def prune(self, model: str, records: list[dict]) -> None: pass
    def status(self, model: str) -> dict:
        return {"vectors_cached": 0, "dimensions": None, "persistent": False}

def cosine(left: list[float], right: list[float]) -> float:
    denominator = math.sqrt(sum(x*x for x in left)) * math.sqrt(sum(x*x for x in right))
    return sum(x*y for x, y in zip(left, right)) / denominator if denominator else 0.0

class KnowledgeStore:
    def __init__(self, source_path: str | Path, *, use_index=True) -> None:
        self.source_path = Path(source_path)
        self.index_path = self.source_path.parent / "knowledge" / "index.json"
        self.settings_path = self.source_path.parent / "rag_settings.json"
        self.settings = RagSettings()
        self.settings_warning = None
        if self.settings_path.exists():
            try:
                self.settings = RagSettings.model_validate(json.loads(self.settings_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                self.settings_warning = "Invalid saved settings; safe defaults are active. Save valid settings to repair."
        payload = json.loads(self.source_path.read_text(encoding="utf-8")); records = payload.get("records", payload) if isinstance(payload, dict) else payload
        self.source_records = [validate_record(item) for item in records]
        if len({r["source_id"] for r in self.source_records}) != len(self.source_records):
            raise KnowledgeValidationError("Duplicate source IDs")
        self.records = [item for item in self.source_records if item["source_status"] == "active"]
        from .chunking import build_chunks
        self.chunks = build_chunks(self.records, self.source_path.parent)
        self.index_status, self.index_generated_at = "not_built", None
        if use_index and self.index_path.exists():
            try:
                cached = json.loads(self.index_path.read_text(encoding="utf-8"))
                indexed = [validate_record(item) for item in cached["records"]]
                indexed = [r for r in indexed if r["source_status"] == "active"]
                if indexed != self.records:
                    raise KnowledgeValidationError("Index differs from approved snapshot")
                self.records = indexed
                self.chunks = build_chunks(self.records, self.source_path.parent)
                if "chunks" in cached and cached["chunks"] != self.chunks:
                    raise KnowledgeValidationError("Chunk index differs from approved documents")
                self.index_status, self.index_generated_at = "ready", cached.get("generated_at")
            except (OSError, ValueError, TypeError, KeyError):
                self.index_status = "stale_or_invalid"
        self.approved_urls = {item["canonical_url"] for item in self.records}
        self.embedding_client = EmbeddingClient()
        try:
            self.embedding_cache = EmbeddingCache(self.source_path.parent / "embedding_cache.db")
        except (OSError, sqlite3.Error):
            self.embedding_cache = UnavailableEmbeddingCache()
        if self.embedding_client.configured:
            try:
                self.embedding_cache.prune(self.embedding_client.model, self.chunks)
            except sqlite3.Error:
                pass

    def search(self, query: str, language: str = "sv", category: str | None = None, limit: int | None = None) -> list[dict]:
        return self.search_details(query, language, category, limit)["records"]

    def search_details(self, query: str, language: str = "sv", category: str | None = None, limit: int | None = None, *, ontology_source_ids=(), ontology_source_scores=None) -> dict:
        settings = self.settings
        details = {"records": [], "confidence": "none", "semantic_enabled": self.embedding_client.configured,
                   "semantic_used": False, "search_mode": "lexical", "fallback_reason": None,
                   "settings": settings.model_dump(), "minimum_score": settings.minimum_score}
        query_terms = terms(query)
        if not query_terms: return details
        candidates = [item for item in self.chunks if item["language"] == language and (not category or item["category"] == category)]
        semantic_scores = {}
        if settings.use_semantic and settings.lexical_weight < 1 and not self.embedding_client.configured:
            details["fallback_reason"] = "embedding_not_configured"
        if settings.use_semantic and settings.lexical_weight < 1 and self.embedding_client.configured and candidates:
            try:
                cached = self.embedding_cache.get(self.embedding_client.model, candidates)
                missing = [item for item in candidates if item["chunk_id"] not in cached]
                vectors = self.embedding_client.embed(
                    [query] + [item["title"] + "\n" + item["content"] for item in missing]
                )
                if len(vectors) != len(missing) + 1 or not vectors[0] or any(
                    len(v) != len(vectors[0]) or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in v)
                    or not any(x != 0 for x in v) for v in vectors
                ):
                    raise ValueError("Invalid embedding vectors")
                fresh = dict(zip((item["chunk_id"] for item in missing), vectors[1:]))
                source_vectors = {**cached, **fresh}
                if len(source_vectors) != len(candidates) or any(
                    len(vector) != len(vectors[0]) for vector in source_vectors.values()
                ):
                    raise ValueError("Invalid cached embedding vectors")
                try:
                    self.embedding_cache.put(self.embedding_client.model, missing, vectors[1:])
                except (OSError, sqlite3.Error, ValueError, TypeError):
                    pass
                semantic_scores = {item["chunk_id"]: max(0.0, cosine(vectors[0], source_vectors[item["chunk_id"]])) for item in candidates}
                details.update(semantic_used=True, search_mode="hybrid")
            except Exception:
                semantic_scores = {}
                details["fallback_reason"] = "embedding_unavailable"
        ontology_source_scores = ontology_source_scores or {source_id: .6 for source_id in ontology_source_ids}
        documents = [set(terms(r["title"] + " " + r["content"])) for r in self.records
                     if r["language"] == language and (not category or r["category"] == category)]
        weights = {token: 1 + math.log((1 + len(documents)) / (1 + sum(
            any(related(token, item) for item in document) for document in documents)))
            for token in set(query_terms)}
        scored = []
        quality = {record["source_id"]: page_quality(record["content"], query)
                   for record in self.records}
        for record in candidates:
            title, body = terms(record["title"]), terms(record["content"])
            lexical_norm = relevance(query_terms, title, body, weights)
            semantic = semantic_scores.get(record["chunk_id"], 0.0); combined = lexical_norm if not semantic_scores else settings.lexical_weight * lexical_norm + (1 - settings.lexical_weight) * semantic
            ontology_score = ontology_source_scores.get(record["source_id"], 0.0)
            combined = max(combined, ontology_score)
            quality_factor = quality[record["source_id"]]
            combined *= quality_factor
            if combined > 0 and round(combined, 4) >= settings.minimum_score:
                result = dict(record); result.update(score=round(combined, 4), lexical_score=round(lexical_norm, 4), semantic_score=round(semantic, 4), ontology_score=ontology_score, page_quality_factor=quality_factor, confidence="high" if combined >= .7 else "medium" if combined >= .35 else "low"); scored.append(result)
        deduplicated = {}
        for item in sorted(scored, key=lambda value: (-value["score"], -value["lexical_score"], -value["ontology_score"], value["source_id"], value["chunk_id"])):
            item['passage_score'] = round(passage_score(query, item['content']), 4)
            previous = deduplicated.get(item['canonical_url'])
            if previous is None or item['passage_score'] > previous['passage_score']:
                deduplicated[item['canonical_url']] = item
        cap = settings.result_limit if limit is None else min(settings.result_limit, max(1, limit))
        details["records"] = list(deduplicated.values())[:cap]
        details["confidence"] = details["records"][0]["confidence"] if details["records"] else "none"
        return details

    def category(self, category: str, language: str) -> list[dict]:
        return [dict(item) for item in self.records if item["category"] == category and item["language"] == language]
