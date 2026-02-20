# Workflow 设计说明

## 结构

- **kernel-pure.yml** / **kernel-sukisu.yml** / **kernel-resukisu.yml**：三个入口，仅 `workflow_dispatch`，参数透传到 `_build-kernel.yml`。
- **_build-kernel.yml**：可复用构建流程（`workflow_call`），GKI 与 VKI 共用；VKI 时在 repo 同步后替换 `common/` 并移除 vivo_rsc。

## VKI 流程

1. Repo init/sync 得到 GKI 目录。
2. 若 `is_vki`：删除 `common/`，用 `vivo_tarball_url` 拉取并解压到 `common/`；在 `common/Kconfig` 与 `common/kernel/Makefile` 中注释 vivo_rsc。
3. 后续与 GKI 共用：Stock Config、KSU（仅 sukisu/resukisu）、ksud.h 修复、SUSFS、ZRAM、BBG、defconfig、Bazel 编译、KPM、打包。

## 版本后缀

- 入口默认：`NebulaML_XingChen-pure` / `NebulaML_XingChen-sukisu` / `NebulaML_XingChen-resukisu`。
- `_build-kernel` 中若 `version` 为空，自动使用 `NebulaML_XingChen-{branch}`。

## 依赖

克隆：AnyKernel3、susfs4ksu（SukiSU 用 ShirkNeko，ReSukiSU 用 simonpunk）、kernel_patches、SukiSU_patch；工具链由 AOSP 预置与 repo 提供。
