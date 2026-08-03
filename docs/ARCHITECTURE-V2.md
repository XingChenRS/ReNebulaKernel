# ReNebula v2 架构设计

## 目标与边界

ReNebula v2 是纯 Google GKI 的可复现构建系统。每一次构建都由固定的
Google Kernel Manifest 与 superproject 提供源代码，并留下实际 checkout
提交、构建后端、配置和产物版本的证据。

它不再下载、替换或修补 Vivo 源码；也不把 WildKernels、xxz 或其他 fork
当作可直接导入的代码来源。WildKernels 仅是架构与行为的观察源：其根仓库
未声明可供 ReNebula 直接复用的许可证，因此 v2 的工作流、脚本和组合逻辑
必须独立实现。外部补丁、工具或 adapter 也必须先通过许可证审查并固定到完整
commit SHA。

## 支持范围：完整 Google 链路，而非单一 6.1

“支持”首先指该 KMI 有完整、可锁定的 Google 源码和构建链路：
manifest、superproject、`common`、所需构建项目和明确的构建入口都可被
固定和核验。它不等同于宣称每一个快照都已经跑完 Image 验证；后者由独立的
发布状态记录。

| Android 世代 | KMI family | 构建入口 | 后端 |
|---|---|---|---|
| Android 12 | `android12-5.10` | `build/build.sh` | `legacy-build-sh-arm64-v1` |
| Android 13 | `android13-5.10` | `build/build.sh` | `legacy-build-sh-arm64-v1` |
| Android 13 | `android13-5.15` | `build/build.sh` | `legacy-build-sh-arm64-v1` |
| Android 14 | `android14-5.15` | `tools/bazel … //common:kernel_aarch64_dist` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 14 | `android14-6.1` | `tools/bazel … //common:kernel_aarch64_dist` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 15 | `android15-6.6` | `tools/bazel … //common:kernel_aarch64_dist` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 16 | `android16-6.12` | `tools/bazel … //common:kernel_aarch64_dist` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 17 | `android17-6.18` | `tools/bazel … //common:kernel_aarch64_dist` | `kleaf-defconfig-fragment-arm64-v1` |

`android14-6.1` 是最先演练的示例和回归基线，而不是唯一目标。任何新快照只要
满足相同的数据契约，就加入既有 family，而不复制一套工作流。

发布状态至少分为：

```text
draft → source-locked → source-verified → image-verified
```

只有 `image-verified` 可以表述为已实际验证的 Image；`source-locked` 仅说明
输入链路已被固定。状态升级必须留下关联的构建计划、日志、产物摘要和版本验收，
不能因为分支存在或配置文件出现而自动发生。

## 数据模型

KMI family、具体 release 和源锁定是三个不同层次，禁止再把它们混成一个
`target` 字符串。

```text
profiles/
  registry.json                    # 唯一可选的静态 release_id 列表
  families/
    <family>.json                  # 稳定兼容性、源码布局、允许的构建后端
  releases/
    <release-id>.json              # family + 不可变 source lock + 验证状态
  roots/                           # 未来 Root capability 声明
  features/                        # 未来 feature capability 与互斥关系
locks/
  sources.lock.json                # manifest/superproject 根锁与派生源码证据
```

- `family_id` 描述 KMI 兼容性，例如 `android14-6.1`。
- `release_id` 描述某一次不可变快照，例如
  `android14-6.1-lts-2026-08-03`。
- `lock_id` 指向该 release 的 manifest/superproject 根锁。

每个锁定项必须固定 manifest commit、`default.xml` 摘要、superproject
commit、manifest ref、预期 `common` 提交和基础版本。同步器从 manifest
解析项目清单，再以 superproject 的 gitlink 验证每个项目的精确提交；实际
materialize 的完整项目列表写入 provenance，而不是信任浮动 branch。

## 不可变构建计划

页面只暴露静态 `release_id`。解析器读取 registry、family、release 和锁文件，
生成唯一的 `build-plan.json`；所有后续阶段只消费该计划。

```json
{
  "schema": 2,
  "selection": {
    "family_id": "android14-6.1",
    "release_id": "android14-6.1-lts-2026-08-03",
    "state": "source-locked",
    "root": "none",
    "features": []
  },
  "source": {
    "lock_id": "gki.android14-6.1.lts.2026-08-03",
    "manifest_commit": "<locked sha>",
    "superproject_commit": "<locked sha>"
  },
  "build": {
    "adapter": "kleaf-defconfig-fragment-arm64-v1"
  },
  "version": {
    "expected_base_release": "<locked value>",
    "local_suffix": "-ReNebula-v2-a14-6.1-none",
    "release_contract": {
      "mode": "base-prefix-and-suffix",
      "prefix": "<locked value>",
      "suffix": "-ReNebula-v2-a14-6.1-none"
    }
  }
}
```

解析器必须拒绝未知 release、未注册 family、未锁定来源、不匹配的 build
adapter、冲突 capability 和任何直接提供的 URL、分支名或 shell 片段。
`expected_base_release` 只是锁定断言；真正的基础版本仍必须在同步后从
`common/Makefile` 重新解析并严格比对。

