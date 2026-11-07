#!/usr/bin/env python3
"""Replace boot logos (logo.bmp / logo_kernel.bmp) inside a Rockchip Android boot.img.

Why it needs a dedicated tool
-----------------------------
On RK3588 Android (rkr12/rkr14) there is **no `logo` partition** in parameter.txt.
Both boot screens are RSCE entries in the `second` stage of boot.img:

    logo.bmp          -> shown by U-Boot (before kernel)
    logo_kernel.bmp   -> shown by kernel until SurfaceFlinger takes over
    battery_*.bmp     -> charging animation (U-Boot / charger)

Replacing an entry changes the RSCE size, which changes `second_size` at 0x18 and
invalidates the Rockchip boot id SHA1 at 0x240 -> U-Boot prints
"Failed to load android image" and aborts. So both must be rewritten.

Usage
-----
    python logo_tool.py list <boot.img>
    python logo_tool.py set  <boot.img> --logo logo.bmp [--kernel-logo logo_kernel.bmp] -o out.img

The output keeps kernel / ramdisk / cmdline byte-identical; only the second stage,
its size field and the SHA1 are touched (verified by the sanity check below).
"""
import argparse
import fnmatch
import struct
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
import rsce_tool as R
import rk_boot_id as B


def align(n, p):
    return ((n + p - 1) // p) * p


def build_rcs_verbatim(entries, header):
    """Rebuild RSCE keeping every entry's 512-byte record byte-for-byte.

    Rockchip's ENTR records keep 256 bytes for the path but only the part before
    the NUL is meaningful -- the rest can hold leftovers from an older, longer
    name. rsce_tool.build_rcs rewrites the whole name field (padding with zeros),
    which is harmless but makes a "replace with the same file" roundtrip differ.
    Copying the record verbatim keeps the roundtrip byte-identical.
    """
    n = len(entries)
    hdr = bytearray(header[:R.HDR])
    struct.pack_into("<I", hdr, 12, n)

    base = R.HDR * (1 + n)
    body = bytearray()
    for e in entries:
        pos = base + len(body)
        if pos % R.HDR:
            body += b"\0" * (R.HDR - pos % R.HDR)
            pos = base + len(body)
        e["offset"] = pos // R.HDR
        e["size"] = len(e["data"])
        body += e["data"]

    idx = bytearray()
    for e in entries:
        blk = bytearray(e["raw"][:R.ENT])
        struct.pack_into("<II", blk, 260, e["offset"], e["size"])
        idx += blk
    return bytes(hdr) + bytes(idx) + bytes(body)


def pick(entries, pattern):
    return [e for e in entries if fnmatch.fnmatchcase(e["name"].lower(), pattern)]


def cmd_list(args):
    boot = open(args.file, "rb").read()
    info = R.boot_parse(boot)
    off, ssize = info["second_off"], info["second_size"]
    print("%s: second@0x%x size=%d entries:" % (args.file, off, ssize))
    print("%3s %10s %8s  %-24s %s" % ("idx", "offset", "size", "name", "bmp w x h / bpp"))
    for e in R.parse_rcs(boot[off:off + ssize]):
        extra = ""
        d = e["data"]
        if d[:2] == b"BM" and len(d) > 30:
            w, h = struct.unpack_from("<ii", d, 18)
            bpp = struct.unpack_from("<H", d, 28)[0]
            extra = "%d x %d / %d bpp" % (w, h, bpp)
        print("%3d %10d %8d  %-24s %s" % (e["idx"], e["offset"], e["size"], e["name"], extra))


def cmd_set(args):
    boot = bytearray(open(args.file, "rb").read())
    info = R.boot_parse(bytes(boot))
    off, ssize, page = info["second_off"], info["second_size"], info["page_size"]
    entries = R.parse_rcs(bytes(boot[off:off + ssize]))

    jobs = []
    if args.logo:
        jobs.append(("logo.bmp", args.logo))
    if args.kernel_logo:
        jobs.append(("logo_kernel.bmp", args.kernel_logo))
    for name, path in jobs:
        hits = pick(entries, name)
        if not hits:
            print("!! %s not found in this boot.img; entries present:" % name)
            for e in entries:
                print("   ", e["name"])
            raise SystemExit(2)
        blob = open(path, "rb").read()
        if blob[:2] != b"BM":
            raise SystemExit("%s is not a BMP" % path)
        w, h = struct.unpack_from("<ii", blob, 18)
        bpp = struct.unpack_from("<H", blob, 28)[0]
        if (w, h) != (args.expect_w, args.expect_h) or bpp != 8:
            raise SystemExit(
                "%s is %dx%d/%dbpp but this panel expects %dx%d/8bpp "
                "(keep the stock geometry, otherwise U-Boot shows garbage)"
                % (path, w, h, bpp, args.expect_w, args.expect_h))
        for e in hits:
            old = len(e["data"])
            e["data"] = blob
            print("replace %-18s %6d -> %6d bytes" % (e["name"], old, len(blob)))

    new_second = build_rcs_verbatim(entries, bytes(boot[off:off + R.HDR]))
    # RSCE keeps its own 512-byte granularity; the boot stage pads to page_size
    new_second += b"\0" * ((-len(new_second)) % R.HDR)
    pad = (-len(new_second)) % page
    # keep everything after the second stage (v2 header may carry a separate dtb
    # segment -- this vendor boot.img does: dtb_size=252757 at 0x670)
    tail = bytes(boot[off + align(ssize, page):])
    out = bytes(boot[:off]) + new_second + b"\0" * pad + tail

    # sanity: nothing before `second` may change except second_size @0x18
    diff = [i for i in range(off) if boot[i] != out[i]]
    bad = [i for i in diff if not (0x18 <= i < 0x1C)]
    if bad:
        raise SystemExit("FATAL: header/kernel/ramdisk changed at %s" % bad[:8])

    open(args.out, "wb").write(out)
    print("boot %d -> %d bytes (second %d -> %d, pad %d, tail %d kept)"
          % (len(boot), len(out), ssize, len(new_second), pad, len(tail)))

    # rewrite Rockchip boot id SHA1 over kernel/ramdisk/second
    blob = bytearray(open(args.out, "rb").read())
    h = B.parse(bytes(blob))
    real = B.digest(bytes(blob), h)
    blob[B.ID_OFF:B.ID_OFF + B.ID_LEN] = real
    open(args.out, "wb").write(bytes(blob))
    h2 = B.parse(open(args.out, "rb").read())
    print("id[20] header %s -> %s  (%s)"
          % (h["id"][:B.ID_LEN].hex(), real.hex(),
             "OK" if B.digest(open(args.out, "rb").read(), h2) == real else "FAIL"))

    chk = R.boot_parse(open(args.out, "rb").read())
    assert chk["second_size"] == len(new_second)
    print("verify: second_size=%d, kernel=%d, ramdisk=%d, cmdline unchanged=%s"
          % (chk["second_size"], chk["kernel_size"], chk["ramdisk_size"],
             chk["cmdline"] == info["cmdline"]))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("file"); p.set_defaults(func=cmd_list)
    p = sub.add_parser("set")
    p.add_argument("file")
    p.add_argument("--logo", help="U-Boot logo.bmp")
    p.add_argument("--kernel-logo", help="kernel logo_kernel.bmp")
    p.add_argument("--expect-w", type=int, default=654)
    p.add_argument("--expect-h", type=int, default=270)
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(func=cmd_set)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
