# ReNebulaKernel 架构契约

## 1. 目标与边界

ReNebulaKernel 只以锁定的 Google GKI 源码链路为内核输入。八个具有完整公开源码与构建链路的 KMI family 都是一等公民，不设置默认优先 KMI，也不引用不完整的 Vivo `common/` 源码。

Vivo 适配不是另一套源码或构建流。它只是 `vivo_vermagic` 布尔能力：在 5.10、5.15、6.1 的 LKM 构建中，把独立 `vivo` token 插入 `aarch64` 前；开关始终可见，但 6.6 及以上必须在任何源码同步或打补丁前拒绝。

## 2. 单一 Workflow 与计划边界

唯一入口是手动 `workflow_dispatch`：

```text
release_id + root_source + susfs + kpm + vivo_vermagic + uname_tag
                                  |
                                  v
                         schema-5 build plan
                                  |
                 +----------------+----------------+
                 |                                 |
          builtin-image                      lkm-module
       SUSFS; SukiSU KPM                 Vivo vermagic only
```

`root_source=none` 是特例，只生成 `baseline-image`。非 `none` Root 请求不再要求用户选择 built-in 或 LKM，而是同时生成两种产物。

计划是输入与构建之间的唯一权限边界。它固定：

- Google manifest、superproject、`common` commit 与构建后端；
- Root provider 的仓库、精确 commit 和接入 profile；
- SUSFS/KPM 的仓库、精确 commit、KMI 分支及应用方式；
- 每个 literal variant 的 Kconfig、版本后缀和产物契约；
- 所有定义文件的摘要。

工作流后续阶段不得从 branch、URL、用户字符串或环境变量重新推断行为，不得失败后静默换 provider，也不得跟随 `main`。

## 3. Root 与 feature 语义

Root provider 是四选一：

- `none`：纯 GKI baseline；
- `kernelsu`：官方 KernelSU；
- `sukisu`：SukiSU-Ultra；
- `resukisu`：ReSukiSU。

KernelSU-Next 明确排除，不建立锁、profile、adapter 或兼容承诺。KSU debug 固定关闭。用户不直接选择 linkage、hook、multi-manager 或原始 Kconfig；这些由锁定 provider profile 和 literal variant 统一编译。

Feature 是独立供应链，不是 Root 名称的隐含效果：

- SUSFS：按 KMI 选择锁定的 `susfs4ksu` source/patch，只应用到 built-in；6.18 因没有锁定上游分支而拒绝。
- KPM：只允许 `root_source=sukisu`。SukiSU built-in 必须同时具备 `CONFIG_KPM=y` 内核桥接和锁定的 Android `SukiSU_KernelPatch_patch` Image 层；生成的 `kpimg` 必须验收为 `config=android,release`。6.18 不准入。
- Vivo vermagic：无外部源码，只修改 LKM 构建产生的 module vermagic；仅 5.10/5.15/6.1 准入。

这些开关可以同时请求，但 resolver 把它们投影到各自适用的变体并校验 provider 能力，避免把 built-in feature 错施加给 LKM、把 SukiSU 专用 KPM 套给其他 provider，或把 Vivo 标记写入 Image。

## 4. 适配顺序

每个 matrix job 必须按固定顺序执行：

```text
恢复并校验计划
  -> 同步并校验 Google 源码
  -> 接入 Root provider
  -> 应用源码期 feature（SUSFS / SukiSU KPM source adapter）
  -> 统一编译 Kconfig 与 LOCALVERSION
  -> 构建 Image 或 kernelsu.ko
  -> 对 Image 执行 KPM 后处理（若开启）
  -> 验收版本、vermagic 与产物
  -> 上传当前 variant 的产物和 provenance
```

Built-in Image 由 Google 的 Kleaf 或 legacy `build.sh` 构建。LKM 不再伪装成树内模块；它由对应 KMI 的摘要锁定 GKI DDK 外部构建，并按 KMI family、vermagic 与唯一 `kernelsu.ko` 产物契约验收。

## 5. 版本契约

Google 基础 release 永远从同步后的 `common/Makefile` 读取。唯一写入者 `configure_variant.py` 只追加紧凑的 `-RN4-...` 管理后缀和可选用户标签，不重复 Android/KMI 信息，不修改 `scripts/setlocalversion`，也不允许 Root/feature adapter 写 `CONFIG_LOCALVERSION`。

`uname_tag` 不带前导横线；resolver 负责校验安全字符、重复 base，并为 Google localversion 预留 Kleaf 25 字节或 legacy 32 字节后再执行 64 字节限制，随后系统以 `-<tag>` 形式追加。所有后端都按“基础 release + Google 中间段 + ReNebula 后缀”验收；产物名称与 `uname -r` 分离，最终值由构建产物再次验证。

## 6. 验证等级

- `source-locked`：所有网络来源均有精确 commit 与摘要。
- `source-preflighted`：适配器在对应锁定源码上完成补丁/布局预检。
- `image-verified`：真实 Actions 构建完成，Image 或 LKM 及其版本契约通过验收。

静态单测和源码预检不能冒充真实编译。新 KMI、provider 或 feature 组合只有在手动工作流留下成功证据后，才能提升为 `image-verified`。
