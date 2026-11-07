#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把友善 Android 14 的「分区镜像包」拼成一张能写进 SD 卡的整盘 .img。

为什么要自己拼：
    静 package（android-14-images-rk3588.tgz）里是一堆分区镜像 + parameter.txt，
    配 upgrade_tool 线刷 eMMC 用的。H88K 的 eMMC 上装着 U-Boot（它负责引导 NVMe
    上的 fnOS），线刷会把那套东西铲掉，fnOS 就起不来了。
    走 SD 卡则完全不碰 eMMC：卡上有自己的 idbloader + uboot_a，Rockchip BootROM
    扫到卡就优先从卡启动。

做的事：
    1. 按 parameter.txt 的 mtdparts 生成 GPT（分区名 = 括号里的名字）
    2. idbloader.img 写到 LBA 64（Rockchip 固定的 loader 偏移）
    3. 各分区镜像按 start_lba 落位；boot_a/boot_b 用我们改过的 H88K boot.img
    4. userdata 留空白（Android 首次启动自己 mkfs）
    5. 全程稀疏写入，写完回读校验每个分区的首尾 4KB

用法：
    python make_sd_image.py --size-gb 8 -o out/h88k-a14-sd.img
    python make_sd_image.py --size-gb 8 --boot build/boot-rk3588-h88k-a14.img -o ...
