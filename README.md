# h88k-android14 — HINLINK H88K（RK3588）Android 14 适配

用友善（FriendlyElec）`rk35xx-android14` 的 RK3588 Android 14 镜像做底，
把板级设备树换成 H88K 的，其余全部保持官方原样。

> 目标板：**HINLINK H88K V3**（`compatible = "hlink,h88k"`，RK3588 / 8GB / 64GB eMMC / NVMe 931G）

## 现状

| 阶段 | 内容 | 状态 |
|---|---|---|
| S1 | 编出 `rk3588-hlink-h88k.dtb`（Android 化） | 进行中 |
| S2 | 换掉官方镜像里的 dtb，出 SD 卡镜像 | 未开始 |
| S3 | SD 卡实机启动（串口 + HDMI） | 未开始 |
| S4 | 按失败点补 dts | 未开始 |
| S5 | eMMC 线刷包（需先解决 U-Boot 共存） | 未开始 |

## 为什么不能照抄 NanoPC-T6 的包

友善 RK3588 Android 的板级 dtb 是 `rk3588-nanopi6-rev01 … rev0b`，**11 个硬件版本槽**，
U-Boot 按板上的 hwrev 电阻/ADC 探测值挑一个。H88K 不是友善板，探测值不可预测，
所以 S2 必须把 **11 个槽全部替换**成 H88K 的 dtb，只换 rev01 会走到原厂 T6 的树。

## dts 相对基线改了什么

基线：`lyw-hh` 仓库的 `patch/kernel/rk35xx-vendor-6.1/dt/rk3588-hinlink-h88k.dts`
（即 armbian `rk-6.1-rkr5.1` 的官方 H88K dts，仅加了一条 `eee-broken-1000t`）。

本仓只做 Android 化增补，硬件描述一行不动：

1. `#include "rk3588-android.dtsi"` — 带来 optee / ramoops / drm-logo / vendor-storage / fiq-debugger / cma
2. `reserved-memory/cma` 加 `/delete-property/ size;` — android.dtsi 的 8M 与本板 `reg` 256M@256M 不能共存
3. `&vop` 补 `vop-supply = <&vdd_log_s0>` + 定频 800MHz — 缺 supply 会显示异常
4. `&display_subsystem` 补 `hdmi0_phy_pll` 时钟 — 缺了 HDMI PHY 拿不到 PLL
5. `&route_hdmi0/1` 补 `connect` 指向具体 vp
6. `compatible` 增补 `hlink,h88k`（实机 `/proc/device-tree/compatible` 是这个，不是 `hinlink`）

## 实机勘察（2026-11-07）

详见 `docs/H88K-现场勘察.md`。要点：

- UART2 = `GPIO0_B5/B6`，`/dev/ttyS2` @ **1500000**
- eMMC 前 32MB 是 Rockchip loader 区，p1 380M 是 U-Boot 的 boot 分区
  ⇒ **eMMC 承载 U-Boot**，往 eMMC 刷 Android 会让 NVMe 上的 fnOS 起不来
- fnOS（mainline 6.18）下两个 HDMI 都是 `deferred probe pending`，`/sys/class/drm` 没有 HDMI 连接器

## CI

`dtb-build.yml`：稀疏克隆 `kernel-6.1`（带 cache）、放 dts、Makefile 注册、用
`nanopi6_android_defconfig` + `CONFIG_SND_SOC_ES8388=y` 编单板 dtb，
然后断言 Android 关键节点（optee/ramoops/drm-logo/vendor-storage/fiq-debugger/chosen.bootargs/cma/vop-supply）。

```bash
gh workflow run dtb-build.yml   # 或网页 Actions → dtb-build → Run workflow
```
