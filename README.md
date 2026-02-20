# ReNebulaKernel

GKI / VKI 内核自动化构建，支持 Pure、SukiSU、ReSukiSU 三分支；VKI 采用 vivo 源码在线拉取（repo 后替换 common + 移除 vivo_rsc）。

## 构建入口

| Workflow | 说明 |
|----------|------|
| **构建 Pure** | 无 KSU，仅 BBR/ZRAM 等通用功能 |
| **构建 SukiSU** | SukiSU + 可选 SUSFS/KPM |
| **构建 ReSukiSU** | ReSukiSU + 可选 SUSFS/KPM |

Actions → 选择上述任一 workflow → Run workflow，按需填写或使用默认参数。

## 选项说明

- **is_vki**：勾选则使用 VKI 流程（repo 同步后用 vivo tarball 替换 `common/` 并移除 vivo_rsc）。
- **vivo_tarball_url**：VKI 时内核源码 tarball 直链，默认为已知在线地址。
- **version**：内核版本后缀，默认 `NebulaML_XingChen-{pure|sukisu|resukisu}`，可自定义。
- **use_bbr / use_bbg**：默认 BBR、BBG 防格机。
- **embed_zram / zram_more_algos**：内嵌 ZRAM、LZ4KD/LZ4K_OPLUS 等算法。
- **enable_susfs / enable_kpm**：SUSFS、KPM（仅 SukiSU/ReSukiSU 有效）。

## 产物

构建产物通过 Actions 的 Artifacts 下载：AnyKernel3.zip、boot.img、boot-gz.img、boot-lz4.img。

**vivo 设备刷入说明**：需从本机提取内核所在分区镜像（如 boot.img），使用 `magiskboot unpack` 解包后，将 kernel 替换为本仓库生成的 Image，再重新打包后刷入。也可使用 AnyKernel3 方式刷入；**仅可替换 kernel，不得更改压缩格式或破坏 boot 完整性**，否则可能导致不开机等严重后果。

## 文档

- [Workflow 设计说明](docs/WORKFLOW-DESIGN.md)
