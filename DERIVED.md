# 固定DICOM研究副本

现有README.md仍定义原件接收、格式子集、JPEG采集、错误类型及原接口。当前工程不实现本文件的派生能力。derived_check.py只有固定布局样例及业务断言，不是改写器；其fixtures模式可在V1运行，verify/demo模式需要本题实现。

## 业务结果和边界

研究员领取限定规则改写的DICOM副本，包内实例定义和引用一致；JPEG原码流不变，复核员能从数据库中的原件与副本分别重建它。这里不是完整DICOM去标识或隐私合规认证，不清除像素文字；不增加DICOMDIR、新传输语法、网络、并发导入或跨进程任务恢复。允许全量解析、重建和重新提取，不限制扫描次数及额外内存；可复用既有解析、JPEG、事务和导出设施。

## 调用

沿原Python类增加以下方法，不改变旧方法：

- acquisition.delivery.batch.CaptureService.derive(package_id, capture_ids, uid_map) 返回传入的package_id字符串。
- acquisition.archive.read.CaptureReader.read_derived(package_id) 返回下面的独立dict，含归档的派生bytes。
- acquisition.delivery.audit.EvidenceAuditor.audit_derived(package_id) 完成实际核账后返回True。
- acquisition.delivery.export.CaptureExporter.export_derived(package_id, destination) 返回导出清单dict。

package_id是非空、可UTF-8编码的不透明字符串，不用作文件路径。capture_ids是非空list或tuple，成员为互不重复的已存在固定capture_id字符串，次序有意义；父成果必须通过原有完整性检查。uid_map是str到str的dict，dict键顺序无意义。不得依赖外部原文件。对同package_id，相同有序父成果和相同映射重投返回原成果；改变任一输入抛SourceConflict，不能悄悄覆盖或创建另一版。未发布的失败尝试不占package_id。输入结构、缺父成果、UID或改写错误抛CaptureError；读取、复核或导出不存在包抛NotFound；归档字节、关联、映射或来源损坏抛IntegrityError。I/O及SQLite异常传播。损坏不能被当作普通重投或自动修复。

## 唯一改写规则

仅处理README支持的Part 10显式VR小端、JPEG Baseline数据集。递归处理全部SQ项目，包括Pixel Data之后的普通元素：

1. 在数据集所有作用域删除PatientName(0010,0010)与PatientID(0010,0020)整个元素；目标按tag识别，不以VR或文本猜测。
2. 在数据集所有作用域，将SOPInstanceUID(0008,0018)和ReferencedSOPInstanceUID(0008,1155)的值改为uid_map中的值；文件元信息中存在的MediaStorageSOPInstanceUID(0002,0003)同样改写。不新增缺失的可选元信息。
3. 上述每个UID目标必须为单值UI，沿values.decode_uid规则解释旧值。映射的key集合必须恰好等于全部父文件中这些目标遇到的旧UID集合；所有value均须符合相同UID语法、长度不超过64字符，且互异。允许映射到自身，也允许新值等于另一个旧值；每次按原值同时替换，不连锁替换。新UI值为ASCII，奇数长度补一个NUL，偶数不补。包外引用不另行查找，出现的引用UID也须被映射覆盖。
4. 类UID、传输语法及非目标UI不改。更新所有受影响的有界元素、项目、SQ及现有文件元信息组长，保留各容器原有的有界或未定义长度形式；其他普通元素原字节、顺序以及整个封装Pixel Data原字节保持不变。保留128字节前导。未定义长度容器的原定界符保留；不得规范化无关文本、重排元素、重分片或重编码JPEG。

同一旧UID无论出现于哪个父文件、哪个层级均采用同一映射。即使V1已接纳某父成果，V2仍须检查其嵌套目标UID的VR和语法。派生附件必须可由V1采集链完整解释，帧数、帧序及各帧原字节与父成果相同。

## 固定结果

原件、派生附件、输入顺序、完整映射及各帧在原件和派生件中的来源共同固定；每帧两侧重建字节必须相等。

read_derived返回以下结构；必需字段如下，可增加不影响这些含义的字段：

