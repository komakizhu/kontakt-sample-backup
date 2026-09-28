# 康泰克采样备份

一个用于 Kontakt 音色库及其他大型采样包的 Codex Skill。它先扫描实际目录，再确定每个独立归档的“库”，最后将各库制作成经过 `7zz t` 校验的 8 GiB 分卷 ZIP。目录不必叫 `Kontakt`，也不必采用固定分类；再次备份时，可根据清单跳过未变化的库。

## 安装与使用

在尚未安装此 Skill 的电脑上，将仓库克隆到 Codex 的 Skills 目录：

```bash
git clone git@github.com:komakizhu/kontakt-sample-backup.git ~/.codex/skills/kontakt-sample-backup
```

如果目标目录已存在，请先检查其内容；不要用克隆命令覆盖现有安装。使用前需要 macOS、Python 3、支持分卷的 `zip` 和 `7zz`。后两者可分别用 `zip -v`、`7zz` 检查是否可用。

在 Codex 中提供源目录和目标归档目录，然后说：

> 用 `$kontakt-sample-backup` 备份这个采样库。先扫描目录并展示分组、空间和并行方案，再执行；如果库的边界不明确，先问我。

Skill 默认同时处理 **5 个不同的库**；机械盘负载过高时可降至 3 或 4。它只创建备份，不删除或迁移源文件，也不会自动上传云盘。

## 工作方式

| 阶段 | 会做什么 |
| --- | --- |
| 扫描 | 查看真实目录层级、散落文件和符号链接，不凭 `Standard` 等名字猜测结构。 |
| 计划 | 明确每个归档单元、目标路径、新增/更新/跳过数量及保守的空间需求。未覆盖的源文件会使计划失败。 |
| 归档 | 每个库写入目标盘上的独立暂存目录，使用 `zip -1 -s 8g` 压缩，并以 `7zz t` 校验。校验通过后才移入正式目录、登记清单。 |
| 重跑 | 已登记且源文件元数据未变化的库会跳过；新增或变化的库生成新版本，旧版本保留。 |

备份清单为目标目录中的 `sample_archive_manifest.json`，记录原相对路径、各版本的 ZIP 入口及卷段、大小和校验时间。每次登记成功后，同目录还会生成 `AGENT_采样库恢复指南.md` 和独立的 `restore_sample_libraries.py`；它们会随清单更新。正式归档位于 `packages/`；`.staging/` 是尚未正式登记的暂存区，不能算作已完成备份。分卷的 `.z01`、`.z02` 等文件必须与对应的 `.zip` 放在同一目录。

变化检测使用文件名、大小和修改时间等**元数据指纹**，不是逐字节哈希。若文件内容改变却刻意保留了大小和修改时间，它可能被误判为未变化。ZIP 校验采用 CRC32，也不是加密学意义上的内容证明。归档本身**不加密**；若要上传百度网盘等云端服务，需要另行安排上传及云端核验。

## 手动运行脚本

通常让 Codex 按 [Skill 指南](SKILL.md)执行即可。需要手动操作时，先运行只读扫描：

```bash
python3 scripts/archive.py scan --source "/path/to/source" --depth 3
```

根据扫描结果准备 `RULES.json`。例如，**仅当实际目录确实如此**：`Standard/库名` 和 `Nonstandard/分类/库名`，才可使用：

```json
{
  "rules": [
    {"path": "Standard", "unit_depth": 1},
    {"path": "Nonstandard", "unit_depth": 2}
  ],
  "symlinks": "reject"
}
```

`unit_depth` 是从 `path` 向下数到独立归档目录的层数。默认拒绝符号链接；如选择 `preserve`，ZIP 保存链接本身，不会包含链接指向的外部文件。规则必须覆盖源目录中的所有文件，唯 `.DS_Store` 会自动忽略。

确认源盘和目标盘均已挂载、目标目录已存在后，生成计划并检查结果，再执行：

```bash
python3 scripts/archive.py plan --source "/path/to/source" --destination "/path/to/archive" --rules "/path/to/RULES.json" --output "/path/to/PLAN.json"
python3 scripts/archive.py run --plan "/path/to/PLAN.json"
python3 scripts/archive.py status --destination "/path/to/archive"
```

`run` 默认 `--jobs 5`，可改为 `--jobs 3` 或 `--jobs 4`。`status` 只检查登记数量和卷段是否存在；它**不会重新读取整个 ZIP 做完整校验**。计划生成后若源文件发生变化，应重新扫描和生成计划。

## 接管旧版 Kontakt 归档

若目标目录已有旧流程的 `ARCHIVE_LOG.tsv`，却没有新清单，`run` 会停止，避免在空间有限的盘上直接重压全部库。先核对旧日志与源目录的对应关系，再运行：

```bash
python3 scripts/archive.py adopt-legacy --plan "/path/to/PLAN.json"
```

这一步会用 `7zz t` 测试旧 ZIP，并逐个读取当前源文件，对照压缩包内的文件清单、大小和 CRC32；大容量音色库可能需要数小时，但不需要再存一整套压缩包。匹配的库会纳入新清单，未匹配的库记录在 `legacy_unmatched`。**接管后重新生成计划**，再对未匹配或后来更新的库运行归档。旧压缩包不会被改写。

## 在另一台电脑恢复

将**整个备份目录**复制或下载到电脑 B，让 Agent 先阅读其中的 `AGENT_采样库恢复指南.md`。电脑 B 不需要安装这个 Codex Skill，但需要 Python 3 与 `7zz`/`7z`。独立恢复脚本会从自身所在目录读取清单，不依赖电脑 A 的原始挂载路径：

```bash
python3 restore_sample_libraries.py list
python3 restore_sample_libraries.py restore --target-root "/path/to/new/library-root"
```

`--target-root` 是替代原始源根目录的新位置。例如清单中的 `Standard/Piano` 会恢复成 `新根目录/Standard/Piano`。可用 `--only "Standard/Piano"` 只恢复一个库。恢复脚本会检查卷段、执行 `7zz t`、暂存解压并核对文件数和字节数；已有目标库不会被覆盖。不要从 `.z01` 开始解压，也不要只复制 `.zip` 而漏掉其他卷段。它不会自动重建 Kontakt 注册状态，也不会补齐符号链接指向的外部数据；详细限制以备份目录内生成的指南为准。

中断后可以重新运行同一计划：完整的暂存包会重新校验并接管；不完整的暂存包会保留并报错。只有检查过对应暂存包后，才使用 `run --plan ... --retry-incomplete` 将其移到 `.failed/` 并重建。不要同时启动两个指向同一目标目录的归档进程。
