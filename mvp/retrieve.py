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
    """Lower-case word tokens for BM25 and the TF-IDF vectoriser. Shared by both so the
    two channels index the same text.
    """
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2]


# ====== VECTORS ======

class Vectoriser:
    """A small TF-IDF vectoriser. Stands in for a sentence-transformer."""

    def __init__(self, documents: list[str]):
        tokenised = [tokenize(d) for d in documents]
        df = Counter()
        for tokens in tokenised:
            df.update(set(tokens))
        # Keep the most informative vocabulary — enough for a demo, small enough to be
        # fast — and SORT IT DETERMINISTICALLY.
        #
        # `df.most_common()` breaks ties by insertion order, and insertion order here comes
        # from iterating a set of strings, whose order depends on PYTHONHASHSEED — which
        # Python randomises for every process. Words with equal document frequency
        # therefore swapped indices between runs, so a vector built by `build_and_store()`
        # in one process and a query encoded in another were using different word->index
        # maps. Rare dimensions silently misaligned, cosine scores moved, and decisions
        # near a threshold flipped: the same library and the same circular produced 65
        # proposals on one run and 62 on the next.
        #
        # `(-count, word)` makes the order a pure function of the corpus. Do not replace
        # it with most_common() again.
        ranked = sorted(df.items(), key=lambda item: (-item[1], item[0]))[:4000]
        self.vocab = {w: i for i, (w, _) in enumerate(ranked)}
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
    """The searchable audit library — BM25 and vectors over the same document text.

    Built once per run and held in memory. The library is the index and a clause is the
    query, which is the opposite of most document search: the question is not "what does
    this circular say" but "which of our tests does this obligation touch".
    """

    def __init__(self, tests: list[dict]):
        self.tests = tests
        self.documents = [document_text(t) for t in tests]
        self.bm25 = BM25Okapi([tokenize(d) for d in self.documents])
        self.vectoriser = Vectoriser(self.documents)
        # Prefer the vectors already stored by build_and_store(); recompute only if the
        # library was never indexed. Recomputing silently would hide a missed setup step.
        stored = [t.get("embedding") for t in tests]
        if all(isinstance(v, list) and v for v in stored) and self._stored_still_valid(stored):
            self.matrix = np.array(stored, dtype=np.float32)
        else:
            self.matrix = np.vstack([self.vectoriser.encode(d) for d in self.documents])

    def _stored_still_valid(self, stored: list) -> bool:
        """True when the stored vectors were built with the vocabulary in use now.

        These vectors are TF-IDF over the library corpus, so they are only meaningful
        against the exact vocabulary that produced them — unlike a real sentence-transformer,
        whose output depends on the model alone. Add tests to the library and the document
        frequencies shift, the vocabulary shifts with them, and every stored vector is
        silently indexed against the wrong words. Nothing errors; retrieval just quietly
        gets worse.

        Five documents are enough to catch it, and re-encoding five is free next to
        re-encoding five hundred.
        """
        if len(stored[0]) != len(self.vectoriser.vocab):
            return False
        step = max(len(self.documents) // 5, 1)
        for i in range(0, len(self.documents), step):
            if not np.allclose(np.array(stored[i], dtype=np.float32),
                               self.vectoriser.encode(self.documents[i]), atol=1e-4):
                print("   NOTE: stored vectors do not match the current vocabulary — "
                      "recomputing. Run the pipeline to store the corrected vectors.")
                return False
        return True

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

        # Reciprocal Rank Fusion. Each channel contributes 1/(k + its rank), so a test
        # ranked well by BOTH beats one ranked brilliantly by one and poorly by the other.
        # k=60 is the value from the original RRF paper and is not tuned here; it damps
        # the top ranks so position 1 does not overwhelm positions 2 and 3.
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
            # All three scores travel with the candidate, and they are NOT interchangeable.
            # score_fused orders the list and nothing else: RRF scores are rank-based, so
            # every top hit lands near 0.033 whether the match is perfect or hopeless.
            # Thresholding on it made every clause an Amendment. decide.py thresholds on
            # score_dense — a cosine similarity, which actually carries a relevance signal.
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
    """Rebuild the search index from the stored library. Call once per run, not per clause."""
    return Index(store.load_library())
