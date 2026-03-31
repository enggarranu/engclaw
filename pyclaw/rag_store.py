from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, List, Tuple

import numpy as np
import requests


class RAGStore:
    def __init__(self, workspace_dir: Path, cfg_rag: dict[str, Any], cfg_integrations: dict[str, Any]):
        self.workspace_dir = workspace_dir
        self.cfg_rag = cfg_rag or {}
        self.cfg_integrations = cfg_integrations or {}
        self._conn = None
        self._index = None
        self._index_map: List[Tuple[str, int]] = []
        self._lock = threading.Lock()

    def _ollama_embeddings_endpoint(self) -> str:
        oll = (self.cfg_integrations.get("ollama") or {})
        ep = str(oll.get("endpoint") or "http://localhost:11434/api/generate")
        if ep.endswith("/api/generate"):
            return ep[:-len("/api/generate")] + "/api/embeddings"
        if ep.endswith("/api"):
            return ep + "/embeddings"
        return "http://localhost:11434/api/embeddings"

    def _connect_tidb(self):
        try:
            import pymysql
        except Exception as e:
            raise RuntimeError("pymysql tidak terpasang. Jalankan: pip install -r requirements.txt") from e
        conf = self.cfg_rag.get("tidb") or {}
        if not (conf.get("host") and conf.get("user") and conf.get("password") and conf.get("database")):
            raise RuntimeError("Konfigurasi TiDB tidak lengkap (host/user/password/database)")
        ssl_conf = conf.get("ssl") or {}
        if not ssl_conf:
            try:
                ca_path = "/etc/ssl/cert.pem"
                from pathlib import Path as _P
                if _P(ca_path).exists():
                    ssl_conf = {"ca": ca_path}
                else:
                    ssl_conf = {"ssl": {}}
            except Exception:
                ssl_conf = {"ssl": {}}
        if not self._conn:
            print(f"[rag] connecting tidb host={conf.get('host')} port={int(conf.get('port', 4000))} user={conf.get('user')} ssl={'on' if ssl_conf else 'off'}")
            self._conn = pymysql.connect(
                host=conf.get("host"),
                port=int(conf.get("port", 4000)),
                user=conf.get("user"),
                password=conf.get("password"),
                database=conf.get("database"),
                charset="utf8mb4",
                autocommit=True,
                ssl=ssl_conf,
            )
            print("[rag] tidb connected")

    def ensure_tables(self):
        if (self.cfg_rag.get("vector_store") or "") != "tidb_cloud":
            return
        self._connect_tidb()
        print("[rag] ensuring tables")
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS docs (
                  doc_id VARCHAR(255) PRIMARY KEY,
                  source_type VARCHAR(32),
                  uri TEXT,
                  hash VARCHAR(64),
                  title VARCHAR(255),
                  metadata JSON,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  doc_id VARCHAR(255),
                  chunk_id INT,
                  dim INT,
                  embedding LONGBLOB,
                  text MEDIUMTEXT,
                  metadata JSON,
                  tokens_len INT,
                  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  INDEX idx_doc_chunk (doc_id, chunk_id),
                  INDEX idx_updated (updated_at)
                ) ENGINE=InnoDB;
                """
            )
        print("[rag] tables ensured")

    def _normalize(self, v: np.ndarray) -> np.ndarray:
        return v / (np.linalg.norm(v) + 1e-9)

    def embed_texts(self, texts: List[str]) -> List[np.ndarray]:
        model = self.cfg_rag.get("embedding_model") or "qwen2-embed"
        ep = self._ollama_embeddings_endpoint()
        print(f"[rag] embeddings endpoint={ep} model={model}")
        out: List[np.ndarray] = []
        for t in texts:
            resp = requests.post(ep, json={"model": model, "input": t}, timeout=60)
            j = {}
            try:
                j = resp.json()
            except Exception:
                raise RuntimeError(f"embeddings response bukan JSON (status={resp.status_code})")
            def _to_vec(obj: Any) -> np.ndarray:
                return np.array(obj, dtype=np.float32)
            if "embedding" in j:
                out.append(_to_vec(j["embedding"]))
                continue
            if "embeddings" in j:
                e = j["embeddings"]
                if isinstance(e, list) and len(e) > 0:
                    item = e[0]
                    if isinstance(item, dict) and "embedding" in item:
                        out.append(_to_vec(item["embedding"]))
                    else:
                        out.append(_to_vec(item))
                    continue
            if j.get("error"):
                fb = "nomic-embed-text"
                if model != fb:
                    r2 = requests.post(ep, json={"model": fb, "input": t}, timeout=60)
                    j2 = {}
                    try:
                        j2 = r2.json()
                    except Exception:
                        raise RuntimeError(f"embeddings fallback gagal (status={r2.status_code})")
                    if "embedding" in j2:
                        out.append(_to_vec(j2["embedding"]))
                        continue
                    if "embeddings" in j2:
                        e2 = j2["embeddings"]
                        if isinstance(e2, list) and len(e2) > 0:
                            item2 = e2[0]
                            if isinstance(item2, dict) and "embedding" in item2:
                                out.append(_to_vec(item2["embedding"]))
                            else:
                                out.append(_to_vec(item2))
                            continue
                raise RuntimeError(f"ollama embeddings error: {j.get('error')}")
            raise RuntimeError("embeddings response tidak memiliki kunci 'embedding' atau 'embeddings'")
        return out

    def chunk_text(self, text: str, size_tokens: int, overlap_tokens: int) -> List[str]:
        words = text.split()
        chunks = []
        i = 0
        while i < len(words):
            chunk = words[i:i + size_tokens]
            chunks.append(" ".join(chunk))
            i += max(1, size_tokens - overlap_tokens)
        return chunks

    def _md5(self, b: bytes) -> str:
        return hashlib.md5(b).hexdigest()

    def _upsert_chunk(self, doc_id: str, chunk_id: int, v: np.ndarray, text: str, metadata: dict[str, Any]) -> None:
        if (self.cfg_rag.get("vector_store") or "") != "tidb_cloud":
            return
        self._connect_tidb()
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM vectors WHERE doc_id=%s AND chunk_id=%s", (doc_id, chunk_id))
            cur.execute(
                "INSERT INTO vectors (doc_id, chunk_id, dim, embedding, text, metadata, tokens_len) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (doc_id, chunk_id, int(v.shape[0]), v.tobytes(), text, json.dumps(metadata or {}), len(text.split()))
            )

    def _ensure_index(self, dim: int):
        import faiss
        if self._index is None:
            self._index = faiss.IndexFlatIP(dim)
            print(f"[rag] faiss index init dim={dim}")

    def index_file(self, path: Path) -> int:
        if not path.exists() or not path.is_file():
            return 0
        text = path.read_text(errors="ignore")
        h = self._md5(text.encode("utf-8"))
        if (self.cfg_rag.get("vector_store") or "") == "tidb_cloud":
            self._connect_tidb()
            with self._conn.cursor() as cur:
                cur.execute("SELECT hash FROM docs WHERE doc_id=%s", (str(path),))
                row = cur.fetchone()
                if row and row[0] == h:
                    return 0
                cur.execute(
                    "REPLACE INTO docs (doc_id, source_type, uri, hash, title, metadata) VALUES (%s,%s,%s,%s,%s,%s)",
                    (str(path), "workspace", str(path), h, path.name, json.dumps({}))
                )
        size = int(self.cfg_rag.get("chunk_size_tokens", 512))
        overlap = int(self.cfg_rag.get("chunk_overlap_tokens", 128))
        chunks = self.chunk_text(text, size, overlap)
        vecs = self.embed_texts(chunks)
        self._ensure_index(vecs[0].shape[0])
        added = 0
        with self._lock:
            for i, v in enumerate(vecs):
                self._upsert_chunk(str(path), i, v, chunks[i], {"range": i})
                self._index.add(np.expand_dims(self._normalize(v), axis=0))
                self._index_map.append((str(path), i))
                added += 1
        return added

    def index_workspace(self) -> int:
        root = self.workspace_dir
        total = 0
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in {".md", ".txt", ".py", ".json"}:
                total += self.index_file(p)
        return total

    def index_readme(self) -> int:
        repo_readme = Path("README.md")
        if repo_readme.exists():
            return self.index_file(repo_readme)
        return 0

    def list_sources(self) -> List[str]:
        src = list(self.cfg_rag.get("sources") or [])
        f = self.workspace_dir / "rag_sources.json"
        if f.exists():
            try:
                extra = json.loads(f.read_text())
                if isinstance(extra, list):
                    src += [str(x) for x in extra]
            except Exception:
                pass
        return sorted(set(src))

    def add_source(self, type_: str, uri: str) -> None:
        f = self.workspace_dir / "rag_sources.json"
        arr: List[Any] = []
        if f.exists():
            try:
                arr = json.loads(f.read_text())
            except Exception:
                arr = []
        arr.append({"type": type_, "uri": uri})
        f.write_text(json.dumps(arr, indent=2))

    def search(self, query: str, top_k: int = 5) -> List[dict[str, Any]]:
        if self._index is None or not self._index_map:
            return []
        q = self.embed_texts([query])[0]
        q = self._normalize(q)
        D, I = self._index.search(np.expand_dims(q, axis=0), max(1, top_k))
        hits: List[dict[str, Any]] = []
        if (self.cfg_rag.get("vector_store") or "") == "tidb_cloud":
            self._connect_tidb()
            with self._conn.cursor() as cur:
                for idx, score in zip(I[0].tolist(), D[0].tolist()):
                    if idx < 0 or idx >= len(self._index_map):
                        continue
                    doc_id, chunk_id = self._index_map[idx]
                    cur.execute("SELECT text, metadata FROM vectors WHERE doc_id=%s AND chunk_id=%s", (doc_id, chunk_id))
                    row = cur.fetchone()
                    if not row:
                        continue
                    hits.append({"doc_id": doc_id, "chunk_id": chunk_id, "text": row[0], "metadata": row[1], "score": float(score)})
        return hits


_RAG_SINGLETON: RAGStore | None = None


def get_rag_store(workspace_dir: Path, cfg_rag: dict[str, Any], cfg_integrations: dict[str, Any]) -> RAGStore:
    global _RAG_SINGLETON
    if _RAG_SINGLETON is None:
        _RAG_SINGLETON = RAGStore(workspace_dir, cfg_rag, cfg_integrations)
        _RAG_SINGLETON.ensure_tables()
    return _RAG_SINGLETON
