# 研究副本实现与交接说明

本文件说明 `CaptureService.derive` / `CaptureReader.read_derived` /
`EvidenceAuditor.audit_derived` / `CaptureExporter.export_derived` 的实现选择、
改写与来源不变量，以及交付、核账与回归的检查点。业务规则以 DERIVED.md 为准。

## 代码位置

| 职责 | 文件 |
| --- | --- |
| 唯一改写规则（字节级重建） | `acquisition/dataset/rewrite.py` |
| 派生包计划、发布、读取与核账 | `acquisition/archive/derived.py` |
| 派生清单格式校验 | `acquisition/archive/schema.py`（`validate_derived_manifest` 等） |
| 新增表与读写 | `acquisition/archive/store.py`（`derived_packages`、`derived_files`） |
| 四个对外入口 | `delivery/batch.py`、`archive/read.py`、`delivery/audit.py`、`delivery/export.py` |
| 联合核验样例 | `derived_check.py` |
| 回归 | `tests/test_derived.py`、`derived_check.DerivedChecks` |

## 改写与不变量

1. **目标识别只按 tag**：删除 (0010,0010)/(0010,0020)，改写 (0008,0018)/
   (0008,1155) 与文件元信息 (0002,0003)。目标元素必须是单值 `UI`，先按
   `values.decode_uid` 解释旧值；VR 不对即 `CaptureError`（V1 能接纳的嵌套
   错误 VR 在这里会被拒绝）。
2. **一次性同时替换**：映射在改写前完成校验（键/值 UID 语法、≤64 字符、值互异），
   每个旧值只查一次表，因此"新值等于另一个旧值"不会连锁、旧值映射到自身保持原样。
   UI 新值 ASCII，奇数长度补一个 NUL。
3. **映射恰好覆盖**：改写时收集每个父文件（含文件元信息与所有嵌套作用域）遇到的
   旧 UID 集合，与映射键集合比对；多键、少键都 `CaptureError`。包外引用不另行查找。
4. **只动必须动的字节**：其余普通元素、项目头、分隔符与整个封装 Pixel Data 原样
   拷贝；128 字节前导不动。仅重算三处长度：UI 元素长度、有界 SQ/项目内容长度、
   文件元信息组长。容器原有的有界/未定义长度形式与定界符逐字保留，因此有界容器
   变长、未定义容器长度字段仍为 `0xFFFFFFFF`。
5. **可被 V1 解释**：每个派生件都会重新走 `parse_metadata` + 分帧提取，帧数、帧序、
   每帧字节与摘要必须与父成果一致，否则派生失败。

`rewrite_file` 的返回值同时是"改写结果"和"遇到的旧 UID 集合"，发布与核账复用同一
函数，避免两套规则漂移。

## 固定结果与持久化

- 包身份 = `package_id`；`derived_packages.manifest` 保存 `version/package_id/
  capture_ids/uid_map/files`，`derived_files` 保存每个文件完整 Part 10 字节
  （`file_index` 对应 `capture_ids` 顺序）。父件不复制，只记
  `parent_capture_id` 与 `source_digest`，并始终经 `CaptureReader.checked_capture`
  读取归档原件。
- `read_derived` 返回全新 dict；调用方修改返回值不会触及库内字节。
- 帧范围两侧分别以对应完整 Part 10 字节为零点：`source_spans` 对原件、
  `derived_spans` 对派生件，按消费顺序；两侧重建必须得到同一 JPEG 正文。
  像素尾填充保留在完整附件中但不在范围内。
- 发布在单个 `BEGIN IMMEDIATE` 事务内写清单和全部文件，`checkpoint` 依次为
  `derived_file:N`、`derived_manifest`、`before_commit`；失败回滚，不占
  `package_id`，已发布包与旧回执不变。
- 旧库（无派生表）在打开时同一事务里补建 `derived_packages`/`derived_files`；
  只加表、不改行，其他表定义与 schema 版本仍严格校验。