"""
import argparse
import os
import re
import struct
import sys
import zlib
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
IMG_DIR = os.path.join(REPO, "extracted", "android14")

SECTOR = 512
LINUX_FS = bytes.fromhex("af3dc60f838472478e793d69d8477de4")  # 0FC63DAF-8483-4772-8E79-3D69D8477DE4
LOADER_LBA = 64          # Rockchip: idbloader 从 32KB 处开始

# parameter.txt -> [(name, start_lba, n_sectors or None for grow)]
PART_RE = re.compile(r"(0x[0-9a-fA-F]+|-)(?:@(0x[0-9a-fA-F]+))?\(([^)]+)\)")


def parse_parameter(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"CMDLINE:\s*mtdparts=rk29xxnand:(.+)$", txt, re.M)
    if not m:
        raise SystemExit("parameter.txt 里找不到 mtdparts")
    parts = []
    for cnt, start, name in PART_RE.findall(m.group(1)):
        name = name.split(":")[0]
        grow = cnt == "-"
        n = None if grow else int(cnt, 16)
        s = None if start == "-" else int(start, 16)
        parts.append((name, s, n, grow))
    return parts


def build_gpt(total_lba, parts):
    """返回 (protective_mbr, pri_header, pri_entries, bak_entries, bak_header)"""
    first_usable, last_usable = 34, total_lba - 34
    disk_guid = uuid.uuid4().bytes

    ents = bytearray(128 * 128)
    for i, (name, start, n, grow) in enumerate(parts):
        e = i * 128
        ents[e:e + 16] = LINUX_FS
        ents[e + 16:e + 32] = uuid.uuid4().bytes
        s = start
        last = (total_lba - 34) if grow else (start + n - 1)
        struct.pack_into("<QQ", ents, e + 32, s, last)
        nm = name.encode("utf-16-le")[:72]
        ents[e + 56:e + 56 + len(nm)] = nm
    ents_crc = zlib.crc32(bytes(ents)) & 0xFFFFFFFF

    def header(my_lba, alt_lba, ent_lba):
        h = bytearray(92)
        h[0:8] = b"EFI PART"
        struct.pack_into("<I", h, 8, 0x00010000)      # revision
        struct.pack_into("<I", h, 12, 92)             # header size
        struct.pack_into("<Q", h, 24, my_lba)
        struct.pack_into("<Q", h, 32, alt_lba)
        struct.pack_into("<Q", h, 40, first_usable)
        struct.pack_into("<Q", h, 48, last_usable)
        h[56:72] = disk_guid
        struct.pack_into("<Q", h, 72, ent_lba)
        struct.pack_into("<I", h, 80, 128)            # num entries
        struct.pack_into("<I", h, 84, 128)            # entry size
        struct.pack_into("<I", h, 88, ents_crc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h) + b"\x00" * (SECTOR - 92)

    mbr = bytearray(SECTOR)
    # 16 字节 MBR 分区项：status, CHS start(3), type=0xEE, CHS end(3), start LBA, size(sectors)
    mbr[446:462] = struct.pack("<BBBBBBBBII",
                               0x00, 0x00, 0x02, 0x00,      # not bootable, dummy CHS
                               0xEE, 0xFF, 0xFF, 0xFF,      # GPT protective
                               1, min(total_lba - 1, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"

    pri_h = header(1, total_lba - 1, 2)
    pri_e = bytes(ents)
    bak_e = bytes(ents)
    bak_h = header(total_lba - 1, 1, total_lba - 33)
    return bytes(mbr), pri_h, pri_e, bak_e, bak_h


def write_at(f, off, data, label=None):
    f.seek(off)
    f.write(data)


# ---- Android sparse image（super.img 就是这种）----
SPARSE_MAGIC = 0xED26FF3A
CHUNK_RAW, CHUNK_FILL, CHUNK_DONT_CARE, CHUNK_CRC32 = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4


def is_sparse(path):
    with open(path, "rb") as f:
        return struct.unpack("<I", f.read(4))[0] == SPARSE_MAGIC


def sparse_info(path):
    with open(path, "rb") as f:
        h = f.read(28)
    _, _, _, fhs, chs, blk, tot_blk, tot_chk, _ = struct.unpack("<IHHHHIIII", h)
    return dict(blk=blk, tot_blk=tot_blk, tot_chk=tot_chk, fhs=fhs, chs=chs,
                expanded=tot_blk * blk)


def write_sparse_at(f_out, off, path):
    """流式展开 sparse 镜像，直接落到镜像文件的 off 处。返回展开字节数。"""
    info = sparse_info(path)
    blk, chs = info["blk"], info["chs"]
    written = 0
    with open(path, "rb") as src:
        src.seek(info["fhs"])
        while True:
            h = src.read(12)
            if len(h) < 12:
                break
            ctype, _, chunk_sz, total_sz = struct.unpack("<HHII", h)
            payload = total_sz - chs
            nbytes = chunk_sz * blk

            if ctype == CHUNK_RAW:
                left = payload
                while left:
                    buf = src.read(min(left, 8 << 20))
                    if not buf:
                        raise SystemExit("sparse RAW chunk 提前结束")
                    f_out.seek(off + written)
                    f_out.write(buf)
                    written += len(buf)
                    left -= len(buf)
            elif ctype == CHUNK_FILL:
                fill = src.read(4)
                src.seek(payload - 4, 1)
                unit = fill * 1024                    # 4 KiB
                left = nbytes
                while left:
                    n = min(left, len(unit))
                    f_out.seek(off + written)
                    f_out.write(unit[:n])
                    written += n
                    left -= n
            elif ctype == CHUNK_DONT_CARE:
                written += nbytes                     # 镜像文件本来就是 0，跳过
            elif ctype == CHUNK_CRC32:
                src.seek(payload, 1)
            else:
                raise SystemExit(f"未知 sparse chunk 类型 0x{ctype:04x}")
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-gb", type=float, default=8.0)
    ap.add_argument("--boot", default=os.path.join(REPO, "build", "boot-rk3588-h88k-a14.img"))
    ap.add_argument("--param", default=os.path.join(IMG_DIR, "parameter.txt"))
    ap.add_argument("--img-dir", default=IMG_DIR)
    ap.add_argument("--userdata-gb", type=float, default=0,
                    help="0 = 吃满剩余空间")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()

    parts = parse_parameter(a.param)
    print("parameter 布局：")
    for name, s, n, g in parts:
        print(f"  {name:14s} start=0x{s:08x} ({(s*SECTOR)/2**20:8.1f} MB) "
              f"{'grow' if g else f'{(n*SECTOR)/2**20:8.1f} MB'}")

    total_lba = int(a.size_gb * 2**30 // SECTOR)
    total_lba &= ~63                     # 对齐到 32KB
    if a.userdata_gb:
        ud = next(p for p in parts if p[3])
        total_lba = ud[1] + int(a.userdata_gb * 2**30 // SECTOR) + 34
    print(f"\n镜像大小 {total_lba*SECTOR/2**30:.2f} GiB ({total_lba} sectors)")

    # grow 分区的 start 必须重算（parameter 里的 0x778400 是按 4GB userdata 算的）
    if os.path.exists(a.out):
        os.remove(a.out)
    with open(a.out, "wb") as f:
        f.truncate(total_lba * SECTOR)

    mbr, pri_h, pri_e, bak_e, bak_h = build_gpt(total_lba, parts)
    with open(a.out, "r+b") as f:
        write_at(f, 0, mbr)
        write_at(f, SECTOR, pri_h)
        write_at(f, 2 * SECTOR, pri_e)
        write_at(f, (total_lba - 33) * SECTOR, bak_e)
        write_at(f, (total_lba - 1) * SECTOR, bak_h)

        # idbloader（SPL）固定写 32KB
        idb = open(os.path.join(a.img_dir, "idbloader.img"), "rb").read()
        write_at(f, LOADER_LBA * SECTOR, idb)
        print(f"idbloader.img  {len(idb):>10,} B @ LBA {LOADER_LBA}")

        def solid(name):
            p = os.path.join(a.img_dir, name)
            return open(p, "rb").read() if os.path.exists(p) else None

        uboot = solid("uboot.img")
        misc = solid("misc.img")
        dtbo = solid("dtbo.img")
        vbmeta = solid("vbmeta.img")
        baseparam = solid("baseparameter.img")
        boot = open(a.boot, "rb").read()

        plan = {
            "uboot_a": uboot, "uboot_b": uboot,
            "misc": misc,
            "dtbo_a": dtbo, "dtbo_b": dtbo,
            "vbmeta_a": vbmeta, "vbmeta_b": vbmeta,
            "boot_a": boot, "boot_b": boot,
            "baseparameter": baseparam,
        }
        by_name = {p[0]: p for p in parts}
        for name, blob in plan.items():
            if blob is None:
                continue
            _, start, n, grow = by_name[name]
            cap = (total_lba - 34 - start) * SECTOR if grow else n * SECTOR
            if len(blob) > cap:
                raise SystemExit(f"{name}: {len(blob):,} B 超过分区容量 {cap:,} B")
            write_at(f, start * SECTOR, blob)
            print(f"{name:14s} {len(blob):>10,} B @ 0x{start*SECTOR:08x}  "
                  f"({len(blob)/cap*100:5.1f}% of {(cap)/2**20:.0f} MB)")

        # super 是 Android sparse，必须展开成 3.125 GiB 的裸分区内容，
        # 否则 Android 读到的是 sparse chunk 头而不是 lp metadata。
        sup = os.path.join(a.img_dir, "super.img")
        if os.path.exists(sup):
            _, start, n, grow = by_name["super"]
            cap = n * SECTOR
            if is_sparse(sup):
                si = sparse_info(sup)
                print(f"super          sparse 展开 {si['expanded']:,} B "
                      f"({si['tot_chk']} chunks) @ 0x{start*SECTOR:08x}")
                if si["expanded"] > cap:
                    raise SystemExit(f"super 展开后 {si['expanded']:,} B 超过分区 {cap:,} B")
                got = write_sparse_at(f, start * SECTOR, sup)
                print(f"               实际落盘 {got:,} B "
                      f"({got/si['expanded']*100:.1f}% 非 DONT_CARE)")
            else:
                blob = open(sup, "rb").read()
                if len(blob) > cap:
                    raise SystemExit(f"super: {len(blob):,} B 超过分区容量 {cap:,} B")
                write_at(f, start * SECTOR, blob)
                print(f"super          raw {len(blob):,} B @ 0x{start*SECTOR:08x}")

    # ---------- 回读校验 ----------
    print("\n回读校验：")
    ok = True
    with open(a.out, "rb") as f:
        f.seek(SECTOR)
        h = f.read(92)
        hdr_for_crc = bytearray(h)
        struct.pack_into("<I", hdr_for_crc, 16, 0)     # 算 CRC 时该字段按 0 处理
        good = h[:8] == b"EFI PART" and (zlib.crc32(bytes(hdr_for_crc)) & 0xFFFFFFFF) == struct.unpack_from("<I", h, 16)[0]
        print("  GPT 主头 CRC", "OK" if good else "BAD")
        ok &= good

        f.seek((total_lba - 1) * SECTOR)
        bh = f.read(92)
        bh_crc = bytearray(bh)
        struct.pack_into("<I", bh_crc, 16, 0)
        good = bh[:8] == b"EFI PART" and (zlib.crc32(bytes(bh_crc)) & 0xFFFFFFFF) == struct.unpack_from("<I", bh, 16)[0]
        print("  GPT 备份头 CRC", "OK" if good else "BAD")
        ok &= good

        f.seek(2 * SECTOR)
        ents = f.read(128 * 128)
        crc = zlib.crc32(ents) & 0xFFFFFFFF
        good = crc == struct.unpack_from("<I", h, 88)[0]
        print(f"  分区表 CRC 0x{crc:08x}", "OK" if good else "BAD")
        ok &= good

        seen = []
        for i in range(128):
            e = ents[i * 128:(i + 1) * 128]
            if e[:16] == b"\x00" * 16:
                continue
            s, en = struct.unpack_from("<QQ", e, 32)
            nm = e[56:128].decode("utf-16-le").rstrip("\0")
            seen.append(f"{nm}[{s}..{en}]")
        print("  分区 " + ", ".join(seen))

        f.seek(LOADER_LBA * SECTOR)
        got = f.read(len(idb))
        good = got == idb
        print(f"  idbloader ({len(idb):,} B)", "OK" if good else "BAD")
        ok &= good

        for name, blob in plan.items():
            if blob is None:
                continue
            start = by_name[name][1]
            f.seek(start * SECTOR)
            if len(blob) <= 4096:
                good = f.read(len(blob)) == blob
            else:
                head = f.read(4096)
                f.seek(start * SECTOR + len(blob) - 4096)
                tail = f.read(4096)
                good = head == blob[:4096] and tail == blob[-4096:]
            if not good:
                print(f"  {name} 回读不一致")
            ok &= good

        # super：只验开头是 lp metadata（0x414C5030 "LP"）且不是 sparse 头
        _, sup_start, _, _ = by_name["super"]
        f.seek(sup_start * SECTOR)
        head = f.read(8)
        lp = struct.unpack("<I", head[:4])[0]
        good = lp != SPARSE_MAGIC
        print(f"  super 头 {head.hex()} {'（未展开的 sparse！）' if not good else 'OK（非 sparse）'}")
        ok &= good

        print("  所有分区首尾 4KB", "OK" if ok else "BAD")

    size = os.path.getsize(a.out)
    print(f"\n完成：{a.out}  {size/2**30:.2f} GiB")
    print("写卡：balenaEtcher / Rufus / Win32DiskImager 均可，写完不需要再改任何东西。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
