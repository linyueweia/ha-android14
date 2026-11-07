#!/usr/bin/env python3
"""One-shot: stock FriendlyElec Android 14 boot.img -> H88K boot.img.

Does three things in a single RSCE rebuild (so the image is only written once):

    1. every `rk3588-nanopi6-rev*.dtb` slot  <- our H88K dtb
       (the stock boot.img carries 12 of them, rev01..rev0c; hwrev probing on a
       non-FriendlyElec board is unpredictable, so all of them must be filled)
    2. logo.bmp / logo_kernel.bmp           <- H88K artwork
    3. second_size + Rockchip boot id SHA1  <- recomputed

Refuses to write a boot.img that does not fit the `boot_a` partition, because an
oversized image flashes fine and then fails to load with no useful message.

Usage:
    python pack_boot.py --stock boot.img --dtb rk3588-hlink-h88k.dtb \
        --logo logos/h88k-lockup-512x128.bmp -o out/boot.img
"""
import argparse
import fnmatch
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logo_tool as L
import rk_boot_id as B
import rsce_tool as R


def bmp_info(blob):
    if blob[:2] != b"BM":
        raise SystemExit("not a BMP")
    w, h = struct.unpack_from("<ii", blob, 18)
    bpp = struct.unpack_from("<H", blob, 28)[0]
    return w, h, bpp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True)
    ap.add_argument("--dtb", required=True, help="H88K dtb; fills every rk3588 slot")
    ap.add_argument("--dtb-pattern", default="rk3588-nanopi6-rev*.dtb")
    ap.add_argument("--logo")
    ap.add_argument("--kernel-logo")
    ap.add_argument("--max-size", type=int, default=0x20000 * 512,
                    help="boot_a partition bytes (default 64 MiB, the rk3588 A/B slot)")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()

    boot = open(a.stock, "rb").read()
    info = R.boot_parse(boot)
    off, ssize, page = info["second_off"], info["second_size"], info["page_size"]
    entries = R.parse_rcs(boot[off:off + ssize])

    dtb = open(a.dtb, "rb").read()
    if dtb[:4] != R.FDT_MAGIC:
        raise SystemExit("%s is not a dtb" % a.dtb)

    slots = [e for e in entries if fnmatch.fnmatchcase(e["name"], a.dtb_pattern)]
    if not slots:
        print("no entry matched %r; present:" % a.dtb_pattern)
        for e in entries:
            print("   ", e["name"])
        raise SystemExit(2)
    for e in slots:
        e["data"] = dtb
    print("dtb   : %d slot(s) <- %s (%d bytes)" % (len(slots), os.path.basename(a.dtb), len(dtb)))
    print("        " + ", ".join(e["name"] for e in slots))

    for name, path in (("logo.bmp", a.logo), ("logo_kernel.bmp", a.kernel_logo)):
        if not path:
            continue
        hits = [e for e in entries if e["name"].lower() == name]
        if not hits:
            print("!! %s missing in stock boot.img" % name)
            raise SystemExit(2)
        blob = open(path, "rb").read()
        w, h, bpp = bmp_info(blob)
        ew, eh, ebpp = bmp_info(hits[0]["data"])
        if (w, abs(h)) != (ew, abs(eh)) or bpp != ebpp:
            raise SystemExit("%s: %dx%d/%dbpp != slot %dx%d/%dbpp"
                             % (path, w, h, bpp, ew, eh, ebpp))
        if (h > 0) != (eh > 0):
            raise SystemExit("%s orientation (%s) != slot (%s); regenerate with/without --topdown"
                             % (path, "bottom-up" if h > 0 else "top-down",
                                "bottom-up" if eh > 0 else "top-down"))
        for e in hits:
            e["data"] = blob
        print("logo  : %-16s <- %s (%dx%d/%dbpp)"
              % (name, os.path.basename(path), w, abs(h), bpp))

    new_second = L.build_rcs_verbatim(entries, boot[off:off + R.HDR])
    new_second += b"\0" * ((-len(new_second)) % R.HDR)
    pad = (-len(new_second)) % page
    tail = boot[off + L.align(ssize, page):]

    head = bytearray(boot[:off])
    struct.pack_into("<I", head, 0x18, len(new_second))
    out = bytes(head) + new_second + b"\0" * pad + tail

    bad = [i for i in range(off) if boot[i] != out[i] and not (0x18 <= i < 0x1C)]
    if bad:
        raise SystemExit("FATAL: header/kernel/ramdisk changed at %s" % bad[:8])

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "wb").write(out)

    buf = bytearray(open(a.out, "rb").read())
    h = B.parse(bytes(buf))
    real = B.digest(bytes(buf), h)
    buf[B.ID_OFF:B.ID_OFF + B.ID_LEN] = real
    open(a.out, "wb").write(bytes(buf))

    chk = R.boot_parse(open(a.out, "rb").read())
    ok_hash = B.digest(open(a.out, "rb").read(), B.parse(open(a.out, "rb").read())) == real
    print("")
    print("boot  : %d -> %d bytes (second %d -> %d, pad %d, tail %d)"
          % (len(boot), len(buf), ssize, len(new_second), pad, len(tail)))
    print("id    : %s  %s" % (real.hex(), "MATCH" if ok_hash else "FAIL"))
    print("check : second_size=%d kernel=%d ramdisk=%d cmdline-unchanged=%s"
          % (chk["second_size"], chk["kernel_size"], chk["ramdisk_size"],
             chk["cmdline"] == info["cmdline"]))
    if a.max_size and len(buf) > a.max_size:
        raise SystemExit("TOO BIG: %d > partition %d bytes" % (len(buf), a.max_size))
    print("size  : %.2f MB / %.2f MB partition (%.2f MB free)"
          % (len(buf) / 1048576, a.max_size / 1048576, (a.max_size - len(buf)) / 1048576))


if __name__ == "__main__":
    main()
