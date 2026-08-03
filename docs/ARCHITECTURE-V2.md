# ReNebula v2 架构设计

## 目标与边界

ReNebula v2 是一个纯 Google GKI 的可复现构建系统。它不再下载、替换或修补 Vivo 源码；每次构建都从 Google Kernel Manifest 取得目标源码，并记录所有实际使用的提交。

WildKernels 是上游观察源和架构参考，**不是直接 vendoring 的代码源**：其主仓没有声明许可证，因此 v2 独立实现工作流、脚本与组合动作。xxz 的 Root/功能解耦模型可作为交互与校验思路参考。外部源代码、补丁和打包工具必须分别经过许可证审查并固定到完整 commit SHA。

首个可发布目标只覆盖：

- Google GKI `android14-6.1`
- `root=none`
- 无可选补丁、无 AnyKernel3、无发布任务
- 只生成并验证正常 `Image`

后续按 `KernelSU-Next → SukiSU → SakiSU → 已验证的 SUSFS 组合` 逐项扩大，而不是一次恢复旧的全部功能。

## 总体结构

```text
workflow_dispatch
       |
       v
dispatch.yml ──> resolve-plan.yml ──> build-plan.json
                                            |
                                            v
                                      build.yml
                                            |
     +-------------------+------------------+--------------------+
     |                   |                  |                    |
 source adapter      root adapter       feature adapters      package adapter
 Google manifest     none / ksun /      susfs / net / ...     Image first;
 + pinned refs       sukisu / sakisu                          packaging later
     |                   |                  |                    |
     +-------------------+------------------+--------------------+
                                            |
                                            v
                            config fragments + patch preflight
                                            |
                                            v
                              build + uname verification + provenance
```

目录职责：

```text
.github/
  workflows/
    dispatch.yml               # 唯一人工入口，最小权限
    resolve-plan.yml           # 输入解析、组合校验、生成不可变计划
    build.yml                  # 仅消费计划，不重新解释用户输入
    upstream-audit.yml         # 手动/定期记录上游变化，不自动合并
  actions/
    source-google-gki/         # Manifest 同步、deprecated fallback、source SHA 记录
    root-*/                    # 每个 Root 方案一个独立 adapter
    feature-*/                 # 每个可选特性一个独立 adapter
    configure-kernel/          # 幂等写入 config fragment
    version-contract/          # 唯一 uname/version 写者与验证器
    patch-contract/            # preflight、apply、.rej 诊断
    build-image/               # 正常 Image 构建
    provenance/                # 产物来源与版本摘要
profiles/
  targets/                     # Android/GKI/patch-level/构建目标的 JSON profile
  roots/                       # Root capability 声明
  features/                    # 特性 capability 与互斥关系
locks/
  sources.lock.json            # 每个外部 Git 源的完整 commit SHA
scripts/
  resolve_plan.py              # 本地可测试的纯计划解析器
  verify_release.py            # uname/version 断言
```

## 不可变构建计划

`resolve-plan.yml` 将页面输入转换为 `build-plan.json`。之后的步骤只读取这个文件，避免同一个输入在多个 YAML 中被重新解释。P0 的页面只暴露一个已锁定 target；Root 和特性要等对应 adapter 通过验证后才进入页面选项。

```json
{
  "schema": 1,
  "target": "android14-6.1",
  "root": "none",
  "features": [],
  "source": {
    "lock_id": "google-gki-a14-6.1-p0",
    "manifest_commit": "<locked sha>",
    "common_commit": "<locked sha>"
  },
  "version": {
    "expected_base_release": "<audited locked value>",
    "local_suffix": "-ReNebula-v2-a14-6.1-none",
    "expected_uname_release": "<expected_base_release><local_suffix>"
  },
  "locks": {
    "root": null,
    "susfs": null
  }
}
```

计划解析必须拒绝未知 target、未知 Root、未锁定来源、冲突特性和不支持的组合。页面输入不允许直接传 URL、分支名或任意 shell 片段。`expected_base_release` 只是锁定断言；真正的基础版本必须在源码同步后从 `common/Makefile` 重新解析并严格比对。

## Root 与功能解耦

Root 是单选，特性是 capability-gated 的集合：

| Root | 初始状态 | SUSFS | KPM | 备注 |
|---|---:|---:|---:|---|
| `none` | 首发支持 | 否 | 否 | 纯 GKI 验证基线 |
| `ksun` | 后续 | 待验证 | 否 | 使用固定 SHA 的 adapter |
| `sukisu` | 后续 | 待验证 | 仅经 profile 明确允许 | 不隐式启用 builtin/KPM |
| `sakisu` | 后续 | 单独 profile | 否 | 首先只支持 tracepoint 模式 |

