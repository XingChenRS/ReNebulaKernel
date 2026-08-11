# ReNebulaKernel

ReNebulaKernel 是一个只使用锁定 Google GKI 源码的可复现构建系统。它不再下载、替换或引用 Vivo 内核源码；Vivo 兼容性被收敛为可审计的 LKM `vermagic` 标记。

## 唯一构建入口

仓库只保留一个手动 GitHub Actions 工作流：`.github/workflows/build.yml`。推送代码不会自动构建，只有手动点击 **Run workflow** 才会执行。

| 选项 | 含义 |
|---|---|
| `release_id` | 选择一个完整锁定的 Google GKI 源码快照。同一 KMI 可以有多个快照；列表顺序不表示优先级。 |
| `root_source` | `none`、`kernelsu`、`sukisu` 或 `resukisu`。不包含 KernelSU-Next。 |
| `susfs` | 只给 Built-in Image 集成锁定的 SUSFS；LKM 不受影响；6.18 暂不允许。 |
| `kpm` | 仅可与 `sukisu` 同时选择：给 Built-in Image 启用 `CONFIG_KPM=y`，再注入 Android 模式的锁定 KernelPatch；LKM 不受影响；6.18 不允许。 |
| `vivo_vermagic` | 始终显示；开启后只给 LKM 的标准 vermagic 增加独立 `vivo` token。仅 5.10、5.15、6.1 允许，6.6 及以上在解析阶段拒绝。 |
| `uname_tag` | 可选安全标签，例如 `MLXC_RENB`。不要写前导 `-`；系统只追加标签，不覆盖 Google 基础版本。 |

`root_source=none` 只产出 `baseline-image`。选择任一 Root 实现后，一次请求自动并行产生：

- `builtin-image`：Root 编入 Image；SUSFS 只作用于这个变体，SukiSU 的 KPM 也只作用于这个变体。
- `lkm-module`：产出 `kernelsu.ko`；Vivo vermagic 只作用于这个变体。

KSU debug 固定关闭，不再作为用户选项。linkage、hook 和内部 Kconfig 也不是公开输入，而是由 provider profile 和变体契约确定。

## KMI 覆盖

| Android | KMI | 构建后端 | 普通 Root | SUSFS | KPM（仅 SukiSU） | Vivo vermagic |
|---|---|---|---|---|---|---|
| 12 | 5.10 | legacy `build/build.sh` | 是 | 是 | 是 | 是 |
| 13 | 5.10 | legacy `build/build.sh` | 是 | 是 | 是 | 是 |
| 13 | 5.15 | legacy `build/build.sh` | 是 | 是 | 是 | 是 |
| 14 | 5.15 | Kleaf | 是 | 是 | 是 | 是 |
| 14 | 6.1 | Kleaf | 是 | 是 | 是 | 是 |
| 15 | 6.6 | Kleaf | 是 | 是 | 是 | 禁止 |
| 16 | 6.12 | Kleaf | 是 | 是 | 是 | 禁止 |
| 17 | 6.18 | Kleaf | 是 | 暂不允许 | 禁止 | 禁止 |

表中的“是”表示 schema、锁、适配器和静态准入已经建立，不表示所有组合都已完成真实 Image 编译。只有手动 Actions 的实际构建与产物验收通过后，组合才可称为 `image-verified`。

`android16-6.12-2025-12-r1` 锁定 Google `6.12.58`，用于与 Vivo 的 `6.12.58-android16-6` 基线做最接近的公开源码对照；`android16-6.12-lts-2026-08-03` 则锁定较新的 `6.12.92`。二者是各自完整的 manifest、superproject 和 `common` commit 组合，不是把版本数字覆盖到另一份源码上。

## 不可变计划

六项公开输入先被 `scripts/resolve_plan.py` 编译为 schema-5 `build-plan.json`。计划固定 Google source lock、Root/feature source lock、变体、配置和版本契约；后续步骤只消费计划，不再重新解释表单输入，也不会跟随浮动分支。

三个 Root provider 接入时都会记录并应用 `pid0-canonical-pid-v2`：保留 `SET_INIT_PGRP` ioctl 与 userspace 失败回退，并避开已被 Vivo 运行时状态破坏的 `init_task.signal->pids[]`。Built-in 直接使用 PID0 恒定身份 `&init_struct_pid`；LKM 因该符号未导出，改从已导出的 `init_task.thread_pid`（`task_pid(&init_task)`）取得同一个 PID0 对象。Android 16 / 6.12 的 Image 还会记录 `android16-6.12-init-task-pid-read-v1`，只在 PID0 读路径发现该恒定身份被破坏时以 `&init_struct_pid` 返回正确结果；LKM 与其他 KMI 不应用这项核心保护。两项适配都只接受锁定源码锚点恰好匹配，源码漂移会在写入前直接拒绝。

版本只由 `scripts/configure_variant.py` 写入。管理后缀使用不重复 Android/KMI 信息的紧凑 `-RN4-...` 格式；`uname_tag` 只能包含 ASCII 字母、数字、`.`、`_`、`-`，不能带前导横线、重复基础 release，且必须在为 Google localversion 预留空间后仍满足 64 字节 `UTS_RELEASE` 限制。

## 本地静态验证

```text
python -m unittest discover -s tests -v
python scripts/validate_repository.py
python scripts/resolve_plan.py --release-id android14-6.1-lts-2026-08-03 --root-source sukisu --susfs true --kpm true --vivo-vermagic true --uname-tag MLXC_RENB --output build-plan.json
python scripts/validate_repository.py --plan build-plan.json
```

架构约束见 [ARCHITECTURE-V3.md](docs/ARCHITECTURE-V3.md)，上游锁定与采纳边界见 [UPSTREAM-ADOPTION.md](docs/UPSTREAM-ADOPTION.md)。