```python
{
  "package_id": str,
  "capture_ids": [str],             # 输入顺序
  "uid_map": {str: str},
  "files": [{
    "parent_capture_id": str,       # 与capture_ids逐项对应
    "source_digest": str,           # 归档原件SHA-256
    "derived_digest": str,          # 派生完整Part 10字节SHA-256
    "body": bytes,
    "frames": [{
      "index": int, "size": int, "digest": str,
      "source_spans": [[int, int]],
      "derived_spans": [[int, int]]
    }]
  }]
}
```

帧index从0连续；digest为父JPEG的SHA-256，size为其字节数。范围仍为左闭右开字节坐标，分别以原件与派生件完整Part 10字节为零点，按消费顺序保存。各侧区间分别拼接得到同一原始JPEG；排除的像素尾填充仍保留在完整附件中。不能把原件坐标冒充派生坐标。清单、父绑定、完整映射和附件须持久保存，read返回值被调用方改动不能修改归档。成功后重开不依赖外部文件或重新生成副本。

任何准备或提交失败均不发布半包，不占package_id，不改变原成果；导出沿用不覆盖及暂存发布规则。包不能以逐项调用ingest_batch产生部分成功来代替。派生失败不能改变任何旧成果或旧回执；保留原接口、原成果读取及全部原回归。用原ArchiveStore故障回调的before_commit阶段核验派生发布回滚；导出沿用CaptureExporter故障回调的before_publish阶段。

 audit_derived必须读取已归档的父、子字节，检查父绑定、映射覆盖、限定结构改写、非目标字节保留和双侧帧来源；仅比较自报摘要或仅检查两份JPEG摘要不足。允许从归档原件重新计算预期作核账，但不能借此修改或代替已存副本；读取与导出消费固定副本。任何一侧字节、映射、父绑定或来源被改坏均应报IntegrityError，即使损坏者同步更新了对应自报摘要。

export_derived先完成核账，再向原本不存在的destination发布。父目录须存在，文件顺序为capture_ids顺序，文件名为dicom-000000.dcm、dicom-000001.dcm等；同目录manifest.json为UTF-8 JSON，结构为read_derived清单去掉每个files条目的body、增加filename。返回值为该JSON对象。失败清理本次暂存，保留原目录，已有目标抛FileExistsError。原JPEG导出方式仍保留。

## 样例与实际核验

 derived_check.py中的samples()独立拼装两个原文件和这个有限样例的期望副本，不接收任意DICOM作转换。A实例为1.2.3，嵌套引用B的1.2.4；B相反。映射为1.2.3→2.25.12345、1.2.4→2.25.678901。样例同时含有界SQ/项目及未定义长度SQ/项目、顶层/嵌套/像素后个人标签、非目标ImplementationClassUID、未知UN字节和JPEG APP中的1.2.3。后两处及非目标UID保持原样。原/派生的元信息组长分别为A 92/96、B 92/98，最外有界SQ长度分别为A 146/104、B 146/100；Pixel Data元素头偏移均由634变为564。文件名不承担身份语义。

可运行入口：

- python3 -B run.py verify：完整原回归，不改写或删除原测试。
- python3 -B derived_check.py fixtures：V1样例自检，实际核对两份布局、JPEG及来源，并拒绝原字节替换反例。
- python3 -B derived_check.py verify：实现后的联合核验；断言失败须非零退出。
- python3 -B derived_check.py demo --directory derived-demo：实际采集、派生、关闭重开、核账、导出并显示双侧来源。

交付实现、简短改写与来源不变量说明及相应回归。除所给正常链路、非法目标/映射、冲突、提交失败、重开和导出核验外，按自己选用的存储表示补充可重跑损坏核验：改坏派生字节、固定映射和父绑定时核账拒绝，不能依靠测试指定某种表名。覆盖新值等于另一个旧值时不连锁替换、原值映射到自身及UI奇偶填充。所给样例不是完整合法输入集合。

现有.pointwise-runtime.json和run-manifest.json的version=1、kind=cli、setup为argv数组列表、verify/demo为单个argv数组；保留该格式。将真实原回归与派生核验共同接入verify，demo展示上述双文件重开后的实际交付，可扩展run.py组织这些入口。运行清单不能仅回显成功。
