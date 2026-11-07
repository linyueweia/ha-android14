#!/usr/bin/env python3
"""Rockchip Android boot.img v1/v2: recompute the id[20] SHA1 that u-boot checks.

WHY THIS EXISTS
  RK u-boot with CONFIG_ANDROID_BOOT_IMAGE_HASH verifies boot.img before running it.
  The expected digest lives in the AOSP v2 header's `id[8]` field at offset 0x240 --
  Rockchip reuses those 32 bytes.  That digest covers kernel/ramdisk/second (and, for
  v1/v2, recovery_dtbo/dtb) *plus* each segment's 4-byte LE size.

  So the moment you replace the DTB inside the RSCE second stage (rsce_tool.py
  boot-replace) or patch super/boot content, the digest no longer matches and u-boot
  stops with:

      Hash from header: 0xef77b76c...
      Hash real:        0x757de090...
      Failed to load android image
      AVB verify failed / Android boot failed, error -1

  It looks like a loader/DDR failure and falls back to eMMC boot.scr, but it is these
  20 bytes.  Note `OK_NOT_SIGNED` is a *pass*, not an error; don't chase AVB -- the
  FriendlyElec packages ship an empty vbmeta (flags=0x2, VERIFICATION_DISABLED) so
  there is nothing to re-sign.

ALGORITHM (verified line by line against rockchip-toybrick/u-boot next-dev,
common/image-android.c: android_image_separate() sets ctx.length, image_load() then
does `crypto_sha_update(data, datasz); crypto_sha_update(&datasz, 4)` per segment)

  for seg in (kernel, ramdisk, second[, recovery_dtbo[, dtb]]):
      sha1.update(data[:seg_size])
      sha1.update(u32le(seg_size))

  * the image header page is NOT hashed: image_load() does `ramdst += pgsz;
    datasz -= pgsz` for IMG_KERNEL.  => rewriting id never changes the result, one
    pass converges, no iteration.
  * a zero-size segment still contributes its 4-byte size (the code jumps to
    `crypto_calc` and updates sizesz bytes anyway).
  * these 4-byte fields are little-endian, exactly the `datasz` register value.

USAGE
  rk_boot_id.py check <boot.img>              # exit 0 = MATCH, 1 = MISMATCH
  rk_boot_id.py fix   <boot.img> -o out.img   # rewrite id[20], verify by re-reading
  rk_boot_id.py fix   <boot.img> --inplace --yes
  rk_boot_id.py info  <boot.img>              # header + segment table, no hashing

  # typical pipeline step, right after the DTB injection:
  python3 tools/rk_boot_id.py fix  boot.img -o boot.img.new && mv boot.img.new boot.img
  python3 tools/rk_boot_id.py check boot.img          # gate: must print MATCH

  # on-device, only these 20 bytes:
  dd if=boot.img of=/dev/mmcblk1p9 bs=1 seek=576 count=20 conv=notrunc

DISCIPLINE
  A write you did not read back is not a verified write.  `fix` re-parses the output
  from disk and recomputes; it refuses to claim success otherwise.  When writing to a
  partition, dump the whole partition back and re-run `check` on it -- reading back 20
  bytes only proves the bytes landed, not that the algorithm is right.
"""

import struct
import sys
import hashlib

MAGIC = b"ANDROID!"
ID_OFF = 0x240
ID_LEN = 20
HDR_V0_LEN = 0x660          # recovery_dtbo_size starts here
OFF_RECOVERY_DTBO_SIZE = 0x660
OFF_HEADER_SIZE = 0x66C
OFF_DTB_SIZE = 0x670


