"""Calibration benchmark for the method-2 ANN task (design doc §5).

Measures, under the eval harness's single-thread conditions:
  1. numpy chunked exact kNN  (the reference/baseline)
  2. faiss IndexFlatL2 exact  (fast-exact reference candidate)
  3. faiss IndexHNSWFlat       (graph ANN)
  4. faiss IndexIVFFlat        (inverted index ANN)
  5. faiss IndexIVFPQ          (quantized ANN)

Run with the eval interpreter (system python):
  python scripts/bench_ann.py [n_db] [n_query]
"""

from __future__ import annotations

import os
import statistics
import sys
import time

import numpy as np

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import faiss  # noqa: E402

N_DB = int(sys.argv[1]) if len(sys.argv) > 1 else 250_000
DIM = 128
N_QUERY = int(sys.argv[2]) if len(sys.argv) > 2 else 200
K = 10


def numpy_exact(db, queries, k=K):
    """Chunked |q-d|^2 exact kNN in float32 (memory-safe at 1M)."""
    out = np.empty((queries.shape[0], k), dtype=np.int64)
    qn = (queries * queries).sum(1, keepdims=True)
    dn = (db * db).sum(1)
    CHUNK = 50
    for i in range(0, queries.shape[0], CHUNK):
        q = queries[i : i + CHUNK]
        d2 = qn[i : i + CHUNK] + dn - 2.0 * (q @ db.T)  # (chunk, n_db) float32
        out[i : i + CHUNK] = np.argpartition(d2, k - 1, axis=1)[:, :k]
    return out


def recall(exact_idx, cand_idx):
    nq = exact_idx.shape[0]
    hits = sum(len(set(exact_idx[i].tolist()) & set(cand_idx[i].tolist())) for i in range(nq))
    return hits / (nq * K)


def med(fn, repeat=3):
    fn()
    ts = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts)


def main() -> None:
    print(f"n_db={N_DB:,} dim={DIM} n_query={N_QUERY} k={K}  (OMP_NUM_THREADS={os.environ['OMP_NUM_THREADS']})", flush=True)
    rng = np.random.default_rng(42)
    db = np.ascontiguousarray(rng.standard_normal((N_DB, DIM)).astype(np.float32))
    queries = np.ascontiguousarray(rng.standard_normal((N_QUERY, DIM)).astype(np.float32))

    t_np = med(lambda: numpy_exact(db, queries))
    exact = numpy_exact(db, queries)
    print(f"1. numpy chunked exact    : {t_np:6.2f}s", flush=True)

    def flat_search():
        idx = faiss.IndexFlatL2(DIM)
        idx.add(db)
        return idx.search(queries, K)
    t_flat = med(flat_search)
    print(f"2. faiss IndexFlatL2 exact: {t_flat:6.2f}s", flush=True)

    for M, ef_c, ef_s in [(16, 200, 100), (16, 200, 300), (32, 200, 200)]:
        def build_search(M=M, ef_c=ef_c, ef_s=ef_s):
            idx = faiss.IndexHNSWFlat(DIM, M)
            idx.hnsw.efConstruction = ef_c
            idx.hnsw.efSearch = ef_s
            idx.add(db)
            return idx, idx.search(queries, K)
        build_search()  # warm-up
        tbs = []
        for _ in range(3):
            t0 = time.perf_counter()
            idx, (D, I) = build_search()
            tbs.append(time.perf_counter() - t0)
        idx, (D, I) = build_search()
        print(f"3. HNSW M={M:2d} efC={ef_c:3d} efS={ef_s:3d}: build+query {statistics.median(tbs):6.2f}s  recall {recall(exact, I):.4f}", flush=True)

    for nlist, nprobe in [(500, 10), (1000, 20)]:
        def build_ivf(nlist=nlist, nprobe=nprobe):
            idx = faiss.IndexIVFFlat(faiss.IndexFlatL2(DIM), DIM, nlist)
            idx.nprobe = nprobe
            idx.train(db)
            idx.add(db)
            return idx, idx.search(queries, K)
        build_ivf()
        tbs = []
        for _ in range(3):
            t0 = time.perf_counter()
            idx, (D, I) = build_ivf()
            tbs.append(time.perf_counter() - t0)
        idx, (D, I) = build_ivf()
        print(f"4. IVF-Flat nlist={nlist:4d} nprobe={nprobe:3d}: build+query {statistics.median(tbs):6.2f}s  recall {recall(exact, I):.4f}", flush=True)

    for nlist, nprobe in [(1000, 20), (4000, 40)]:
        def build_pq(nlist=nlist, nprobe=nprobe):
            idx = faiss.IndexIVFPQ(faiss.IndexFlatL2(DIM), DIM, nlist, 16, 8)
            idx.nprobe = nprobe
            idx.train(db)
            idx.add(db)
            return idx, idx.search(queries, K)
        build_pq()
        tbs = []
        for _ in range(3):
            t0 = time.perf_counter()
            idx, (D, I) = build_pq()
            tbs.append(time.perf_counter() - t0)
        idx, (D, I) = build_pq()
        print(f"5. IVF-PQ nlist={nlist:4d} nprobe={nprobe:3d}: build+query {statistics.median(tbs):6.2f}s  recall {recall(exact, I):.4f}", flush=True)

    print("\ndone.", flush=True)


if __name__ == "__main__":
    main()
