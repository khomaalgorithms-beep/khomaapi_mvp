"""Random-walk check of Spec A alone (10:30 only) and of the old look-ahead chain, for the record."""
import numpy as np
from multiprocessing import Pool
import max_stress as MS, stress as T
def run(args):
    seed, cfg = args
    orig = T.legs
    T.legs = lambda c, _o=orig, _x=cfg: _o(dict(c, **_x))
    r = MS.rw_R(seed); T.legs = orig
    return seed, cfg, r
if __name__ == "__main__":
    jobs = [(s, dict(chain=T.BASE_CHAIN[:1])) for s in (1, 2, 3)] + [(s, dict(lookahead_chain=True)) for s in (1, 2)]
    with Pool(5) as pool:
        for seed, cfg, r in pool.map(run, jobs):
            nm = "Spec A only" if "chain" in cfg else "OLD look-ahead chain"
            print(f"RANDOM WALK {nm:22} seed {seed}: net meanR {r['net']['mean']:+.3f} t={r['net']['t']:.2f} PF={r['net']['pf']:.2f} | gross {r['gross']['mean']:+.3f} t={r['gross']['t']:.2f}")
