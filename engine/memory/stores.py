# memory/stores.py —— 情景/语义记忆（JSON 文件存储即可，chromadb 可选导入）
# 铁律：记忆/反思/蒸馏走异步队列；检索 top-k 注入决策。本 MVP 用 JSON 文件 + 关键词检索兜底。

import os
import json
import time

try:
    import chromadb  # type: ignore
    _HAS_CHROMADB = True
except ImportError:
    _HAS_CHROMADB = False


class MemoryStore:
    def __init__(self, data_dir: str = "memory_data", use_vector: bool = False):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.episodic_path = os.path.join(data_dir, "episodic.json")
        self.semantic_path = os.path.join(data_dir, "semantic.json")
        self.episodic: list = self._load(self.episodic_path)
        self.semantic: list = self._load(self.semantic_path)
        self.use_vector = use_vector and _HAS_CHROMADB
        self._client = None
        if self.use_vector:
            try:
                self._client = chromadb.Client()
            except Exception:
                self.use_vector = False

    def _load(self, p: str) -> list:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self, p: str, data: list):
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add_episodic(self, entry: dict):
        entry.setdefault("t", time.time())
        self.episodic.append(entry)
        self._save(self.episodic_path, self.episodic)

    def add_semantic(self, entry: dict):
        entry.setdefault("t", time.time())
        self.semantic.append(entry)
        self._save(self.semantic_path, self.semantic)

    def search(self, query: str, top_k: int = 5) -> list:
        """关键词+子串检索（无 embedding 兜底）。返回情景+语义混合条目。"""
        q = (query or "").lower()
        pool = [("episodic", e) for e in self.episodic] + [("semantic", s) for s in self.semantic]
        scored = []
        for kind, item in pool:
            blob = json.dumps(item, ensure_ascii=False).lower()
            score = blob.count(q) if q else 0
            if score > 0:
                scored.append((score, kind, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"kind": k, "item": it} for _, k, it in scored[:top_k]]

    def stats(self) -> dict:
        return {"episodic": len(self.episodic), "semantic": len(self.semantic),
                "vector_db": "chromadb" if self.use_vector else "json"}
