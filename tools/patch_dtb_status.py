#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch DT node status in-place inside every rk3568-nanopi5-rev* slot of a boot.img.

Why: RK u-boot verifies the boot header SHA1 that covers the RSCE second stage, so any
DTB edit must be followed by `rk_boot_id.py fix`. In-place status patch keeps the blob
length identical ("okay\\0" -> "fail\\0", both 5 bytes), so no FDT re-serialisation and
no slot size/offset churn.

Usage:
  python patch_dtb_status.py boot.img -o boot.out.img --node npu@fde40000=30096 \
      --node iommu@fde4b000=32952
The offsets are byte offsets of the property VALUE inside the DTB blob (find them with
fdt_find_status.py, or from a previous u-boot patch attempt).
"""
import argparse
import struct
import sys

OKAY = b"okay\x00"
FAIL = b"fail\x00"


def rsce_slots(img):
    """Yield (name, abs_offset, size) for every RSCE entry."""
    ks = struct.unpack_from("<I", img, 8)[0]
    rs = struct.unpack_from("<I", img, 16)[0]
    page = struct.unpack_from("<I", img, 0x24)[0] or 2048
    align = lambda n: ((n + page - 1) // page) * page
    so = align(page + align(ks) + align(rs))
    if img[so:so + 4] != b"RSCE":
        raise SystemExit("second stage at 0x%X is not RSCE (%r)" % (so, img[so:so + 4]))
    count = struct.unpack_from("<I", img, so + 12)[0]
    for i in range(count):
        e = img[so + 512 * (i + 1): so + 512 * (i + 2)]
        if e[:4] != b"ENTR":
            continue
        name = e[4:260].split(b"\x00")[0].decode("latin1")
        off, size = struct.unpack_from("<II", e, 0x104)
        yield name, so + off * 512, size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("img")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--pattern", default="rk3568-nanopi5-rev",
                    help="only patch slots whose name startswith this")
    ap.add_argument("--node", action="append", required=True,
                    metavar="NAME=OFFSET", help="property value offset inside the DTB blob")
    a = ap.parse_args()

    targets = []
    for spec in a.node:
        name, _, off = spec.partition("=")
        targets.append((name, int(off, 0)))

    data = bytearray(open(a.img, "rb").read())
    patched = 0
    for name, base, size in rsce_slots(bytes(data)):
        if not name.startswith(a.pattern):
            continue
        for node, off in targets:
            p = base + off
            if data[p:p + 5] != OKAY:
                raise SystemExit("%s @%s: expected %r, got %r"
                                 % (name, node, OKAY, bytes(data[p:p + 5])))
            data[p:p + 5] = FAIL
            patched += 1
    if not patched:
        raise SystemExit("no slot matched pattern %r" % a.pattern)

    open(a.out, "wb").write(bytes(data))
    print("patched %d property value(s) across %d slot(s) -> %s"
          % (patched, patched // len(targets), a.out))
    print("next: rk_boot_id.py fix %s -o %s.fixed && rk_boot_id.py check %s.fixed"
          % (a.out, a.out, a.out))


if __name__ == "__main__":
    sys.exit(main())