## 重投、冲突与失败

- 同 `package_id` 且有序父列表与映射（dict 序无关）相同：先完整核账，再返回原
  `package_id`；期间发现损坏报 `IntegrityError`，不会借重投"修好"。
- 父顺序或映射不同：`SourceConflict`，绝不覆盖或另建版本。
- 输入结构问题、缺父成果、UID/改写错误：`CaptureError`（缺父件在预检时转换，
  而不是泄漏 `NotFound`）；读取/核账/导出不存在包：`NotFound`；字节、关联、
  映射或来源损坏：`IntegrityError`；I/O 与 SQLite 异常原样传播。

## 核账（`audit_derived`）

读取已归档父件与派生件，并**从父件重新计算预期副本**，逐项检查：

- 父绑定：`parent_capture_id` 存在于 `capture_ids` 同序位置，`source_digest`
  与父件归档摘要一致，父件本身通过原完整性检查。
- 映射覆盖：用存储映射重算每个父件的旧 UID 集合，必须恰好等于映射键集。
- 改写正确：重算字节必须与存储的派生字节逐字相等——这同时覆盖"删除姓名/患者号"、
  "只改目标 UID"、"非目标字节与 Pixel Data 不变"、"容器形式与长度正确"。
- 双侧来源：存储的 `source_spans` 必须等于父件提取出的帧来源，`derived_spans`
  重建字节必须与原件侧相同、等于帧摘要与大小；审计（以及导出前核账）还重新
  运行采集链解析父件与副本，要求帧边界、正文与摘要和存储来源一致。自报摘要被
  同步改坏也无效，因为比较的是重算字节，而非摘要是否自洽。

`read_derived` 与导出前的读取也执行同一套校验；区别只在 `acquisition.frames`
解析器：固定读取在禁止导入提取器的进程里仍可用（V1 对读取/导出的约束），而
`audit_derived` 与 `export_derived` 会重新解析双方字节，做更深的派生链核验。

`tests/test_derived.py` 覆盖五种独立损坏：派生字节（同时更新自报摘要）、映射、
父绑定（连同来源摘要互换）、父 `source_digest`、帧来源区间；均要求
`read_derived`/`audit_derived`/`export_derived` 拒绝且不留导出目录。

## 导出

`export_derived` 先核账，再在目标同级暂存目录写完 `dicom-NNNNNN.dcm`（按
`capture_ids` 顺序）与 `manifest.json`（read 清单去掉 `body`、加入 `filename`），
最后改名发布；目标已存在抛 `FileExistsError`，失败只清理本次暂存。原 JPEG 导出
`export` 与 `frame-NNNNNN.jpg` 格式不变。

## 运行入口

- `python3 -B run.py verify`：原回归 + 派生回归 + `DerivedChecks`。
- `python3 -B derived_check.py fixtures`：V1 布局自检（不依赖派生实现）。
- `python3 -B derived_check.py verify`：联合核验（断言失败非零退出）。
- `python3 -B derived_check.py demo --directory derived-demo`：采集、派生、重开、
  核账、导出并打印双侧来源。
- `python3 -B run.py demo --directory demo-output`：原链路 + 双文件派生交付；
  重复运行验证既有目录字节不变。

`run-manifest.json` 与 `.pointwise-runtime.json` 保持 `version=1`、`kind=cli`、
`setup=[]`，verify/demo 仍为单个 argv 数组（`run.py` 内部组合两类核验）。

## 已知边界

- 仅 README 规定的 Part 10 显式 VR 小端、JPEG Baseline 子集；不做完整去标识
  （像素文字、私有标签内容不清除），不新增 DICOMDIR/传输语法/网络。
- 全量解析与内存重建被允许；当前实现逐文件重建，不缓存跨次结果。
- 不是并发或跨进程恢复设计；SQLite 事务与暂存发布保证单工作者下的原子性。
