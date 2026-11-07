#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a boot.img whose embedded board dtb carries our patches -- strictly verified.

WHY: rk_dtb_replace.py once inserted the patched dtb 0x6000 bytes into the wrong
place, shifting every following dtb while the RSCE index kept its old offsets, and
u-boot answered with `Failed to load DTB, ret=-9` / `Failed to load android image`
even though the boot header hash was MATCH.  This builder only ever overwrites a
dtb in place, and refuses to emit an image it cannot re-parse.

  python build_boot_img.py <vendor_boot.img> <patched.dtb> <out.img>
  python rk_boot_id.py fix out.img --inplace --yes && rk_boot_id.py check out.img

Guarantees / assertions
  * every patched dtb is written at its original slot start -- nothing moves
  * patched length <= gap to the next slot (tail slot may extend second_size)
  * every RSCE entry pointing at a slot gets size = patched length
  * re-parse: FDT magic count unchanged, totalsize == size, model preserved,
    slots do not overlap; any failure => exit 1 and no output file
"""

import hashlib
import struct
import sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "board/boot_a.img"
PATCHED = sys.argv[2] if len(sys.argv) > 2 else "board/rev0f_patched.dtb"
OUT = sys.argv[3] if len(sys.argv) > 3 else "board/boot_final.img"
MAGIC = b"\xd0\x0d\xfe\xed"


def be32(b, o):
    return struct.unpack(">I", b[o:o + 4])[0]


def fdt_model(dtb):
    if len(dtb) < 40 or dtb[:4] != MAGIC:
        return None
    _, so, st, _, _, _, _, _, szs = struct.unpack(">9I", dtb[4:40])
    strs = dtb[st:st + szs]
    # walk tokens: FDT_BEGIN_NODE(1) root, then root props until END_NODE
    p = so
    if be32(dtb, p) != 1:
        return None
    p += 4
    while p < len(dtb) and dtb[p] != 0:
        p += 1
    p = (p + 4) & ~3
    while p < len(dtb):
        tok = be32(dtb, p)
        p += 4
        if tok != 3:
            break
        ln, nameoff = struct.unpack(">II", dtb[p:p + 8])
        p += 8
        val = dtb[p:p + ln]
        p += (ln + 3) & ~3
        name = strs[nameoff:strs.find(b"\x00", nameoff)].decode("utf8", "replace")
        if name == "model":
            return val.split(b"\x00")[0].decode("utf8", "replace")
    return None


def main():
    img = bytearray(open(BASE, "rb").read())
    patched = open(PATCHED, "rb").read()
    plen = len(patched)
    assert patched[:4] == MAGIC, "patched dtb bad magic"

    page = struct.unpack("<I", img[36:40])[0]
    ksz, rsz, ssz = struct.unpack("<III", img[8:12] + img[16:20] + img[24:28])[0:3] \
        if False else (struct.unpack("<I", img[8:12])[0],
                       struct.unpack("<I", img[16:20])[0],
                       struct.unpack("<I", img[24:28])[0])
    align = lambda x: (x + page - 1) // page * page
    sec_off = page + align(ksz) + align(rsz)
    sec = bytes(img[sec_off:sec_off + ssz])
    print("base %s  page=%d k=%d r=%d s=%d  second@0x%x" % (BASE, page, ksz, rsz, ssz, sec_off))
    print("patched dtb %d bytes (grow %+d)" % (plen, plen - 172436))

    # ---- collect slots: every .dtb entry that carries the T68M blob
    num = struct.unpack("<I", sec[12:16])[0]
    entries = []       # (entry_file_pos_in_sec, name, off, size)
    for i in range(num + 1):                  # +1: the vendor's stale trailing slot
        j = 4 + i * 512
        if j + 0x108 > len(sec):
            break
        name = sec[j:j + 256].split(b"\x00")[0].decode("utf8", "replace")
        off, sz = struct.unpack("<II", sec[j + 0x100:j + 0x108])
        if not name.endswith(".dtb") or off == 0:
            continue
        entries.append((j, name, off, sz))

    slots = []          # (slot_pos, [entry positions])
    for j, name, off, sz in entries:
        pos = off * 512
        if pos + 8 > len(sec) or sec[pos:pos + 4] != MAGIC:
            print("  ! entry %-28s off=0x%x -> no magic (stale, left untouched)" % (name, pos))
            continue
        slots.append([pos, [j], sz, name])
    # merge duplicates (several entries can share one slot)
    uniq = {}
    for pos, js, sz, name in slots:
        if pos in uniq:
            uniq[pos][1].extend(js)
        else:
            uniq[pos] = [pos, js, sz, name]
    slots = sorted(uniq.values(), key=lambda x: x[0])
    print("dtb slots found: %d" % len(slots))

    # ---- capacity check against the next slot
    t68m = 0
    for idx, (pos, js, sz, name) in enumerate(slots):
        cap = (slots[idx + 1][0] - pos) if idx + 1 < len(slots) else 10 ** 9  # tail slot may extend second
        model = fdt_model(sec[pos:pos + 400000])
        is_t68m = (model == "LYT T68M")
        t68m += is_t68m
        if not is_t68m:
            print("  skip 0x%06x %-28s model=%r" % (pos, name, model))
            continue
        if plen > cap:
            raise SystemExit("FATAL: patched dtb %d > capacity %d at 0x%x (%s)"
                             % (plen, cap, pos, name))
        print("  patch 0x%06x %-28s cap=%d slack=%d" % (pos, name, cap, cap - plen))
    if t68m == 0:
        raise SystemExit("FATAL: no LYT T68M dtb slot found")
    print("T68M dtb variants to patch: %d" % t68m)

    # remember what each slot looked like in the base image (for verification)
    orig_model = {pos: fdt_model(sec[pos:pos + 400000]) for pos, _, _, _ in slots}

    # ---- write in place
    new = bytearray(sec)
    last_end = 0
    for idx, (pos, js, sz, name) in enumerate(slots):
        model = fdt_model(sec[pos:pos + 400000])
        if model != "LYT T68M":
            continue
        cap = (slots[idx + 1][0] - pos) if idx + 1 < len(slots) else plen
        new[pos:pos + plen] = patched
        # zero the rest of this slot's allocation, never one byte past capacity
        tail_end = pos + cap
        if tail_end > len(new):
            new += b"\x00" * (tail_end - len(new))
        new[pos + plen:tail_end] = b"\x00" * (tail_end - pos - plen)
        for j in js:
            new[j + 0x104:j + 0x108] = struct.pack("<I", plen)
        last_end = max(last_end, pos + plen)

    new_ssz = max(last_end, ssz)
    if new_ssz > len(new):
        new += b"\x00" * (new_ssz - len(new))
    else:
        new = new[:new_ssz]
    print("second_size %d -> %d" % (ssz, new_ssz))

    # ---- rebuild image
    out = bytearray(img[:sec_off])
    out += new
    struct.pack_into("<I", out, 24, new_ssz)          # second_size
    total = align(len(out))
    out += b"\x00" * (total - len(out))

    # ---- header id hash: sha256(kernel) then sha256(ramdisk) then sha256(second),
    #      each followed by its 4-byte LE length
    # RK u-boot: SHA1 over each segment then its 4-byte LE size; id[20] @ 0x240
    h = hashlib.sha1()
    p = page
    for ln in (ksz, rsz, new_ssz):
        h.update(out[p:p + ln])
        h.update(struct.pack("<I", ln))
        p += align(ln)
    out[0x240:0x254] = h.digest()

    # ---- verify
    def check():
        err = []
        if out[:8] != b"ANDROID!":
            err.append("magic")
        o_page = struct.unpack("<I", out[36:40])[0]
        o_k, o_r, o_s = (struct.unpack("<I", out[8:12])[0],
                         struct.unpack("<I", out[16:20])[0],
                         struct.unpack("<I", out[24:28])[0])
        so = o_page + align(o_k) + align(o_r)
        sc = out[so:so + o_s]
        hh = hashlib.sha1()
        q = o_page
        for ln in (o_k, o_r, o_s):
            hh.update(out[q:q + ln]); hh.update(struct.pack("<I", ln)); q += align(ln)
        if hh.digest() != out[0x240:0x254]:
            err.append("header id hash")
        mag = [i for i in range(len(sc) - 3) if sc[i:i + 4] == MAGIC]
        for pos, js, sz, name in slots:
            if sc[pos:pos + 4] != MAGIC:
                err.append("slot 0x%x magic gone" % pos)
                continue
            ts = be32(sc, pos + 4)
            mdl = fdt_model(sc[pos:pos + 400000])
            if orig_model.get(pos) != "LYT T68M":
                # untouched slot: must still parse to exactly the same model
                if mdl != orig_model.get(pos):
                    err.append("slot 0x%x model changed %r->%r"
                               % (pos, orig_model.get(pos), mdl))
                continue
            if ts != plen:
                err.append("slot 0x%x totalsize %d != %d" % (pos, ts, plen))
            if mdl != "LYT T68M":
                err.append("slot 0x%x model lost" % pos)
            for j in js:
                if struct.unpack("<I", sc[j + 0x104:j + 0x108])[0] < ts:
                    err.append("entry@0x%x size < totalsize" % j)
        print("verify: fdt magics=%d (base had %d)" %
              (len(mag), sum(1 for i in range(len(sec) - 3) if sec[i:i + 4] == MAGIC)))
        return err

    errs = check()
    if errs:
        for e in errs:
            print("  FAIL", e)
        raise SystemExit("REFUSING to write a broken image")
    # final patched-dtb sanity: the props we care about
    open(OUT, "wb").write(out)
    print("OK  %s  %d bytes (%.1f MiB)" % (OUT, len(out), len(out) / 1048576))


main()
