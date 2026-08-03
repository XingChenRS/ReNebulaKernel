# ReNebulaKernel v3 重构架构

## 1. 目标、边界与术语

v3 是一个只以锁定的 Google GKI 源码链路为输入的内核构建系统。它不再依赖、下载或修补 Vivo 源码，也不把任意上游 fork 的 workflow、脚本或补丁当作可直接导入的实现。

这里的“支持”有严格含义：一个**具体组合**必须拥有固定输入、通过补丁/链接预检、成功产出 Image，并留下版本与产物证据，才能称为 `image-verified`。仅有源码锁、配置文件或上游分支，不构成已验证支持。

完整 Google GKI 源码与构建链路对应的 KMI family 均为一等公民：

| Android 世代 | KMI family | 构建后端 |
|---|---|---|
| Android 12 | `android12-5.10` | `legacy-build-sh-arm64-v1` |
| Android 13 | `android13-5.10`、`android13-5.15` | `legacy-build-sh-arm64-v1` |
| Android 14 | `android14-5.15`、`android14-6.1` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 15 | `android15-6.6` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 16 | `android16-6.12` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 17 | `android17-6.18` | `kleaf-defconfig-fragment-arm64-v1` |

表中没有“首选 KMI”。某个 KMI 被选作一次回归样本或首次验证样本，不会改变其余 KMI 的地位；手动构建入口也不提供隐式默认 KMI。

## 2. 不可变构建计划

工作流只接受经 allowlist 约束的选择，先解析为一个 canonical、可校验的 `build-plan.json`，再进入验证、同步、root 接入、配置、构建和产物验收。后续阶段只消费计划，不能重新解释用户输入或从分支名、URL、shell 片段中推导构建行为。

```text
release_id + root_provider + root_linkage + hook_mode + config_profile + uname_suffix
                         │
                         ▼
                  resolver / 静态矩阵
                         │
                         ▼
                 immutable build plan
                         │
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
 Google 源码锁      root/补丁供应链锁     版本与配置契约
       └─────────────────┴─────────────────┘
                         │
                         ▼
                预检 → Image 构建 → 证据验收
```

计划至少固定并摘要以下输入：Google manifest/superproject 与实际 checkout、family/release profile、构建后端、root 实现与其源码锁、链接方式、hook 与 feature profile、配置片段、用户 uname 后缀、版本契约，以及所有上述文件的摘要。构建时不得使用浮动分支、隐式 `git pull`、运行时下载的未锁补丁，或“失败后换一个上游”的静默回退。

`source-locked`、`source-verified` 与 `image-verified` 是不同状态。状态只能随相应证据提升，不能因选择器中出现了某条目而升级。

## 3. Root 的四个正交维度

root 的“解耦”指的是**把可审计的维度分别建模，再以有限的显式组合放行**；它不等于把所有开关拼成自由笛卡尔积。一个 root 组合由下列维度描述：

| 维度 | 说明 | 例子 | 约束 |
|---|---|---|---|
| implementation | 具体项目及固定版本 | `none`、ReSukiSU、后续候选 SakiSU | 每个实现有独立源码锁、许可审查、adapter 与 KMI 证据 |
| linkage | 编入内核的方式 | built-in、可加载 LKM | 由 implementation 的 adapter 声明；不能靠通用 Kconfig 猜测 |
| hook | 实现接入内核的 hook 策略 | tracepoint、manual、实现专用 hook | hook 是实现语义，互斥或可组合关系必须显式声明 |
| feature | 建立在 root/内核之上的独立能力 | 空集、SUSFS、KPM、未来其他能力 | feature 有自己的上游、补丁、信任与测试链；不是裸 bool |

因此，`CONFIG_KSU` 的值并不能单独表达一个可支持组合。例如同为 KernelSU 系谱的实现，built-in 与 LKM 的链接布局、tracepoint 与 manual 的 hook 约束、以及 SUSFS 的补丁序列都可能不同。ReSukiSU 相对 SukiSU、SakiSU 相对 ReSukiSU 的上下游关系可作为审计线索，但**不能**替代某个固定提交与某个 KMI 的构建证据。SukiSU-Ultra 的 KPM 路线则是另一条独立供应链，不能从 ReSukiSU 或 SakiSU 的存在推导出来。

实现、链接、hook 与配置档位在 profile 中合成为一个不可变元组；当前 Actions 已分别展示 `root_provider`、`root_linkage`、`hook_mode` 和 `config_profile`，但 resolver 只放行兼容矩阵中写明的组合。feature 仍不暴露为自由输入；不得把原始 Kconfig、URL、分支或任意补丁暴露给 dispatch。

## 4. 当前矩阵与演进路线

下表是当前架构的**准入状态**，不是对未跑过 Image 的组合做成功声明。