SakiSU 固定到 `XingChenRS/SakiSU@6f9672837b9359f8853f47e18e00261edbf6d31e`，在 P1 完成 `sakisu-tracepoint` 验证后才成为页面 Root 预选项。其 adapter 不能调用会 `git pull` 的上游 `setup.sh`，而应在 GKI checkout 根目录以 detached checkout 接入完整 Git 工作树：

1. 将 `common/drivers/kernelsu` 链接至 `KernelSU/kernel`；
2. 幂等加入 `obj-$(CONFIG_KSU) += kernelsu/`；
3. 幂等加入 `source "drivers/kernelsu/Kconfig"`；
4. 首个 SakiSU profile 固定 `CONFIG_KSU=y` 与 `CONFIG_KSU_TRACEPOINT_HOOK=y`。

`CONFIG_KSU_TRACEPOINT_HOOK`、`CONFIG_KSU_MANUAL_HOOK` 和 `CONFIG_KSU_SUSFS` 在 SakiSU 中属于互斥选择。SakiSU + SUSFS 必须另建 `sakisu-susfs-inline` profile；不得把通用 SUSFS 配置叠加到 tracepoint profile，也不得复用 SukiSU 的 KPM 或 companion patch。

## uname 与版本契约

版本只有一个写者：`version-contract` action。禁止改写 `scripts/setlocalversion`，禁止在多个步骤同时写 `CONFIG_LOCALVERSION`、构建时间或 artifact 名称。

```text
base_release          = 从已检出的 common/Makefile 解析 VERSION.PATCHLEVEL.SUBLEVEL
local_suffix          = 仅以 '-' 开头的后缀，例如 -ReNebula-v2-a14-6.1-none
expected_uname_release = base_release + local_suffix
artifact_name         = ReNebula-v2_<target>_<base_release>_<root>_<features>_<source-sha>
```

规则：

- `base_release` 永远来自源码，不是页面输入；
- `local_suffix` 必须以 `-` 开头，禁止携带 `base_release` 或空白；
- 仅通过构建后端支持的 `CONFIG_LOCALVERSION` / config fragment 设置后缀，并显式关闭不需要的自动 SCM 后缀；
- `uname -v` 的构建时间/构建号与 `uname -r` 分开管理；
- 构建前计算预期 release，构建后从产物中的 `Linux version` 反向验证；
- SUSFS 的运行时 `SPOOF_UNAME` 是独立功能，不能替代真实 `uname -r` 验收。

xxz 当前 branding action 把完整 `kernel_version.sublevel` 写进 `scripts/setlocalversion`。该脚本输出本应只是附加后缀，而 Kbuild 已会拼接基础版本，因此会出现概念上的 `6.12.81` + `6.12.81-...` 重复。v2 的 contract 从结构上杜绝该类双写。

## 来源、补丁与可观测性

- `sources.lock.json` 必须固定 Google manifest、manifest XML 摘要、每个同步 project（至少 common、构建脚本、Kleaf/Bazel 依赖与预编译工具链）、Root、SUSFS、打包器和工具链的完整 SHA；构建摘要记录最终解析值。
- 所有补丁先运行 preflight；关键补丁失败立即失败，绝不使用 `patch ... || true` 掩盖结果。
- 失败时始终上传 `.rej`、受影响文件列表、`build-plan.json`、已解析 source SHA 与最后构建日志。
- 初期不引入设备补丁、ABI export 删除、bypass 双镜像、BBRv3、CIFS、NTSync、DroidSpaces、BBG 或复杂缓存；每个能力只有在独立 profile 验证后才能加入。
- `upstream-audit.yml` 只报告 WildKernels、xxz、SakiSU 的 SHA/变更摘要；不会自动同步或执行第三方脚本。

## 迁移阶段

1. **P0：纯 GKI** — 页面仅选 `android14-6.1`，固定 `root=none`、无特性；验证 source lock、Image、真实 uname 和诊断产物。
2. **P1：Root adapters** — 依次加入 ksun、sukisu、sakisu-tracepoint；每个 adapter 有独立组合测试。
3. **P2：受控特性** — 每个 Root/特性组合以 profile 声明，而非自由叠加；先验证再开放 SUSFS。
4. **P3：打包与发布** — 仅在正常 Image 持续稳定后评估 AnyKernel3、boot image 和 release；保留对应许可证与 notices。

v2 的完成标准不是“覆盖更多开关”，而是每个可见选项都能追溯到锁定来源、明确兼容规则、唯一版本语义和可复现诊断。
