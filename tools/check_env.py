"""Check a training machine before spending GPU time on it.

    python tools/check_env.py --data laya-data

Verifies Python, PyTorch, the GPU and its compute capability, Laya, and the
dataset, then estimates how long an epoch will take. Prints a go / no-go.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

OK, WARN, BAD = "ok  ", "warn", "FAIL"
results: list[tuple[str, str, str]] = []


def check(status: str, name: str, detail: str) -> None:
    results.append((status, name, detail))
    print(f"[{status}] {name}: {detail}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="laya-data", help="directory holding train/val jsonl")
    args = ap.parse_args()

    v = sys.version_info
    check(OK if v >= (3, 10) else BAD, "python", f"{v.major}.{v.minor}.{v.micro} (need 3.10+)")

    try:
        import torch
    except ImportError:
        check(BAD, "torch", "not installed - pip install torch --index-url "
                            "https://download.pytorch.org/whl/cu130")
        torch = None

    gpu_ok = False
    if torch:
        check(OK, "torch", f"{torch.__version__}")
        if not torch.cuda.is_available():
            check(BAD, "cuda", "no CUDA device visible to torch - training will crawl on CPU")
        else:
            name = torch.cuda.get_device_name(0)
            major, minor = torch.cuda.get_device_capability(0)
            mem = torch.cuda.get_device_properties(0).total_memory / 1e9
            check(OK, "gpu", f"{name}, sm_{major}{minor}, {mem:.0f} GB, "
                             f"{torch.cuda.device_count()} visible")

            # A Blackwell card on a pre-12.8 torch build reports fine here and
            # then fails on the first kernel launch, so actually launch one.
            try:
                x = torch.randn(256, 256, device="cuda")
                torch.mm(x, x).sum().item()
                check(OK, "cuda kernels", "a test matmul ran on the GPU")
                gpu_ok = True
            except Exception as err:
                msg = str(err).split("\n")[0][:120]
                check(BAD, "cuda kernels", f"{msg}\n       this torch build has no sm_{major}{minor}"
                                           " kernels - install a cu128 or cu130 wheel")

    try:
        import laya
        check(OK, "laya", f"{getattr(laya, '__version__', 'unknown')}")
    except ImportError:
        check(BAD, "laya", "not installed - pip install laya")

    data = Path(args.data).expanduser()
    train, val = data / "train.jsonl", data / "val.jsonl"
    items = 0
    if train.exists():
        rows = train.read_text().splitlines()
        items = sum(len(json.loads(r).get("gold", {})) for r in rows if r.strip())
        check(OK, "train.jsonl", f"{len(rows)} emails, {items} labelled questions")
    else:
        check(BAD, "train.jsonl", f"not found at {train}")
    check(OK if val.exists() else BAD, "val.jsonl",
          f"{len(val.read_text().splitlines())} emails" if val.exists() else f"not found at {val}")

    free = shutil.disk_usage(data if data.exists() else Path.cwd()).free / 1e9
    check(OK if free > 10 else WARN, "disk", f"{free:.0f} GB free (want 10+ for weights "
                                             "and checkpoints)")

    if gpu_ok and items:
        # ~1,500 items/s is a conservative rate for a 322M encoder at bf16 on a
        # current card; good enough to tell minutes from hours.
        minutes = items / 1500 / 60
        check(OK, "estimate", f"roughly {minutes:.0f}-{minutes * 3:.0f} min per epoch")

    print()
    if any(s == BAD for s, _, _ in results):
        print("NOT READY - fix the FAIL lines above.")
        sys.exit(1)
    print("Ready to train. Next:")
    print("  CUDA_VISIBLE_DEVICES=0 laya-train --data %s --out out/inbox-v1 \\" % train)
    print("    --loss soft-ce --epochs 3 --micro-batch 32 --grad-accum 2")


if __name__ == "__main__":
    main()
