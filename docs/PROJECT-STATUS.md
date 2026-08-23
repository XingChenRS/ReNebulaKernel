# 项目状态与维护记录

更新时间：2026-08-23

功能基线：`4c487cc`（本次扫尾只整理文档，不改变构建行为）

## 当前定位

ReNebulaKernel 是一个以精确锁定 Google GKI 源码为输入的可复现构建系统，不是设备通用内核发行版。仓库提供构建组合、适配器、验收与溯源契约；真实设备兼容性仍须由对应 OEM 固件验证。

仓库只保留一个手动 `workflow_dispatch`。推送、合并和文档更新不会自动编译，也不会自动发布产物。

## 已完成的系统化改进

### 可复现输入

- Android 12 / 5.10 至 Android 17 / 6.18 的完整 GKI family 都是一等输入，不设默认优先 KMI。
- 每个 `release_id` 锁定完整 manifest、superproject、`common` commit 和期望基础版本；6.12 同时保留 6.12.58 与 6.12.92 两个独立快照。
- Root、SUSFS 与 KPM 来源均固定仓库和精确 commit；构建不得跟随浮动分支，也不得失败后静默替换上游。

### 单一构建契约

- 六项公开输入先编译为不可变 schema-5 `build-plan.json`，后续阶段只消费计划。
- `root_source=none` 只生成 baseline Image；其他 Root 请求自动并行生成 Built-in Image 与 `kernelsu.ko`，不再让用户手工选择 linkage。
- KernelSU、SukiSU-Ultra 与 ReSukiSU 是三个显式 provider；KernelSU-Next 已完全排除。
- KSU debug、hook、multi-manager 和底层 Kconfig 不作为公共开关，避免 UI 生成未审计组合。

### Feature 解耦

- SUSFS 是独立锁定供应链，只作用于 Built-in Image；6.18 因缺少准入分支而拒绝。
- KPM 只对 SukiSU Built-in 准入，同时要求 `CONFIG_KPM=y` 桥接和经 Android 模式验收的 KernelPatch Image；ReSukiSU/KernelSU 不伪装成支持该桥接。
- `vivo_vermagic` 是单一布尔开关。5.10、5.15、6.1 开启时，Built-in Image 的内核 vermagic 与外部 LKM 的 `.modinfo` 都加入独立 `vivo` token；6.6 及以上在源码同步前拒绝。
- `uname_tag` 只允许安全附加标签，并在预留 Google localversion 空间后执行 64 字节 `UTS_RELEASE` 限制。

### 构建与兼容修复

- Kleaf、legacy `build.sh` 与摘要锁定 GKI DDK 分工明确；LKM 不再伪装成树内 GKI 模块。
- SUSFS 的 Google/source/provider 差异、SukiSU KPM 桥接、SELinux wrapper 与 KPM 工具头文件均由受审计适配器处理，源码锚点漂移即失败。
- Root provider 的 `SET_INIT_PGRP` 路径统一使用 PID0 恒定身份；Built-in 与 LKM 分别采用可链接的等价引用。
- Android 16 / 6.12 Built-in Image 增加仅限 PID0 读取路径的保护，避免损坏的 `init_task.signal->pids[]` 再次触发 panic；不修改其他任务或其他 KMI。
- 每个产物保存 source、root、feature、config、compat 与最终版本记录，并校验 Image/LKM 摘要和版本契约。

## 可复核构建证据

以下只证明对应提交的 Actions 构建和产物验收成功，不等同于完整设备兼容：

| Run | 提交 | 请求 | 结果 |
|---|---|---|---|
| [31425100562](https://github.com/XingChenRS/ReNebulaKernel/actions/runs/31425100562) | `c425d82` | Android 14 / 6.1，SukiSU，SUSFS，KPM，Vivo 请求 | Built-in 与 LKM 成功；属于后续修复前的历史证据 |
| [31432742451](https://github.com/XingChenRS/ReNebulaKernel/actions/runs/31432742451) | `d10974a` | Android 16 / 6.12.92，SukiSU，SUSFS，KPM | Built-in 与 LKM 成功；KPM 仅完成构建/注入验收 |
| [31493714357](https://github.com/XingChenRS/ReNebulaKernel/actions/runs/31493714357) | `bf8ee9c` | Android 16 / 6.12.92，ReSukiSU，SUSFS | Built-in 与 LKM 成功；包含最终 PID0 修复链 |
| [31504596406](https://github.com/XingChenRS/ReNebulaKernel/actions/runs/31504596406) | `4c487cc` | Android 14 / 6.1，ReSukiSU，SUSFS，Vivo | Built-in 与 LKM 成功；Image/LKM 均在产物中检出 `vivo aarch64` |

`31504596406` 的 Built-in Image 产物包含：

```text
6.1.175-android14-11-maybe-dirty-RN4-r-b-s-v SMP preempt mod_unload modversions vivo aarch64
```

对应 LKM 包含：

```text
6.1.166-dirty SMP preempt mod_unload modversions vivo aarch64
```

二者依赖同一 6.1 KMI 与 `CONFIG_MODVERSIONS` 契约，不承诺完整小版本字符串相同。

## 设备观察与已知限制

- 6.12.92 ReSukiSU + SUSFS Image 在测试设备上已经能够进入系统，说明此前的 PID0 panic 路径得到缓解。
- 同一次 6.12 设备测试中 Wi-Fi 不可用。现有留存日志没有 `unknown symbol`、CRC、namespace 或模块加载失败的直接证据，且已无实机复测条件。因此不能把问题归因于“缺少导出符号”，仓库也没有据此放宽 KMI、批量导出符号或绕过 modversion。
- 6.1 的旧 Vivo 构建能够开机，但实机曾选择标准 `6.1-gki` 模块集。`4c487cc` 已修正 Built-in Image 漏写 `vivo` 的构建缺陷，且产物已验证；修正后的 OEM 模块选择尚未完成新的设备验证。
- KPM 的成功记录表示 KernelPatch Image 构建和注入契约通过，不表示目标设备上的 KP/KPM 运行时已验证。
- 其他 KMI/provider/feature 组合大多处于 source-locked 或 source-preflighted；没有成功 Actions 记录时不得称为 image-verified。
- 当前 Workflow 没有跨运行持久构建缓存。仓库不应把 runner 临时状态或未经内容寻址校验的目录描述为“安全缓存”。

## 维护原则

1. 不以设备症状猜测 ABI 修复；新增导出、CRC 例外或 KMI 放宽必须有模块加载日志和原厂/目标符号证据。
2. 不把源码锁、补丁预检或单元测试写成真实编译成功，也不把 Actions 成功写成设备兼容。
3. 更新 source/provider/feature 时只新增精确锁和对应测试，不覆盖旧快照的事实。
4. 每次真实构建保留 run URL、提交、六项输入、variant 和产物记录；设备观察单独登记。
5. 默认不自动构建。只有明确需要新的编译证据时才手动运行单一 Workflow。

架构细节见 [ARCHITECTURE-V3.md](ARCHITECTURE-V3.md)，上游边界见 [UPSTREAM-ADOPTION.md](UPSTREAM-ADOPTION.md)。
