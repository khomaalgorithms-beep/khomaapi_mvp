"""Build the daily-R matrix (sessions x 1,800 configs) for the ORB optimization grid."""
import itertools, json, numpy as np
from multiprocessing import Pool
import orb_lab as OL
NS = (3, 5, 10, 15); ENT = ("touch", "close"); FIL = (("pdl",), ("pdl", "gap"), ("pdl", "onl"))
STK = (0.10, 0.15, 0.20, 0.25, 0.30); BE = (None, 0.5, 0.75, 1.0, 1.5); TG = (None, 3.0, 5.0)
GRID = list(itertools.product(NS, ENT, FIL, STK, BE, TG))
def job(i):
    n, e, f, sk, be, tg = GRID[i]
    res = OL.run("NQ", n=n, entry=e, cutoff=60, filt=f, stop="atr", stop_k=sk, be=be, tgt_R=tg)
    return i, res["day"], res["R"]
if __name__ == "__main__":
    g = OL.G("NQ"); idx = {int(d): k for k, d in enumerate(g.days)}
    M = np.zeros((g.D, len(GRID)), np.float32)
    with Pool(8) as pool:
        for i, day, R in pool.imap_unordered(job, range(len(GRID)), chunksize=8):
            for d, r in zip(day, R): M[idx[int(d)], i] += r
    np.save("results/orb_opt_M.npy", M); json.dump([list(map(lambda x: list(x) if isinstance(x, tuple) else x, c)) for c in GRID], open("results/orb_opt_grid.json", "w"))
    print("matrix", M.shape, "nonzero cols", int((np.abs(M).sum(0) > 0).sum()))
