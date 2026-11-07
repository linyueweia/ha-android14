#!/usr/bin/env python3
"""Rockchip RSCE v0 resource image + Android boot.img (v2) dtb replacer.

Format (empirically confirmed on FriendlyElec rk3568-sd-android14-20251222.img):
  header 512 B: "RSCE" | u32 0 | u16 0x0101 | u16 0x0001 | u32 count | zeros
  entry  512 B: "ENTR" @0 | path[256] @4 | u32 offset_blocks @0x104 | u32 size @0x108
                offset unit = 512 bytes, relative to RSCE start
  data from 512 + 512*count, each blob 512-aligned

Usage:
  python rsce_tool.py list   <rcs.img>
  python rsce_tool.py replace <rcs.img> --pattern 'rk3568-nanopi5-rev*' --dtb file.dtb -o out.rcs
  python rsce_tool.py boot-list   <boot.img>
  python rsce_tool.py boot-replace <boot.img> --pattern 'rk3568-nanopi5-rev*' --dtb file.dtb -o boot.new.img
  python rsce_tool.py probe <boot.img>       # show header fields + second offset

Notes:
  * replace keeps entry count, entry order and every non-dtb blob untouched.
  * blobs shorter than their slot are re-aligned to 512, so total size may change.
    boot-replace then rewrites second_size (+0x18) and page-pads the file.
"""
import argparse
import fnmatch
import re
import struct
import sys

FDT_MAGIC = b"\xd0\x0d\xfe\xed"
RSCE_MAGIC = b"RSCE"
ENTR_MAGIC = b"ENTR"
HDR = 512
ENT = 512


def parse_rcs(data):
    if data[:4] != RSCE_MAGIC:
        raise SystemExit(f"not RSCE (head={data[:8]!r})")
    n = struct.unpack_from("<I", data, 12)[0]
    entries = []
    for i in range(n):
        raw = data[HDR + i * ENT : HDR + (i + 1) * ENT]
        if raw[:4] != ENTR_MAGIC:
            raise SystemExit(f"entry {i}: bad magic {raw[:4]!r}")
        name = raw[4:260].split(b"\0")[0].decode("utf-8", "replace")
        blocks, size = struct.unpack_from("<II", raw, 260)
        off = blocks * 512
        blob = data[off : off + size]
        entries.append(
            {"idx": i, "name": name, "raw": raw, "offset": off, "size": size, "data": blob}
        )
    return entries


def build_rcs(entries, header=None, count=None):
    n = len(entries)
    hdr = bytearray(header[:HDR] if header else b"")
    if len(hdr) != HDR:
        hdr = bytearray(HDR)
        hdr[0:4] = RSCE_MAGIC
        hdr[8:10] = struct.pack("<H", 0x0101)
        hdr[10:12] = struct.pack("<H", 0x0001)
    struct.pack_into("<I", hdr, 12, n)

    base = HDR * (1 + n)
    body = bytearray()
    for e in entries:
        pos = base + len(body)
        if pos % 512:
            pad = 512 - (pos % 512)
            body += b"\0" * pad
            pos += pad
        e["offset"] = pos // 512
        e["size"] = len(e["data"])
        body += e["data"]

    idx = bytearray()
    for e in entries:
        blk = bytearray(e["raw"][:ENT])
        nm = e["name"].encode("utf-8")[:255]
        # only touch the name field when it actually changes: Rockchip leaves
        # junk in the unused tail of path[256], and rewriting it changes bytes
        # that a golden roundtrip (replace with the same blob) must preserve.
        if blk[4:260].split(b"\0")[0] != nm:
            blk[4:260] = nm.ljust(256, b"\0")
        struct.pack_into("<II", blk, 260, e["offset"], e["size"])
        idx += blk
    return bytes(hdr) + bytes(idx) + bytes(body)


def replace_in_rcs(data, pattern, dtb, fill_range=None):
    entries = parse_rcs(data)
    hits = [e for e in entries if fnmatch.fnmatchcase(e["name"], pattern)]
    if not hits:
        print(f"!! no entry matched {pattern!r}; names present:")
        for e in entries:
            print("   ", e["name"])
        raise SystemExit(2)
    for e in hits:
        e["data"] = dtb
    added = []
    if fill_range:
        lo, hi = fill_range
        tmpl = hits[0]["name"]
        m = re.search(r"rev([0-9a-fA-F]{2})", tmpl)
        if not m:
            raise SystemExit(f"cannot derive rev template from {tmpl!r}")
        tpl = tmpl[: m.start(1)] + "%02x" + tmpl[m.end(1) :]
        have = {e["name"] for e in entries}
        sample = hits[0]
        for rev in range(lo, hi + 1):
            name = tpl % rev
            if name in have:
                continue
            e = {
                "idx": len(entries),
                "name": name,
                "raw": sample["raw"],
                "offset": 0,
                "size": len(dtb),
                "data": dtb,
            }
            entries.append(e)
            added.append(name)
    out = build_rcs(entries, data[:HDR])
    print(f"replaced {len(hits)} entr{'y' if len(hits)==1 else 'ies'} with {len(dtb)} bytes: "
          + ", ".join(e["name"] for e in hits))
    if added:
        print(f"added {len(added)} rev slot(s): " + ", ".join(added))
    print(f"rcs size {len(data)} -> {len(out)} bytes (delta {len(out)-len(data):+d})")
    return out