| KMI 范围 | implementation | linkage / hook | feature | 准入状态与结论 |
|---|---|---|---|---|
| 所有已登记完整 GKI KMI | `none` | 无 / 无 | 空集 | 基线组合；每个 release 的实际验证状态独立记录 |
| Android 12–16 / 5.10–6.12 | ReSukiSU（首个 root 实现） | `lkm` 或 `builtin` / `tracepoint` | 空集 | Actions 允许 `release`、`debug` 两种配置档；源码锁为固定 SHA，`main` 仅作 provenance，逐 KMI 的 Image 证据仍独立记录 |
| 任意 KMI | SakiSU | 不适用 | 空集 | 当前没有 adapter、profile 或 Actions 选项；不是 ReSukiSU 的自动回退 |
| Android 17 / 6.18 | `none` | 无 / 无 | 空集 | 当前只准入基线；没有 root 组合承诺 |
| 任意 KMI | 任一实现 | 任一 | SUSFS | 不在当前选择器；必须走独立供应链与组合验证 |
| 任意 KMI | SukiSU-Ultra 独立链 | 由其自身契约定义 | KPM | 不在当前选择器；不属于 ReSukiSU 或 SakiSU 的隐含功能 |
| 任意 KMI | KernelSU-Next | 任一 | 任一 | 明确排除：不建立 adapter、profile、开关或兼容性承诺 |

**ReSukiSU** 是纯 GKI 的首个 root implementation：它有独立源码锁、linkage adapter、hook/profile 定义和逐 KMI 选择矩阵。锁中的 `main` 只记录来源，不得在运行时被解析、fetch 或 checkout；adapter 只获取固定 SHA 并 detached checkout。SakiSU 当前没有实现，也不是失败时的自动回退。若任一组合在预检或真实构建中失败，应停用/撤回该路径并保留失败证据；不得为了“让它过”而加入未审计兼容补丁，更不能在一次构建中静默切换实现。ReSukiSU、SakiSU 与 SukiSU 的上下游关系只用于确定调研优先级，不自动授予兼容性。

任何新元组的准入顺序固定为：

```text
固定上游与许可
  → 记录 implementation/linkage/hook/feature 契约
  → 对目标 KMI 做干净树预检
  → 构建 Image 与模块/链接验收
  → 记录 uname、provenance、日志和产物摘要
  → 单独标记为 image-verified 后才可宣称支持
```

## 5. SUSFS 与 KPM：独立供应链，不是两个布尔值

SUSFS 不能被建模为 `susfs=true`。它至少需要固定的 SUSFS 上游提交/分支、与特定 root implementation 匹配的 provider patch、与目标 GKI KMI 匹配的内核 patch/源码布局、明确的 Kconfig 与 hook 约束、补丁顺序、许可审查、预检结果和最终 Image 证据。若 ReSukiSU 的 SUSFS 路线通过验证，它也应以单独的 feature profile 和单独矩阵进入，而非隐含在 `resukisu`、SakiSU 或任意 KernelSU 配置中。

KPM 同样不是 `kpm=true`，并且**不属于 ReSukiSU**。它属于 SukiSU-Ultra 的独立供应链，必须单独锁定其内核侧接口/loader、与对应 implementation 的耦合点、KPM payload ABI、模块加载策略、来源与完整性校验、签名或哈希/allowlist、权限边界与自动加载策略。即使某个上游声称兼容某一内核版本，也不能替代 ReNebula 对特定 implementation、KMI、payload 与安全策略的验证。没有完整供应链和安全设计时，KPM 不进入构建选择器。

这两个能力可以在架构上与 built-in/LKM、不同 KernelSU 系谱解耦，但只有显式列出的并且通过测试的组合才允许共存。任何互斥关系（例如某一 implementation 的 tracepoint/manual/SUSFS hook 关系）必须写进 profile 与验证器，在打补丁之前失败。

## 6. 安全的自定义 uname 与单写入者

基础版本永远由同步后的 Google `common/Makefile` 读取；root adapter、feature patch、构建工作流和用户输入都不能重写它。ReNebula 管理后缀由已解析的 family 与配置 profile 生成，例如：

```text
<Google base release>-ReNebula-v3-a<generation>-<kmi>-<config-token><user-suffix>
```

`user-suffix` 是唯一允许的自由文本，但它不是命令行片段：只能为空，或以 `-` 开头、后接 ASCII 字母/数字/`.`/`_`/`-`，长度最多 33 个字符（含前导 `-`）；不得包含完整 Google 基础版本，并且最终 `UTS_RELEASE` 不得超过 64 个字符。它先进入不可变计划，再由版本契约复核。

版本契约是唯一写入者：它只通过所选构建后端的受控配置机制设置 ReNebula 后缀，并校验 `local_suffix = managed_suffix + user-suffix`。不得修改 `scripts/setlocalversion`，不得让 root/feature adapter 写 `CONFIG_LOCALVERSION`，也不得由 artifact 命名、多个 workflow step 或补丁再次追加版本。legacy 后端验证精确的 `base + suffix`；Kleaf 后端允许 Google localversion 保留在中间，只验证 Google 基础版本前缀和 ReNebula 后缀，并从生成元数据与 Image 的 `Linux version` 记录实际值。

## 7. 变更准入原则

v3 的成功标准不是更多开关，而是每个可见选项都有可追溯的源码、链接、hook、feature、版本和测试证据。新 root、SUSFS、KPM 或其他内核定制功能必须新增自己的锁、profile、冲突规则、预检和测试矩阵；不能以“同一上游系谱”“另一个 KMI 曾经成功”或“某 fork 提供了开关”作为准入依据。
