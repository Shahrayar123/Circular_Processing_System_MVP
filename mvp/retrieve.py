"""Search the audit test library — BM25 plus vectors, fused.

DEMO SHORTCUT. The real system embeds with BGE-large (1024 dimensions, 1.3 GB of
weights) and stores the vectors in pgvector. Here the "embedding" is a TF-IDF vector
built with numpy, so the demo needs no model download and starts instantly. The search
behaves the same way — keyword and semantic channels, fused — which is what the demo is
meant to show.
"""

import json
import math
import re
from collections import Counter

import numpy as np
from rank_bm25 import BM25Okapi

from . import config, store

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "be", "that", "for",
    "on", "as", "by", "with", "at", "from", "it", "this", "shall", "should", "will",
    "check", "any", "all", "have", "has", "not", "which", "their", "its", "was", "were",
}


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2]


# ====== VECTORS ======

class Vectoriser:
    """A small TF-IDF vectoriser. Stands in for a sentence-transformer."""

    def __init__(self, documents: list[str]):
        tokenised = [tokenize(d) for d in documents]
        df = Counter()
        for tokens in tokenised:
            df.update(set(tokens))
        # keep the most informative vocabulary — enough for a demo, small enough to be fast
        self.vocab = {w: i for i, (w, _) in enumerate(df.most_common(4000))}
        n = len(tokenised) or 1
        self.idf = np.zeros(len(self.vocab), dtype=np.float32)
        for word, index in self.vocab.items():
            self.idf[index] = math.log((n + 1) / (df[word] + 1)) + 1.0

    def encode(self, text: str) -> np.ndarray:
        vector = np.zeros(len(self.vocab), dtype=np.float32)
        tokens = tokenize(text)
        if not tokens:
            return vector
        counts = Counter(tokens)
        for word, count in counts.items():
            index = self.vocab.get(word)
            if index is not None:
                vector[index] = (1.0 + math.log(count)) * self.idf[index]
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector


def document_text(test: dict) -> str:
    """What gets indexed for one test.

    `source_reference` is included deliberately. A superseding clause names a circular,
    not a subject — without the reference in the index, those cases are unreachable.
    """
    return " ".join(str(test.get(f) or "") for f in
                    ("test_description", "exception_description", "strata",
                     "department", "source_reference"))


# ====== INDEX ======

class Index:
    def __init__(self, tests: list[dict]):
        self.tests = tests
        self.documents = [document_text(t) for t in tests]
        self.bm25 = BM25Okapi([tokenize(d) for d in self.documents])
        self.vectoriser = Vectoriser(self.documents)
        stored = [t.get("embedding") for t in tests]
        if all(isinstance(v, list) and v for v in stored):
            self.matrix = np.array(stored, dtype=np.float32)
        else:
            self.matrix = np.vstack([self.vectoriser.encode(d) for d in self.documents])

    def search(self, query: str, top_k: int = None) -> list[dict]:
        """Top candidates by Reciprocal Rank Fusion of the two channels."""
        top_k = top_k or config.TOP_K
        if not query.strip():
            return []

        bm_scores = self.bm25.get_scores(tokenize(query))
        bm_rank = {i: r for r, i in enumerate(np.argsort(bm_scores)[::-1])}

        vector = self.vectoriser.encode(query)
        dense_scores = self.matrix @ vector
        dense_rank = {i: r for r, i in enumerate(np.argsort(dense_scores)[::-1])}

        k = 60.0
        fused = {
            i: 1.0 / (k + bm_rank.get(i, 9999)) + 1.0 / (k + dense_rank.get(i, 9999))
            for i in range(len(self.tests))
        }
        ordered = sorted(fused, key=fused.get, reverse=True)[:top_k]

        results = []
        for i in ordered:
            test = dict(self.tests[i])
            test.pop("embedding", None)
            test["score_bm25"] = round(float(bm_scores[i]), 3)
            test["score_dense"] = round(float(dense_scores[i]), 3)
            test["score_fused"] = round(float(fused[i]), 5)
            results.append(test)
        return results


def build_and_store() -> int:
    """Compute a vector for every test and write it back to the database."""
    tests = store.query("SELECT * FROM audit_tests ORDER BY id")
    documents = [document_text(t) for t in tests]
    vectoriser = Vectoriser(documents)
    with store.connect() as conn:
        for test, text in zip(tests, documents):
            vector = vectoriser.encode(text)
            conn.execute("UPDATE audit_tests SET embedding = ? WHERE id = ?",
                         (json.dumps([round(float(x), 5) for x in vector]), test["id"]))
    return len(tests)


def load_index() -> Index:
    return Index(store.load_library())
