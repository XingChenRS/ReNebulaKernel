# 上游采纳与锁定记录

## 原则

ReNebulaKernel 从原始项目获取锁定源码或补丁，不复制第三方组合仓库的 Workflow。WildKernels 与 xxz13352 的仓库只作为架构、功能组合和失败诊断的观察来源；实现由本仓库的 schema、adapter、测试和验收契约独立定义。

所有运行时来源必须记录精确 commit。`ref` 只用于说明来源，构建不得解析或跟随浮动分支。

## Root 来源

| Provider | 上游 | 锁定方式 | 内核目录许可证 |
|---|---|---|---|
| KernelSU | [tiann/KernelSU](https://github.com/tiann/KernelSU) | `locks/root-sources.lock.json` 中的精确 SHA | GPL-2.0-only |
| SukiSU | [SukiSU-Ultra/SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) | 精确 SHA | GPL-2.0-only |
| ReSukiSU | [ReSukiSU/ReSukiSU](https://github.com/ReSukiSU/ReSukiSU) | 精确 SHA | GPL-2.0-only |

三者保留完整、非 shallow 的 Git checkout。Built-in 变体将 provider 的 `kernel/` 接入 `common/drivers/kernelsu` 并由 Google 构建系统编译；LKM 变体使用对应 KMI 的摘要锁定 GKI DDK 外部构建。KernelSU-Next 不在候选列表中。

## Feature 来源

| Feature | 上游 | 锁定与适配 |
|---|---|---|
| SUSFS | [simonpunk/susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu) | 5.10 至 6.12 每个 KMI 分支分别锁定 commit；按 provider 使用官方补丁、原生接入或受审计的 Suki 适配。 |
| KPM | [SukiSU-Ultra/SukiSU_KernelPatch_patch](https://github.com/SukiSU-Ultra/SukiSU_KernelPatch_patch) | 仅对 SukiSU built-in 准入：内核启用 `CONFIG_KPM=y` 桥接，外部 `kpimg` 保持 Android 模式，验收后再对 Image 显式后处理。 |
| Vivo vermagic | 无外部源码 | 根据真实 Vivo 5.15/6.1 模块样本，将单独 `vivo` token 插入 module arch token 前。 |

当前锁定 ReSukiSU 源码只有对既有 KernelPatch 的检测，没有 `CONFIG_KPM` 和 `sukisu_kpm_*` 桥接符号；官方 KernelSU 同样没有这套桥接，因此不能套用 SukiSU 的 KernelPatch fork。6.18 当前没有锁定的 SUSFS 分支，也未进入 KPM 准入矩阵。Vivo vermagic 只允许 5.10、5.15、6.1；6.6 及以上拒绝。

## 观察来源

| 仓库 | 用途 | 边界 |
|---|---|---|
| [WildKernels/GKI_KernelSU_SUSFS](https://github.com/WildKernels/GKI_KernelSU_SUSFS) | 观察 GKI 覆盖、组合语义与构建诊断 | 不 vendoring Workflow 或组合脚本 |
| [xxz13352/GKI_KernelSU_SUSFS](https://github.com/xxz13352/GKI_KernelSU_SUSFS) | 观察解耦选项与 uname 反例 | 不复制实现；避免把完整 base release 再写入 localversion |
| [GX0704/vivo_SakiSU](https://github.com/GX0704/vivo_SakiSU) | 理解 Vivo 模块加载兼容需求 | 不引入 Vivo 内核源码；只保留 vermagic 能力边界 |

## 拒绝的行为

- 浮动 branch、`curl | bash`、未校验下载或失败后换上游；
- `patch || true`、宽松 fuzz、未声明模块绕过；
- 把源码锁、静态测试或补丁预检描述成真实 Image 编译成功；
- 修改 `scripts/setlocalversion` 或把完整 base release 重复写进后缀；
- 添加 KernelSU-Next、Vivo `common/` 源码或设备专用内核补丁。
