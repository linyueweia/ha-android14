# h88k-android14 — HINLINK H88K（RK3588）Android 14 适配

用友善（FriendlyElec）`rk35xx-android14` 的 RK3588 Android 14 镜像做底，
把板级设备树换成 H88K 的，其余全部保持官方原样。

> 目标板：**HINLINK H88K V3**（`compatible = "hlink,h88k"`，RK3588 / 8GB / 64GB eMMC / NVMe 931G）

## 现状

| 阶段 | 内容 | 状态 |
|---|---|---|
| S1 | 编出 `rk3588-hlink-h88k.dtb`（Android 化） | ✅ 285,685 B / 1,568 节点 |
| S2 | 换掉官方镜像里的 dtb，出 SD 卡镜像 | ✅ `out/h88k-a14-sd-8g.img`（8 GiB） |
| S2b | USB 线刷包（RKAF `update.img`） | ✅ [Release `h88k-a14-v1.0.0`](https://github.com/linyueweia/ha-android14/releases/tag/h88k-a14-v1.0.0) |
| S3 | 实机启动（串口 + HDMI） | ⏳ 等上板 |
| S4 | 按失败点补 dts | 未开始 |
| S5 | eMMC 与 NVMe 的 U-Boot 共存 | 未开始（现在 eMMC 承载 U-Boot，刷 eMMC 会让 NVMe 上的 fnOS 起不来） |

两条线怎么烧：SD 卡看 **`docs/H88K-烧写指南.md`**，USB 线刷看 **`docs/H88K-USB线刷说明.txt`**。

## USB 线刷包

Release 里两个文件（都是 1.03 GB）：

| 文件 | 内容 | 用途 |
|---|---|---|
| `H88K-Android14-update.img.zip` | RKAF 容器 `update.img`（2.33 GB，15 个分区） | RKDevTool「升级固件」一键刷 |
| `H88K-Android14-partitions.zip` | `parameter.txt` + `MiniLoaderAll.bin` + 各分区 img + `说明.txt` | 一键刷失败时按 LBA 逐分区写 |

由 `.github/workflows/build-h88k-usb.yml` 产出，用 Rockchip 官方 `afptool` + `rkImageMaker`
打真正的 RKAF（不是自己拼容器格式），链路是：

```
下载友善底包(1 GB, ~20 s) → cpp+dtc 编 dtb(20 s) → pack_boot 换 12 槽+2 logo+SHA1
→ afptool 打 RKAF → rkImageMaker 套 RKFW → 结构校验 → zip(44%) → Release
```

三处和 TM（rk3568）那份不同，都是 rk3588 的实际情况：

1. **A/B 双槽全写**：`uboot/dtbo/vbmeta/boot` 的 `_a` 和 `_b` 都进包。TM 是单槽只写 `_a`，
   这里漏 `_b` 会让首次启动切到空槽直接起不来。
2. **TAG 不写死**：从 `MiniLoaderAll.bin` 偏移 21 读 4 字节再反序（`8853` → `3588`）。
3. **产物级校验**：不只查分区表，还在 `pos` 处真读内容 —— `boot_a` 是 `ANDROID!`、
   `parameter` 是 `FIRMWARE_VER`、`super` 是 sparse magic。afptool 出错最典型的就是
   "条目录了、数据没写"，只有真读才拦得住。

踩过的两个格式坑（都写进校验器注释了）：RKAF 的 `num_parts` 在 `+136`（厂商包实测，
`+134` 是 138 字节头的旧版本），分区 `size` 字段单位是 **2048 字节**不是 512 ——
按 512 算会把 53.5 MB 的 boot 读成 13.4 MB，看着像"包坏了"，其实是单位错。

## 为什么不能照抄 NanoPC-T6 的包

友善 RK3588 Android 的板级 dtb 是 `rk3588-nanopi6-rev01 … rev0c`，**12 个硬件版本槽**
（实测数出来的，不是推算），U-Boot 按板上的 hwrev 电阻/ADC 探测值挑一个。
H88K 不是友善板，探测值不可预测，所以 S2 必须把 **12 个槽全部替换**成 H88K 的 dtb，
只换 rev01 会走到原厂 T6 的树。

## dts 相对基线改了什么

基线：`lyw-hh` 仓库的 `patch/kernel/rk35xx-vendor-6.1/dt/rk3588-hinlink-h88k.dts`
（即 armbian `rk-6.1-rkr5.1` 的官方 H88K dts，仅加了一条 `eee-broken-1000t`）。

本仓只做 Android 化增补，硬件描述一行不动：

1. `#include "rk3588-android.dtsi"` — 带来 optee / ramoops / drm-logo / vendor-storage / fiq-debugger / cma
2. `reserved-memory/cma` 加 `/delete-property/ size;` — android.dtsi 的 8M 与本板 `reg` 256M@256M 不能共存
3. `&vop` 只定频 800MHz，**不写 vop-supply**（厂商 A12 dtb 同节点也没有；RK3588 的 VOP 走 power-domain，
   `vop-supply` 是 RK3568 的形态）
4. `&display_subsystem` 补 `hdmi0_phy_pll` + `hdmi1_phy_pll` 两个时钟 — 少一个则对应口拿不到 PLL
5. `&route_hdmi0/1` 补 `connect` + `force-output` + logo 属性
6. `compatible` 增补 `hlink,h88k`（实机 `/proc/device-tree/compatible` 是这个，不是 `hinlink`）

以上 3/4/5 都以厂商 Android 12 固件里解出的 `rk-kernel.dtb` 为对照（见 `dist/reference/`）。

## 实机勘察（2026-11-07）

详见 `docs/H88K-现场勘察.md`。要点：

- UART2 = `GPIO0_B5/B6`，`/dev/ttyS2` @ **1500000**
- eMMC 前 32MB 是 Rockchip loader 区，p1 380M 是 U-Boot 的 boot 分区
  ⇒ **eMMC 承载 U-Boot**，往 eMMC 刷 Android 会让 NVMe 上的 fnOS 起不来
- fnOS（mainline 6.18）下两个 HDMI 都是 `deferred probe pending`，`/sys/class/drm` 没有 HDMI 连接器

## CI

`dtb-build.yml`：**不克隆内核树**。先在本地把 dtb 的 `#include` 闭包抓进仓库
（`tools/fetch_dts_tree.py` → `vendor/dts-tree/`，27 个文件 / 455KB），
CI 里只做 `cpp -nostdinc -I... -undef -D__DTS__` + `dtc -@`，
再断言 Android 关键节点（optee / ramoops / drm-logo / vendor-storage / fiq-debugger /
chosen.bootargs 有 `console=ttyFIQ0` / cma 只有 reg / vop 无 supply 但 800MHz /
display-subsystem 两个 PHY PLL），最后上传 artifact。

`build-h88k-usb.yml`：USB 线刷包，见上面「USB 线刷包」一节。手动触发可填 `release_tag`
直接发 Release，也可以推 `h88k-*` 标签自动发。

```bash
gh workflow run build-h88k-usb.yml -f release_tag=h88k-a14-v1.0.0 -f with_partitions=true
```

**全程 40-60 秒**（之前从 gitea 克隆整棵树跑 31 分钟连 clone 都没完成）。

```bash
gh workflow run dtb-build.yml   # 或网页 Actions → dtb-build → Run workflow
```

闭包要维护时：

```bash
python tools/fetch_dts_tree.py            # 增量补齐（8 并发，gitea 会 503 限流就降线程）
python tools/fetch_dts_tree.py --verify   # 只校验，确认每个 #include 都能本地解析
```
