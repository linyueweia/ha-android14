#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把两个 DTB 拆成「节点 → 属性 → 原始字节」逐项对齐，输出**语义级**差异。

为什么不直接用 dtc -I dtb -O dts | diff：
  1. 文本 diff 会把 phandle 编号偏移当成差异（节点集合一变，全表编号就挪位，
     几百行假差异），真问题被淹掉。
  2. 它分不清「板级本来就该不一样」（内存大小、引脚、regulator、LED、PHY 地址）
     和「同一个 SoC IP 的开关竟然不同」（NPU/GPU/VPU/vop/dfi），后者才是要命的。

本工具做的对齐：
  - phandle 属性本身**不比较**（编号是编译器分配的）
  - 引用类属性（`*-supply`、`iommus`、`remote-endpoint`、`clocks`、`power-domains`、
    `rockchip,srv`、`phy-handle`…）先把每个 u32 解析成目标节点路径再比较，
    这样编号变了但指向同一个节点 = 一致

用法：
  python dtb_diff.py board.dtb base.dtb              # 语义差异
  python dtb_diff.py board.dtb base.dtb --only-ip    # 只看 SoC IP 节点
  python dtb_diff.py board.dtb base.dtb --raw        # 不解析 phandle（原始数字）
  python dtb_diff.py board.dtb base.dtb --summary    # 只统计
  python dtb_diff.py board.dtb base.dtb --node /npu@fde40000
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fdt_edit as F  # noqa: E402

REF_PROPS = (
    "-supply", "iommus", "remote-endpoint", "clocks", "clock-parents",
    "power-domains",
    "resets", "dmas", "phys", "phy-handle", "sound-dai", "memory-region",
    "nvmem-cells", "io-channels", "mboxes", "rockchip,srv", "rockchip,sram",
    "rockchip,grf", "rockchip,pmu", "-gpios", "-gpio",
    "pinctrl-", "rockchip,pins", "thermal-sensors", "operating-points-v2", "syscon",
    "rockchip,codec", "rockchip,display-subsystem",
)

# 这几个属性的值是**纯 phandle 列表**（没有紧跟的 specifier），每个 u32 都要解析。
# `pinctrl-0 = <&a &b &c>`、`xxx-supply = <&reg>`、`remote-endpoint = <&ep>` 都是这种。
# 其余（clocks/resets/iommus/*-gpios）是 `<&provider spec spec ...>` 交替，只解析下标 0。
ALL_PHANDLE = ("pinctrl-", "-supply", "remote-endpoint", "phy-handle")

IP_KEYS = ("npu", "gpu", "rkvdec", "rkvenc", "vdpu", "vepu", "rga", "iep",
           "jpeg", "vop", "hdmi", "iommu", "bus-npu", "mpp", "dfi", "rng",
           "crypto", "display-subsystem")


def load(path):
    tree = F.parse(open(path, "rb").read())
    flat = []
    F._walk(tree["root"], "", flat)
    nodes = {}
    ph2path = {}
    for p, n in flat:
        nodes[p] = {k.decode("latin1"): v for k, v in n.props}
        ph = nodes[p].get("phandle")
        if ph and len(ph) == 4:
            ph2path[struct.unpack(">I", ph)[0]] = p
    return nodes, ph2path


def norm(name, val, ph2path):
    """把引用属性里的 phandle 换成目标节点路径。

    注意：`clocks` / `resets` / `power-domains` / `*-gpios` / `iommus` 这类属性是
    `<&provider specifier> <&provider specifier> ...` 交替排列，specifier 是普通的
    整数（clock id、gpio 号、domain 号），**不能**也当 phandle 解析——
    否则一个恰好等于某个 phandle 的 clock id 会被解析成一个毫不相干的节点，
    造出一堆假差异。所以只解析偶数下标。
    """
    if name == "phandle":
        return None                      # 编号本身没有语义，不比
    if not any(k in name for k in REF_PROPS):
        return val
    if len(val) % 4:
        return val
    out = []
    allph = any(k in name for k in ALL_PHANDLE) and "pinctrl-names" not in name
    for i in range(0, len(val), 4):
        v = struct.unpack(">I", val[i:i + 4])[0]
        if name == "rockchip,pins":
            # <bank pin func &pcfg> —— 只有第 4 个 u32 可能是 phandle
            hit = (i % 16 == 12) and v in ph2path
        elif name == "nvmem-cells":
            # <&provider cell-id> 交替。cell-id 是按 provider 注册顺序现编的，
            # 两块板节点集合不同就会差 1（104 vs 103），但指向同一个 OTP cell。
            # 只比 provider，specifier 用 * 抹平。
            hit = (i % 8 == 0) and v in ph2path
            if not hit and i % 8 == 4:
                out.append("*")
                continue
        else:
            hit = (allph or i % 8 == 0) and v in ph2path
        out.append("<%s>" % ph2path[v] if hit else str(v))
    return " ".join(out)