Android 12 与 Android 13 5.10/5.15 共用 `legacy-build-sh-arm64-v1`：
它以 `common/build.config.gki.aarch64` 调用 `build/build.sh`，导出
`LOCALVERSION=`，再通过 post-defconfig configuration hook 写入唯一的
ReNebula 后缀。Android 14+ 才使用生成 defconfig fragment 的
`kleaf-defconfig-fragment-arm64-v1`。

```text
workflow_dispatch
       |
       v
dispatch.yml ──> resolve-plan.yml ──> build-plan.json
                                      |
                                      +──> static verification gate
                                      |
                                      v
                                   build.yml
                                      |
                 +---------------------------+---------------------------+
                 |                                                       |
                 v                                                       v
      legacy-build-sh-arm64-v1                         kleaf-defconfig-fragment-arm64-v1
      Android 12 + Android 13 / build/build.sh          Android 14+ / defconfig fragment
                 |                                                       |
                 +---------------------------+---------------------------+
                                      |
                                      v
                   Image + uname verification + provenance record
```

构建 job 必须依赖解析与静态验证 job，不能在 gate 失败后仍开始同步源码。后端
选择只能是计划中的 allowlist 值，不得由 workflow 动态拼装命令。

## P0、Root 与功能解耦

P0 对 registry 中每一个 release 均固定为：

- `root=none`
- 无可选 feature、补丁、AnyKernel3 或发布任务
- 只生成并验证正常的 arm64 GKI `Image`

这让 KMI 覆盖、源锁定、构建后端和 uname 契约先被独立验证。未来的 Root 是
单选 adapter，feature 是 capability-gated 集合；两者都必须由明确 profile
声明兼容性，不能通过隐式默认值或自由组合开关接入。

SakiSU 的首个候选项是锁定源码的 `sakisu-tracepoint`：只在独立 adapter、
许可证审查和组合测试通过后加入。它不是通用 SUSFS 或 KPM 的隐式开关。
KernelSU-Next 明确不在 ReNebula 的范围内：不建立 adapter、profile、构建开关
或兼容性承诺。

## uname 与版本契约

`common/Makefile` 是基础 release 的唯一来源。版本契约只有一个写入者，按所选
构建后端加入以 `-` 开头的 ReNebula 后缀，绝不把基础版本再写入后缀。所有现代
Kleaf 分支都可能自行写入 Google localversion；ReNebula 不删除、重写或伪造
该段，也不会在 Image 证据出现前声明完整 uname 的精确值。

```text
base_release           = common/Makefile 的 VERSION.PATCHLEVEL.SUBLEVEL
local_suffix           = -ReNebula-v2-<family>-<root>
legacy contract         = exact(base_release + local_suffix)
A14+ Kleaf contract     = prefix(base_release) + suffix(local_suffix)
observed Kleaf release  = base_release + Google localversion + local_suffix
```

- 不修改 `scripts/setlocalversion`；
- 不允许多个步骤同时写 `CONFIG_LOCALVERSION`、版本文件或 artifact 名称；
- Android 12 与 Android 13 5.10/5.15 的 legacy adapter 必须断言完整的
  `base_release + local_suffix`；
- Android 14+ 的 `kleaf-defconfig-fragment-arm64-v1` 使用
  `base-prefix-and-suffix`：实际 release 必须以 `base_release` 开头、以
  `local_suffix` 结尾，同时记录完整 Google localversion 段；
- 只关闭后端明确不需要的动态 SCM 后缀，不能覆盖锁定 Google localversion；
- 构建后从生成元数据和 Image 中的 `Linux version` 反向验证真实 release；
- artifact 名称、构建时间和 `uname -r` 分开管理。

xxz 的现有 branding 做法把完整 `kernel_version.sublevel` 写进
`scripts/setlocalversion`。Kbuild 已经会拼接基础版本，这会形成类似
`6.12.81` 与 `6.12.81-…` 的双前缀语义。v2 通过单写入者和“后缀不得包含
base release”的规则从结构上禁止该问题。

## 来源、补丁与可观测性

- source lock 固定 Google 根锁，并将派生后的项目提交完整写入构建记录；
- 每个外部补丁先做 preflight，关键失败必须立即失败，不能使用
  `patch … || true` 掩盖结果；
- 失败时保留 `.rej`、受影响文件、build plan、已解析 source SHA 和最后的
  构建日志；
- 初期不引入设备专用补丁、ABI/export 删除、模块 bypass、双镜像 bypass、
  BBRv3、NTSync、DroidSpaces、BBG 或复杂缓存；
- 上游审计仅报告 WildKernels、xxz、SakiSU 的固定 SHA 与变更摘要，不自动
  同步、合并或执行第三方脚本。

## 演进顺序

1. **P0：多 KMI 纯 GKI 基线**：锁定并验证所有完整 Google 构建链路，先完成
   `root=none` 的 source、Image、真实 uname 和 provenance 验收。
2. **P1：Root adapters**：按独立 profile 引入已审查的 SakiSU 等方案；
   每个 adapter 都有自己的 KMI 兼容矩阵与组合测试。
3. **P2：受控 feature**：仅在明确 capability 和组合验证后开放 SUSFS 等功能，
   不提供自由叠加。
4. **P3：打包与发布**：仅在正常 Image 持续稳定后评估 AnyKernel3、boot image
   和 release，并保留所需 notices。

v2 的完成标准不是“有更多开关”，而是每一个可见选项都能追溯到锁定来源、明确
兼容规则、唯一版本语义和可复现诊断。
