# 上游采纳记录（clean-room）

## 目的与适用范围

本记录保存 ReNebula v2 设计阶段观察到的上游状态，供后续人工审查和变更比对。
它不是依赖清单、镜像清单或代码导入许可；构建系统不得因本文自动拉取、执行或
合并任何第三方内容。

ReNebula 只采纳独立归纳出的接口、验证和可观测性原则。不得复制上游 workflow、
组合 action、脚本、补丁、配置片段、README、发布文案或构建产物。

## 观察引用

| 观察源 | 固定引用 | 本次用途 | 采纳边界 |
|---|---|---|---|
| [WildKernels/GKI_KernelSU_SUSFS](https://github.com/WildKernels/GKI_KernelSU_SUSFS) | [`5ab507c4516c522826172fb06e76b605ae55f191`](https://github.com/WildKernels/GKI_KernelSU_SUSFS/commit/5ab507c4516c522826172fb06e76b605ae55f191) | 观察纯 Google GKI 的发布范围、目标覆盖与失败诊断思路 | 仅记录行为与风险；不 vendoring 仓库内容 |
| [xxz13352/GKI_KernelSU_SUSFS](https://github.com/xxz13352/GKI_KernelSU_SUSFS) | [`a09d33e01bab6b95d2a934a33904c530419a61d2`](https://github.com/xxz13352/GKI_KernelSU_SUSFS/commit/a09d33e01bab6b95d2a934a33904c530419a61d2) | 观察 Root/feature 解耦、输入组合校验和版本语义的反例 | 不把 fork 当作发布上游；不复制实现 |
| [XingChenRS/SakiSU](https://github.com/XingChenRS/SakiSU) | [`6f9672837b9359f8853f47e18e00261edbf6d31e`](https://github.com/XingChenRS/SakiSU/commit/6f9672837b9359f8853f47e18e00261edbf6d31e) | 审查 P1 `sakisu-tracepoint` adapter 的能力与互斥关系 | 仅在独立 adapter、许可证审查和组合测试完成后接入 |

若引用被移动、重写或不可访问，保留本表 SHA 作为历史证据，并新建审计记录；
不得静默改成分支名或“最新版本”。

## Google GKI 覆盖准则

ReNebula 的 KMI 范围由 Google 发布的完整源码与构建链路决定，而不是由某个
第三方 workflow 已列出的矩阵决定。当前 registry 覆盖：

| KMI family | 构建后端 |
|---|---|
| `android12-5.10` | `legacy-build-sh-arm64-v1` |
| `android13-5.10` | `legacy-build-sh-arm64-v1` |
| `android13-5.15` | `legacy-build-sh-arm64-v1` |
| `android14-5.15` | `kleaf-defconfig-fragment-arm64-v1` |
| `android14-6.1` | `kleaf-defconfig-fragment-arm64-v1` |
| `android15-6.6` | `kleaf-defconfig-fragment-arm64-v1` |
| `android16-6.12` | `kleaf-defconfig-fragment-arm64-v1` |
| `android17-6.18` | `kleaf-defconfig-fragment-arm64-v1` |

每个 release 的事实来源是本仓库的不可变 source lock：manifest commit、
`default.xml` 摘要、superproject commit、manifest ref、`common` 提交和
预期基础 release。同步后必须从 manifest 与 superproject 派生所有实际项目
提交并写入 provenance。Google 的
[内核构建文档](https://source.android.com/docs/setup/build/building-kernels)
是 Kleaf 入口的规范参考。Android 12 与 Android 13 5.10/5.15 均使用
`legacy-build-sh-arm64-v1`：以 `common/build.config.gki.aarch64` 调用
`build/build.sh`，导出 `LOCALVERSION=`，并通过 post-defconfig configuration
hook 写入版本。Android 14+ 使用 `kleaf-defconfig-fragment-arm64-v1`。
文档或 branch 的存在不能替代锁定和 Image 验证。

## WildKernels 的许可证边界

在上述观察引用中，WildKernels 根目录未声明可供 ReNebula 直接复用的许可证。
因此它在 ReNebula 中是**观察源**，而不是可复制的代码来源。以下限制是硬边界：

- 不复制或改写其 YAML、脚本、组合 action、补丁、配置、README、发布说明或打包
  内容；
- 不从其仓库直接 vendoring `kernel_patches`、AnyKernel3 定制内容或设备补丁；
- 不以“少量改动”“改名”或“参考后重排”为理由规避该边界；
- 若未来需要同类能力，先定位原始上游及明确许可证，再独立实现、审查并固定来源。

clean-room 的方法是先写出自己的输入契约、数据模型、测试和验收条件，再用公开
可观察行为验证结果，而不是从上游文件推导实现细节。

## 已采纳的原则

1. **Google GKI 是唯一源码路径。** 平台选择由锁定的 Google manifest /
   superproject / `common` 提交定义，不再替换、下载或修补 Vivo `common/`。
2. **family 与 release 分离。** KMI family 表达稳定兼容性；release 表达不可变
   快照；source lock 是可审计根，三者不得用一个自由文本 target 混写。
3. **计划先于构建。** 用户输入先解析为不可变 build plan；后续步骤只能消费计划，
   避免多个 YAML 对同一输入作不同解释。
4. **Root 与 feature 分离。** Root 是单选 adapter，feature 是 capability-gated
   集合；不支持的组合必须在打补丁前失败。
5. **源码和 Image 验证分离。** source-locked / source-verified /
   image-verified 是不同事实，不能把配置存在或上游 branch 存在写成已验证支持。
6. **配置写入必须幂等。** 同一 config key 只保留一个最终值；重复执行不得生成
   重复行。
7. **版本只有一个写入者。** Android 12 与 Android 13 5.10/5.15 断言完整的
   `base release + ReNebula suffix`。Android 14+ Kleaf 可能写入 Google
   localversion，因此 Image 证据出现前不声称精确完整 uname，只要求基础版本
   前缀与最终 ReNebula 后缀，并从 `Linux version` 记录完整观察值。

这些原则由 ReNebula 自己的设计与测试实现，不构成对任一观察仓库实现的导入。

## 明确拒绝的行为

- 浮动分支、`curl | bash`、未校验下载和未锁定的运行时依赖；
- 使用 `patch … || true` 掩盖关键补丁失败，或以宽松 fuzz 作为默认成功标准；
- 为通用 GKI 默认加入设备专用补丁、ABI/export 删除、模块版本 bypass、双镜像
  bypass、BBRv3、NTSync、DroidSpaces、BBG 或 FUSE-BPF；
- 一次性把所有 Android/GKI 组合标为“已验证”，或把配置目录中的条目视为已验证
  支持；
- KernelSU-Next 的 adapter、profile、构建开关、补丁或兼容性承诺；
- 通过在源码树中临时提交来隐藏 dirty 状态；
- 把上游 workflow 的结构、命名、文案或代码作为 ReNebula 实现的复制模板。

## xxz：版本“双前缀”反例

`kernel_version.sublevel` 是 Kbuild 的基础 release，应从实际同步后的
`common/Makefile` 解析；`setlocalversion` 的语义则是追加后缀，而不是第二个
基础版本。

xxz 的 branding 设计会把完整 `kernel_version.sublevel` 写入
`scripts/setlocalversion`。当 Kbuild 已提供基础 release 时，这在概念上会把
例如 `6.12.81` 与另一个 `6.12.81-…` 叠加，形成双写、双前缀或难以审计的
release 语义。

ReNebula 的约束如下：

- `base_release` 只从锁定 GKI checkout 的 `common/Makefile` 读取；
- `local_suffix` 只能以 `-` 开头，且不能包含 `base_release`；
- 只有 version contract 可以写入该后缀；不改写 `scripts/setlocalversion`；
- legacy Android 12 与 Android 13 5.10/5.15 断言精确
  `base_release + local_suffix`；
- Android 14+ 的 `kleaf-defconfig-fragment-arm64-v1` 使用
  `base-prefix-and-suffix`：断言 `base_release` 前缀与最终 `local_suffix`，
  并保留和记录真实 Google localversion 段；
- 产物命名与 `uname -r` 分开生成；构建后必须验证实际 `Linux version`。

## SakiSU：P1 tracepoint-only adapter 约束

SakiSU 在 P1 前不是页面可选 Root。P1 只允许 `sakisu-tracepoint`，并必须满足：

1. 仅使用本记录固定的 SHA，以 detached checkout 取得完整 Git 工作树；adapter
   不得调用可能执行 `git pull` 的上游 `setup.sh`。
2. 接入点固定在 GKI checkout 根目录：将 `common/drivers/kernelsu` 链接到
   SakiSU 的 `KernelSU/kernel`，并幂等登记 `drivers/Makefile` 与
   `drivers/Kconfig`。
3. 首个 profile 仅允许 `CONFIG_KSU=y` 与
   `CONFIG_KSU_TRACEPOINT_HOOK=y`。
4. `CONFIG_KSU_TRACEPOINT_HOOK`、`CONFIG_KSU_MANUAL_HOOK` 与
   `CONFIG_KSU_SUSFS` 互斥；tracepoint profile 不得叠加通用 SUSFS 配置。
5. SakiSU + SUSFS 必须另建并单独验证 `sakisu-susfs-inline` profile，不得引入
   KPM 或未审查的 companion patch。
6. adapter 的源码、配置、构建和真实 uname 组合测试通过前，不产生发布物，也
   不进入默认选项。

## 采纳门槛

任何未来上游变化均按以下顺序处理：

```text
审计固定 SHA
  → 评估许可证与来源
  → 写出独立 ReNebula 需求和测试
  → 实现 clean-room adapter/feature
  → 生成 provenance 与诊断产物
  → 完成组合验证后才开放 profile
```

上游变化永远不会自动合并到 ReNebula。