def boot_parse(boot):
    if boot[:8] != b"ANDROID!":
        raise SystemExit(f"not Android boot img (head={boot[:8]!r})")
    f = struct.unpack_from("<10I", boot, 8)
    ksize, kaddr, rsize, raddr, ssize, saddr, tags, page, hdrver, osver = f
    second_off = page * (
        1 + (ksize + page - 1) // page + (rsize + page - 1) // page
    )
    return {
        "kernel_size": ksize, "kernel_addr": kaddr,
        "ramdisk_size": rsize, "ramdisk_addr": raddr,
        "second_size": ssize, "second_addr": saddr, "tags_addr": tags,
        "page_size": page, "header_version": hdrver, "os_version": osver,
        "second_off": second_off,
        "cmdline": boot[0x40:0x240].split(b"\0")[0].decode("utf-8", "replace"),
        "extra_cmdline": boot[0x260:0x660].split(b"\0")[0].decode("utf-8", "replace"),
    }


def boot_replace(boot, pattern, dtb, fill_range=None):
    info = boot_parse(boot)
    page, off, ssize = info["page_size"], info["second_off"], info["second_size"]
    second = boot[off : off + ssize]
    new_second = replace_in_rcs(second, pattern, dtb, fill_range)
    # RSCE keeps its own 512-byte granularity (stock images are 512-multiples)
    new_second += b"\0" * ((-len(new_second)) % HDR)
    pad = (-len(new_second)) % page
    # v2 header may carry a separate dtb segment after `second` (the vendor H88K
    # Android 12 boot.img does: dtb_size=252757). Dropping it breaks the SHA1 and
    # the boot. Keep whatever follows, byte for byte.
    tail = boot[off + ((ssize + page - 1) // page) * page:]
    head = bytearray(boot[:off])
    struct.pack_into("<I", head, 0x18, len(new_second))
    out = bytes(head) + new_second + b"\0" * pad + tail
    print(
        f"boot: second {ssize} -> {len(new_second)} (+{pad} page pad); "
        f"file {len(boot)} -> {len(out)}; second_offset={off} page={page}; "
        f"tail {len(tail)} kept"
    )
    # sanity: everything before second must be byte-identical except second_size @0x18
    diff = [i for i in range(off) if boot[i] != out[i]]
    bad = [i for i in diff if not (0x18 <= i < 0x1C)]
    if bad:
        raise SystemExit(f"FATAL: header/kernel/ramdisk changed at {bad[:8]}")
    print(f"head+kernel+ramdisk identical except second_size at 0x18 ({len(diff)} byte(s))")
    assert boot_parse(out)["second_size"] == len(new_second)
    return out


def cmd_list(args):
    data = open(args.file, "rb").read()
    entries = parse_rcs(data)
    print(f"{args.file}: count={len(entries)} size={len(data)}")
    print(f"{'idx':>3} {'offset':>10} {'size':>8}  {'fdt':4} name")
    for e in entries:
        fdt = "yes" if e["data"][:4] == FDT_MAGIC else "-"
        print(f"{e['idx']:3d} {e['offset']:10d} {e['size']:8d}  {fdt:4} {e['name']}")


def cmd_probe(args):
    info = boot_parse(open(args.file, "rb").read())
    for k, v in info.items():
        if isinstance(v, int) and k not in ("cmdline", "extra_cmdline"):
            print(f"{k:16s} {v}  (0x{v:x})")
        elif k in ("cmdline", "extra_cmdline"):
            print(f"{k:16s} {v}")
    off = info["second_off"]
    data = open(args.file, "rb").read()
    print(f"second magic     {data[off:off+4]!r}")


def cmd_boot_list(args):
    data = open(args.file, "rb").read()
    info = boot_parse(data)
    off, ssize = info["second_off"], info["second_size"]
    entries = parse_rcs(data[off : off + ssize])
    print(f"{args.file}: second@{off} size={ssize} entries={len(entries)}")
    for e in entries:
        fdt = "yes" if e["data"][:4] == FDT_MAGIC else "-"
        print(f"{e['idx']:3d} {e['offset']:10d} {e['size']:8d}  {fdt:4} {e['name']}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("file"); p.set_defaults(func=cmd_list)
    p = sub.add_parser("probe"); p.add_argument("file"); p.set_defaults(func=cmd_probe)
    p = sub.add_parser("boot-list"); p.add_argument("file"); p.set_defaults(func=cmd_boot_list)

    p = sub.add_parser("replace")
    p.add_argument("file"); p.add_argument("--pattern", required=True)
    p.add_argument("--dtb", required=True); p.add_argument("-o", "--out", required=True)
    p.add_argument("--fill-revs", default="", help="e.g. 0-15: also create missing rev slots (name template derived from first hit)")
    p.set_defaults(func=lambda a: run_replace(a))

    p = sub.add_parser("boot-replace")
    p.add_argument("file"); p.add_argument("--pattern", required=True)
    p.add_argument("--dtb", required=True); p.add_argument("-o", "--out", required=True)
    p.add_argument("--fill-revs", default="")
    p.set_defaults(func=lambda a: run_boot_replace(a))

    args = ap.parse_args()
    args.func(args)


def _range(spec):
    if not spec:
        return None
    lo, hi = spec.split("-")
    return int(lo, 0), int(hi, 0)


def run_replace(args):
    data = open(args.file, "rb").read()
    dtb = open(args.dtb, "rb").read()
    if dtb[:4] != FDT_MAGIC:
        raise SystemExit(f"{args.dtb} is not a DTB")
    out = replace_in_rcs(data, args.pattern, dtb, _range(args.fill_revs))
    open(args.out, "wb").write(out)


def run_boot_replace(args):
    boot = open(args.file, "rb").read()
    dtb = open(args.dtb, "rb").read()
    if dtb[:4] != FDT_MAGIC:
        raise SystemExit(f"{args.dtb} is not a DTB")
    out = boot_replace(boot, args.pattern, dtb, _range(args.fill_revs))
    open(args.out, "wb").write(out)


if __name__ == "__main__":
    main()
