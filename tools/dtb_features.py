#!/usr/bin/env python3
"""Print a board's SoC-IP enable matrix from a DTB, optionally diff against a baseline.

The point: a DTS that "compiles fine" can still leave half the SoC disabled
(NPU/VPU/RGA/JPEG/dfi/pcie lanes), and that is invisible until someone runs
rkmpp or rknn. Compare every build against the vendor shipping board for the
same SoC (e.g. rk3568-nanopi5-rev01.dtb out of a FriendlyElec Android package).

usage:
  python dtb_features.py board.dtb [--base vendor.dtb] [--json]
"""
import argparse
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
from dtb_tool import parse  # noqa: E402

# path keyword -> short label
INTEREST = [
    ("npu@", "NPU"),
    ("bus-npu", "NPU-bus"),
    ("npu_mmu", "NPU-mmu"),
    ("gpu@", "GPU"),
    ("rkvdec@", "VPU-dec"),
    ("rkvenc@", "VPU-enc"),
    ("vdpu@", "VPU-comb"),
    ("vepu@", "VPU-enc2"),
    ("rk_rga@", "RGA"),
    ("iep@", "IEP"),
    ("jpegd@", "JPEG"),
    ("rng@", "RNG"),
    ("dfi", "DFI"),
    ("sata@", "SATA"),
    ("pcie@fe260000", "PCIe2x1"),
    ("pcie@fe270000", "PCIe3x1"),
    ("pcie@fe280000", "PCIe3x2"),
    ("dwmmc@fe000000", "SDIO-WiFi"),
    ("mmc@fe000000", "SDIO-WiFi"),
    ("dwmmc@fe2b0000", "eMMC"),
    ("mmc@fe2b0000", "eMMC"),
    ("sdhci@fe310000", "SD-card"),
    ("dwmmc@fe2c0000", "SDIO-slot"),
    ("mmc@fe2c0000", "SDIO-slot"),
    ("ethernet@fe2a0000", "GMAC0"),
    ("ethernet@fe010000", "GMAC1"),
    ("firmware/optee", "OP-TEE"),
    ("fiq-debugger", "fiq-debug"),
    ("ramoops", "ramoops"),
    ("linux,cma", "CMA"),
]


def matrix(path):
    _, nodes = parse(open(path, "rb").read())
    out = {}
    for n in nodes:
        p = n["path"]
        st = n["props"].get("status", b"okay").rstrip(b"\0").decode("utf-8", "replace")
        for key, label in INTEREST:
            if key in p and p.count("/") <= 3:
                out.setdefault(label, []).append((p, st))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dtb")
    ap.add_argument("--base", default="", help="vendor baseline dtb to diff against")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    cur = matrix(a.dtb)
    base = matrix(a.base) if a.base else {}

    def stata(m, label):
        v = m.get(label)
        if not v:
            return "(absent)"
        return ",".join(s for _, s in v)

    labels = list(dict.fromkeys([l for l in [x[1] for x in INTEREST]]))
    rows = []
    for l in labels:
        rows.append((l, stata(base, l) if a.base else "-", stata(cur, l)))

    if a.json:
        print(json.dumps({"dtb": a.dtb, "base": a.base,
                          "rows": [{"feature": r[0], "base": r[1], "board": r[2]} for r in rows]},
                         indent=2))
        return

    print(f"board : {a.dtb}")
    if a.base:
        print(f"base  : {a.base}")
    print(f"{'feature':12s} {'base':14s} {'board':14s} diff")
    for name, b, c in rows:
        mark = ""
        if a.base and b != c:
            mark = "<<< DIFF"
        print(f"{name:12s} {b:14s} {c:14s} {mark}")


if __name__ == "__main__":
    main()
