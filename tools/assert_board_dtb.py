#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板级 DTB 断言：这份 dtb 上板后一定是「实机验证过的那份行为」。

为什么需要它
------------
`dtb_diff.py --only-ip` 只比 SoC IP 节点（IP_KEYS 里没有 usb），`dtb_features.py`
只看各 IP 的 status。两者都覆盖不到下面这些**板级**属性 —— 它们任何一个缺失或
被改回默认值，固件都能正常开机、内核日志零报错，只有用起来才发现不对：

  /hdmi-sound        mclk-fs != 128 或声卡名不是 rockchip,hdmi0 → 有画面没声音
  /usbdrd/usb@fcc…   dr_mode=otg 或带 extcon → USB3.0 口插什么都不枚举（鼠标不认）
  /vop@fe040000      缺 vop-supply → `_opp_set_regulators: no regulator (vop) found`

用法
----
  python tools/assert_board_dtb.py dist/rk3568-lyt-t68m-android.dtb
  python tools/assert_board_dtb.py built.dtb dist.dtb     # 多份逐个查
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dtb_diff as D  # noqa: E402

WANT_MODEL = "LYT T68M"


def _s(v):
    return None if v is None else v.rstrip(b"\x00").decode("utf-8", "replace")


def check(path):
    if not os.path.isfile(path):
        return ["%s: 文件不存在" % path]
    blob = open(path, "rb").read()
    node, _ = D.load(path)
    bad = []

    root = node.get("") or {}
    model = _s(root.get("model"))
    if model != WANT_MODEL:
        bad.append("root model=%r, want %r（拿错板子的 dtb 了）" % (model, WANT_MODEL))

    h = node.get("/hdmi-sound") or {}
    mclk = h.get("simple-audio-card,mclk-fs")
    name = _s(h.get("simple-audio-card,name"))
    if mclk != b"\x00\x00\x00\x80":
        bad.append("/hdmi-sound mclk-fs=%r, want 128 (0x80)" % (mclk,))
    if name != "rockchip,hdmi0":
        bad.append("/hdmi-sound card name=%r, want rockchip,hdmi0" % (name,))

    u = node.get("/usbdrd/usb@fcc00000") or {}
    dr = _s(u.get("dr_mode"))
    if not u:
        bad.append("/usbdrd/usb@fcc00000 缺失")
    elif dr != "host":
        bad.append("/usbdrd/usb@fcc00000 dr_mode=%r, want host"
                   "（otg 会被 extcon 永久锁在 device，USB3.0 口插设备零枚举）" % (dr,))
    if "extcon" in u:
        bad.append("/usbdrd/usb@fcc00000 仍有 extcon —— 它会抢走 device 角色请求，"
                   "必须删除")

    vop = node.get("/vop@fe040000") or {}
    for k, why in (("vop-supply", "缺 vop-supply → failed to init opp info"),
                   ("support-multi-area", "缺 support-multi-area → 多区域刷新异常")):
        if k not in vop:
            bad.append("/vop@fe040000 缺 %s（%s）" % (k, why))

    sha = hashlib.sha256(blob).hexdigest()[:16]
    print("%-46s %7d B  sha256:%s" % (path, len(blob), sha))
    for b in bad:
        print("   FATAL: %s" % b)
    return bad


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    bad = []
    for p in argv[1:]:
        bad += check(p)
    if bad:
        print("\n板级 DTB 断言失败：这份 dtb 上板会出现「开机正常但功能不对」的问题")
        return 1
    print("OK: 板级 DTB 断言全部通过（model / HDMI 音频 / USB3.0 host / VOP 供电）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