def show(v):
    if isinstance(v, str):
        return v
    if v is None:
        return "<absent>"
    if len(v) == 0:
        return "<empty>"
    if v.endswith(b"\x00") and all(32 <= c < 127 or c == 0 for c in v[:-1]):
        return '"%s"' % v[:-1].decode("latin1")
    if len(v) <= 16:
        return "[" + ",".join("0x%02x" % c for c in v) + "]"
    return v[:24].hex() + ".."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("base")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--only-ip", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--node")
    a = ap.parse_args()

    Bn, Bp = load(a.board)
    Rn, Rp = load(a.base)
    print("board : %s  (%d nodes)" % (a.board, len(Bn)))
    print("base  : %s  (%d nodes)" % (a.base, len(Rn)))

    if a.node:
        for tag, N, P, other, OP in (("board", Bn, Bp, Rn, Rp), ("base", Rn, Rp, Bn, Bp)):
            for p in sorted(N):
                if p.endswith(a.node) or p == a.node:
                    print("\n[%s] %s" % (tag, p))
                    for k, v in sorted(N[p].items()):
                        if k == "phandle":
                            continue
                        ov = other.get(p, {}).get(k)
                        nv = norm(k, v, P) if not a.raw else v
                        on = norm(k, ov, OP) if (ov is not None and not a.raw) else ov
                        mark = "" if (p not in other or nv == on) else "   <<< DIFF"
                        print("   %-32s %s%s" % (k, show(nv), mark))
        return 0

    common = sorted(set(Bn) & set(Rn))
    same, diff = [], []
    for p in common:
        keys = set(Bn[p]) | set(Rn[p])
        bad = []
        for k in keys:
            vb, vr = Bn[p].get(k), Rn[p].get(k)
            if k == "phandle":
                continue
            nv = norm(k, vb, Bp) if vb is not None else None
            nr = norm(k, vr, Rp) if vr is not None else None
            if nv != nr:
                bad.append((k, nv, nr))
        if bad:
            diff.append((p, bad))
        else:
            same.append(p)
    only_b = sorted(set(Bn) - set(Rn))
    only_r = sorted(set(Rn) - set(Bn))

    print("\n共同节点 %d：语义完全一致 %d，有差异 %d"
          % (len(common), len(same), len(diff)))
    print("仅 board 有 %d 个，仅 base 有 %d 个" % (len(only_b), len(only_r)))

    if a.only_ip:
        diff = [(p, d) for p, d in diff if any(k in p for k in IP_KEYS)]
        only_b = [p for p in only_b if any(k in p for k in IP_KEYS)]
        only_r = [p for p in only_r if any(k in p for k in IP_KEYS)]

    if a.summary:
        print("\n有差异的节点：")
        for p, _ in diff:
            print("  " + p)
        return 0

    if diff:
        print("\n=== 共同节点语义差异（%d） ===" % len(diff))
        for p, bad in diff:
            print("\n%s" % p)
            for k, nv, nr in bad:
                print("   %-32s board=%-34s base=%s" % (k, show(nv), show(nr)))
    if only_b:
        print("\n=== 只在 board 里有（%d） ===" % len(only_b))
        for p in only_b:
            print("  " + p)
    if only_r:
        print("\n=== 只在 base 里有（%d） ===" % len(only_r))
        for p in only_r:
            print("  " + p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