def align(n, p):
    return ((n + p - 1) // p) * p


def parse(img):
    if len(img) < 0x680:
        raise SystemExit("file too small for a boot v1/v2 header: %d bytes" % len(img))
    if img[:8] != MAGIC:
        raise SystemExit("bad magic %r (expected ANDROID!)" % img[:8])
    kernel_size, ramdisk_size, second_size = struct.unpack_from("<III", img, 0x08)[0], \
        struct.unpack_from("<I", img, 0x10)[0], struct.unpack_from("<I", img, 0x18)[0]
    page_size = struct.unpack_from("<I", img, 0x24)[0]
    header_version = struct.unpack_from("<I", img, 0x28)[0]
    if page_size < 512 or page_size > 65536 or (page_size & (page_size - 1)):
        raise SystemExit("implausible page_size %d" % page_size)
    if header_version > 2:
        raise SystemExit("header_version %d: v3/v4 use a different layout, unsupported"
                         % header_version)
    rec_size = struct.unpack_from("<I", img, OFF_RECOVERY_DTBO_SIZE)[0] if header_version >= 1 else 0
    dtb_size = struct.unpack_from("<I", img, OFF_DTB_SIZE)[0] if header_version >= 2 else 0
    return {
        "kernel_size": kernel_size, "ramdisk_size": ramdisk_size, "second_size": second_size,
        "page_size": page_size, "header_version": header_version,
        "recovery_dtbo_size": rec_size, "dtb_size": dtb_size,
        "cmdline": img[0x40:0x240].split(b"\0")[0].decode("utf-8", "replace"),
        "id": img[ID_OFF:ID_OFF + 32],
    }


def segments(img, h):
    """(name, data, size_field) in the exact order u-boot feeds the hash."""
    p = h["page_size"]
    off = p
    out = []
    for name, key in (("kernel", "kernel_size"), ("ramdisk", "ramdisk_size"),
                      ("second", "second_size")):
        size = h[key]
        out.append((name, off, size))
        off += align(size, p)
    if h["header_version"] >= 1:
        out.append(("recovery_dtbo", off, h["recovery_dtbo_size"]))
        off += align(h["recovery_dtbo_size"], p)
    if h["header_version"] >= 2:
        out.append(("dtb", off, h["dtb_size"]))
        off += align(h["dtb_size"], p)
    for name, o, size in out:
        if o + size > len(img):
            raise SystemExit("segment %s @%#x+%d exceeds file size %d (truncated image?)"
                             % (name, o, size, len(img)))
    return out


def digest(img, h):
    d = hashlib.sha1()
    for name, off, size in segments(img, h):
        d.update(img[off:off + size])
        d.update(struct.pack("<I", size))
    return d.digest()[:ID_LEN]


def cmd_info(img, h):
    print("magic         ANDROID!  (page_size=%d, header_version=%d)"
          % (h["page_size"], h["header_version"]))
    print("kernel_size   %d" % h["kernel_size"])
    print("ramdisk_size  %d" % h["ramdisk_size"])
    print("second_size   %d" % h["second_size"])
    if h["header_version"] >= 1:
        print("recovery_dtbo %d" % h["recovery_dtbo_size"])
    if h["header_version"] >= 2:
        print("dtb_size      %d" % h["dtb_size"])
    print("cmdline       %s" % h["cmdline"])
    print("id@0x240      %s" % h["id"][:ID_LEN].hex())
    hdr_vers = h["header_version"]
    total = sum(s for _, _, s in segments(img, h)) + 4 * len(segments(img, h))
    print("hash coverage %d bytes over %d segment(s)" % (total, len(segments(img, h))))
    print("file          %d bytes, %d page(s)" % (len(img), (len(img) + h["page_size"] - 1) // h["page_size"]))


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    cmd, src = sys.argv[1], sys.argv[2]
    img = open(src, "rb").read()
    h = parse(img)

    if cmd == "info":
        cmd_info(img, h)
        return 0

    if cmd == "check":
        real = digest(img, h)
        hdr = h["id"][:ID_LEN]
        cmd_info(img, h)
        print("")
        print("Hash from header %s" % hdr.hex())
        print("Hash real        %s" % real.hex())
        if real == hdr:
            print("MATCH  -> u-boot will accept this boot.img")
            return 0
        print("MISMATCH -> u-boot will print 'Failed to load android image' and abort")
        print("            fix with: %s fix %s -o boot.fixed.img" % (sys.argv[0], src))
        return 1

    if cmd == "fix":
        out, inplace, yes = None, False, "--yes" in sys.argv
        for i, a in enumerate(sys.argv):
            if a == "-o":
                out = sys.argv[i + 1]
            if a == "--inplace":
                inplace = True
        if inplace and not yes:
            raise SystemExit("--inplace rewrites the input in place; pass --yes to confirm")
        if not out and not inplace:
            raise SystemExit("give -o OUT or --inplace --yes")

        real = digest(img, h)
        old = h["id"][:ID_LEN]
        if real == old:
            print("already MATCH, nothing to do (%s)" % real.hex())
            if out:
                open(out, "wb").write(img)
            return 0
        buf = bytearray(img)
        buf[ID_OFF:ID_OFF + ID_LEN] = real
        blob = bytes(buf)
        assert len(blob) == len(img), "size changed: %d -> %d" % (len(img), len(blob))

        dst = src if inplace else out
        open(dst, "wb").write(blob)

        # read back from disk and recompute -- independent of the buffer we wrote
        back = open(dst, "rb").read()
        hb = parse(back)
        again = digest(back, hb)
        changed = sum(1 for a, b in zip(img, back) if a != b)
        print("header id  %s -> %s" % (old.hex(), hb["id"][:ID_LEN].hex()))
        print("recomputed %s" % again.hex())
        print("wrote %s: size %d unchanged, %d byte(s) changed" % (dst, len(back), changed))
        if again != hb["id"][:ID_LEN]:
            raise SystemExit("READ-BACK FAILED: recomputed digest != stored id")
        # 用「读回文件的 digest == 写入的 id」判对错，不能用「必须改 20 字节」：
        # 新 SHA1 与原来的占位值（0x11223344…）完全可能有个别字节相同，
        # 实测就出现过 19/20 —— 那不是写坏，只是那一字节本来就不需要变。
        # 真正异常的是「一个字节都没变」（说明写到了别处）或改到了 id 之外。
        if not 0 < changed <= ID_LEN:
            raise SystemExit("READ-BACK FAILED: %d byte(s) changed, expect 1..%d"
                             % (changed, ID_LEN))
        print("MATCH (verified by re-reading the written file)")
        return 0

    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
