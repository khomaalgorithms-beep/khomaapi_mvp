"""Stream FirstRate 1-min unadjusted continuous files out of the bundle zip into compact
compressed numpy arrays: data/{SYM}.npz with ts (int32 minutes since epoch, US/Eastern wall clock),
o,h,l,c (float32), v (int32). No intermediate text file is written (disk is tight)."""
import subprocess, sys, os, time
import numpy as np

ZIP = os.path.expanduser("~/khomaapi_mvp/.claude/worktrees/khoma-volume-orb-protocol-673616/"
                         "orb_research/frd/bundle/futures_full_1min_contin_UNadj.zip")
OUT = os.path.join(os.path.dirname(__file__), "data")

# Lucid-approved tradables available in the bundle + ZN as a signal-only series
UNIVERSE = ["ES", "NQ", "RTY", "YM", "NKD", "CL", "NG", "QG", "GC", "SI", "HG", "PL",
            "A6", "B6", "E6", "J1", "E1", "N6", "ZC", "ZS", "ZW", "ZL", "ZM", "HE", "LE", "ZN"]


def convert(sym, src=None):
    t0 = time.time()
    if src is None:
        raw = subprocess.run(["unzip", "-p", ZIP, f"{sym}_full_1min_continuous_UNadjusted.txt"],
                             capture_output=True, check=True).stdout
    else:
        raw = subprocess.run(["unzip", "-p", src], capture_output=True, check=True).stdout
    lines = raw.split(b"\n")
    del raw
    lines = [l for l in lines if len(l) > 20]
    ts = np.array([l[:16].decode() for l in lines], dtype="datetime64[m]").astype(np.int64)
    num = b",".join(l[20:] for l in lines)
    del lines
    arr = np.array(num.split(b","), dtype=np.float64).reshape(-1, 5)
    del num
    order = np.argsort(ts, kind="stable")
    ts, arr = ts[order], arr[order]
    keep = np.ones(len(ts), bool); keep[1:] = ts[1:] != ts[:-1]       # drop duplicate minutes
    ts, arr = ts[keep], arr[keep]
    np.savez_compressed(os.path.join(OUT, f"{sym}.npz"), ts=ts.astype(np.int32),
                        o=arr[:, 0].astype(np.float32), h=arr[:, 1].astype(np.float32),
                        l=arr[:, 2].astype(np.float32), c=arr[:, 3].astype(np.float32),
                        v=np.minimum(arr[:, 4], 2**31 - 1).astype(np.int32))
    first = np.datetime64(int(ts[0]), "m"); last = np.datetime64(int(ts[-1]), "m")
    print(f"{sym}: {len(ts):,} bars {first} -> {last}  ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    syms = sys.argv[1:] or UNIVERSE
    for s in syms:
        if s == "NQ_ADJ":
            convert(s, os.path.expanduser("~/Downloads/NQ_full_1min_continuous_absolute_adjusted_z23su0.zip"))
        elif not os.path.exists(os.path.join(OUT, f"{s}.npz")):
            convert(s)
