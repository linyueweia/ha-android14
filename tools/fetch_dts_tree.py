#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""递归抓取「只编一个 dtb」所必需的依赖闭包。

背景：友善的 SDK 在 gitea（112.124.9.243:3000），仓库几个 GB，
`git clone --filter=blob:none` 在 Azure runner 上跑了 31 分钟连 clone 都没完成。
但我们真正需要的只有：从板级 dts 出发能 #include 到的 dtsi + dt-bindings 头文件，
几十个文件、几百 KB。抓下来存进仓库，CI 侧就只用 gcc -E + dtc，秒级完成。

产出目录：
    vendor/dts-tree/arch/arm64/boot/dts/rockchip/*.dtsi
    vendor/dts-tree/include/dt-bindings/**/*.h

用法：
    python tools/fetch_dts_tree.py            # 增量补齐
    python tools/fetch_dts_tree.py --verify   # 只校验现有闭包有没有缺
"""
import concurrent.futures as cf
import os
import re
import sys
import time
import urllib.request
from threading import Lock

BASE = "http://112.124.9.243:3000/friendlyelec/rk35xx-android14/raw/branch/master/kernel-6.1/"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(REPO, "vendor", "dts-tree")
DTS_DIR = "arch/arm64/boot/dts/rockchip"
SEED_LOCAL = os.path.join(REPO, "dts", "rk3588-hlink-h88k-android.dts")

INC_RE = re.compile(rb'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)

_lock = Lock()
_done = {}
_queue = []
_failed = []


def local_path(rel):
    """rel 是相对 kernel-6.1 的路径。dtsi 在 dts/rockchip，头文件在 include/。"""
    if rel.startswith("include/"):
        return os.path.join(OUT, rel)
    return os.path.join(OUT, DTS_DIR, os.path.basename(rel))


def resolve(name, from_rel):
    """把 #include 的名字解析成相对 kernel-6.1 的路径。"""
    if name.startswith("dt-bindings/") or name.startswith("<dt-bindings"):
        return "include/" + name
    if name.startswith("arm64/"):
        return "arch/arm64/boot/dts/" + name[len("arm64/"):]
    if "/" in name:
        # 相对路径（例如 "dt-bindings/usb/pd.h" 已被上面接住，剩下的按 include/ 试）
        return "include/" + name
    return DTS_DIR + "/" + name


def fetch(rel, depth=0, retries=3):
    with _lock:
        if rel in _done:
            return
        _done[rel] = None

    dst = local_path(rel)
    data = None
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        data = open(dst, "rb").read()

    for a in range(retries):
        if data is not None:
            break
        url = BASE + rel
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "h88k-dtb-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read()
            break
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            if code == 404:
                with _lock:
                    _failed.append((rel, 404))
                print(f"  [404] {rel}")
                return
            print(f"  [retry {a+1}/{retries}] {rel}: {e}")
            time.sleep(3 * (a + 1))

    if data is None:
        with _lock:
            _failed.append((rel, "network"))
        return

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(data)

    for m in INC_RE.finditer(data):
        name = m.group(1).decode()
        if name.startswith("/"):          # 绝对路径，内核里也没有
            continue
        child = resolve(name, rel)
        with _lock:
            if child in _done:
                continue
        fetch(child, depth + 1)


def main():
    verify = "--verify" in sys.argv
    os.makedirs(OUT, exist_ok=True)

    # 种子里既有我们自己的 board dts（先落盘），也有三个顶层 dtsi
    roots = [
        "arch/arm64/boot/dts/rockchip/rk3588.dtsi",
        "arch/arm64/boot/dts/rockchip/rk3588-android.dtsi",
        "arch/arm64/boot/dts/rockchip/rk3588-rk806-single.dtsi",
    ]

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch, r) for r in roots]
        # 板级 dts 本身在仓库里，直接走一遍它的 include 列表
        if os.path.exists(SEED_LOCAL):
            src = open(SEED_LOCAL, "rb").read()
            for m in INC_RE.finditer(src):
                name = m.group(1).decode()
                if name.startswith("/"):
                    continue
                futs.append(ex.submit(fetch, resolve(name, DTS_DIR + "/seed.dts")))
        for f in cf.as_completed(futs):
            f.result()

    files = []
    for root, _, names in os.walk(OUT):
        for n in names:
            p = os.path.join(root, n)
            files.append((os.path.relpath(p, OUT).replace("\\", "/"), os.path.getsize(p)))

    total = sum(s for _, s in files)
    print(f"\n抓到 {len(files)} 个文件，合计 {total/1024:.1f} KB，用时 {time.time()-t0:.0f}s")
    if _failed:
        print(f"失败 {len(_failed)} 个：")
        for rel, why in _failed[:20]:
            print("   ", rel, why)

    # 闭包完整性：任何已抓到的文件里 #include 的东西，本地都得有
    missing = []
    for rel, _ in files:
        p = os.path.join(OUT, rel)
        if not rel.endswith((".dtsi", ".dts", ".h")):
            continue
        for m in INC_RE.finditer(open(p, "rb").read()):
            name = m.group(1).decode()
            if name.startswith("/"):
                continue
            child = resolve(name, rel)
            if not os.path.exists(local_path(child)):
                missing.append((rel, name, child))

    if missing:
        print(f"\n闭包不完整，缺 {len(missing)} 处：")
        for rel, name, child in missing[:25]:
            print(f"   {rel} -> {name}   (expect {child})")
        return 1
    print("闭包完整（所有 #include 都能本地解析）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
