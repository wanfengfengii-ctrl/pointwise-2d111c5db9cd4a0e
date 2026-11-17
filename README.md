# Offline image acquisition

区域影像研究资料室的资料员接收离线 DICOM 导出；研究员取得逐帧原始 JPEG；复核员从归档原文件重建来源。本工程仅使用 Python 3.11+ 标准库，从项目目录运行，无需安装或网络。

一天工作为：提交完整文件并固定字节快照；精确核对来源身份；解释顶层元数据与序列；建立封装项目目录；按 JPEG 语法分帧并投影来源；核账后事务发布；复核原附件；导出固定研究资料。采集不会解码或重编码像素。

## 运行

- 完整回归：`python3 -B run.py verify`
- 实际持久演示：`python3 -B run.py demo --directory demo-output`
- 导入：`python3 -B run.py ingest --db archive.sqlite --source-id device-A export.dcm`
- 清单：`python3 -B run.py read --db archive.sqlite CAPTURE_ID`
- 帧（JSON 中 body_hex 为原字节）：`python3 -B run.py read --db archive.sqlite CAPTURE_ID --frame 0`
- 复核：`python3 -B run.py audit --db archive.sqlite CAPTURE_ID`
- 导出：`python3 -B run.py export --db archive.sqlite CAPTURE_ID delivery-directory`

数据库父目录和导出父目录须存在。demo 会建立指定工作目录，重复初始化和同字节重投返回同一回执；重复运行验证既有交付字节。已有目标不被覆盖。CLI 业务、缺失、I/O 或 SQLite 错误输出 error JSON 并退出 2；回归失败退出 1。

## 格式契约

接受 Part 10（128 字节前导及 DICM）文件，文件元信息与数据集均为显式 VR 小端；TransferSyntaxUID 必须为 1.2.840.10008.1.2.4.50。可选文件元信息组长须准确；可选 MediaStorageSOPClassUID / MediaStorageSOPInstanceUID 须与数据集一致。仅 unsigned 8-bit JPEG Baseline Process 1 封装，不支持扩展偏移表、其他语法、像素解码或网络服务。

必要顶层元素：SOPClassUID (0008,0016)、SOPInstanceUID (0008,0018) 为 UI；SamplesPerPixel (0028,0002)、Rows (0028,0010)、Columns (0028,0011)、BitsAllocated/BitsStored/HighBit/PixelRepresentation (0028,0100—0103) 为单值 US。行列正数，像素属性为 8/8/7/0。PhotometricInterpretation (0028,0004) 为 CS；单分量为 MONOCHROME1/2，三分量为 RGB、YBR_FULL 或 YBR_FULL_422，PlanarConfiguration 缺省或为 0。NumberOfFrames (0028,0008) 缺失为 1；出现时为不超过 12 字符的完整正 IS，允许外围空格及正号。矛盾重复值拒绝。UI 仅移除单个尾 NUL，CS 移除 VR 尾空格。未知普通元素按已验证偶数长度跳过；SQ 的有界/未定义长度及项目层级仍完整校验，嵌套值不参与顶层元数据。

Pixel Data (7FE0,0010) 必须为未定义长度 OB。第一项目为基本偏移表（0 或 4 的倍数字节），后续为正偶数长度分片，最后为零长度序列定界符。非空表条目严格递增，首项 0，数量等于帧数，以首分片项目标签为零点，包含每个 8 字节项目头；每项必须指向实际 JPEG 帧首项目。空表按语法分帧，不按项目数猜帧。

JPEG 按 SOI、SOF0、DQT、DHT、DRI、SOS、EOI 解释，支持 APP/COM、不交错和交错的 Baseline 顺序扫描。SOF0 与 DICOM 尺寸及分量一致；量化/霍夫曼表必须先于引用定义，SOS 分量不可重复且须覆盖 SOF0 全部分量。扫描识别 FF00、RST0—RST7 与 FF 标记填充；有重启须有 DRI 且编号连续。长度限定段中的 FFD9 是正文。标记及长度可跨项目。检查帧级结构、编码表及引用，不验证熵码解码后的像素、MCU 数量或影像质量。仅真实 EOI 之后、该帧末项目最后为偶数长度添加的一个 00 可从正文排除；其他垃圾拒绝。原附件保留填充。

全部位置以字节计，范围为左闭右开 [start,end)，帧索引从 0 连续。逻辑载荷不含项目头，来源按消费顺序投影，不排序改写。完整帧 SHA-256 为 64 字符小写十六进制；来源逐字重建等于正文。相同帧换合法分片，正文和摘要不变，原文件区间随布局改变。无随机数或时间参与身份/正文计算。

## Python API 与固定成果

```python
from acquisition.archive.store import ArchiveStore
from acquisition.delivery.batch import CaptureService
from acquisition.archive.read import CaptureReader
from acquisition.delivery.audit import EvidenceAuditor
from acquisition.delivery.export import CaptureExporter

with ArchiveStore("archive.sqlite") as archive:
    receipt = CaptureService(archive).ingest("device-A", file_bytes)
    manifest = CaptureReader(archive).read(receipt.capture_id)
    frame = CaptureReader(archive).read(receipt.capture_id, 0)
    results = EvidenceAuditor(archive).audit(receipt.capture_id)
    delivery = CaptureExporter(archive).export(receipt.capture_id, "new-delivery")
```

source_id 是不透明非空字符串，精确保留大小写和空格。输入 bytes、bytearray、memoryview 在接收时复制为 bytes。回执 SourceReceipt 含 source_id、source_digest、attachment_id、capture_id、frame_count；同身份同字节返回原回执，异字节抛 SourceConflict，摘要之外还逐字比较。新身份即使同字节也有独立成果。顺序 ingest_batch 返回逐输入 BatchItemResult；业务失败不撤回此前成功项，I/O 异常传播。

非法格式抛 CaptureError；读取不存在成果/帧抛 NotFound；损坏抛 IntegrityError。异常分别定义在 receipt/source.py、receipt/identity.py 和 archive/schema.py。read 清单为独立 JSON 对象，帧为 ArchivedFrame（index、body、digest、有序 SourceSpan 元组）；audit 返回 AuditFrameResult 元组。export 返回 DeliveryManifest，输出数值顺序的 frame-000000.jpg 等原字节和 manifest.json。

receipt 固定身份；dataset 拥有元数据和像素入口；encapsulation 拥有项目坐标、载荷映射和原位置；frames 拥有语法边界、正文及来源组合；archive 拥有 SQLite 连接、固定清单及原子提交；delivery 组织批次、复核和交付。每包的四个模块均进入上述入口调用链。

原附件 BLOB、清单、全部帧和来源、回执同事务提交；准备状态不对读者可见。失败不占用来源身份，旧成果不变。只追加成果，无修改已发布数据接口；重复初始化不清库，不兼容 schema 明确拒绝，不自动迁移。读取与复核检查附件、正文、来源和关联，不重新提取或自动修复损坏；后续导入不改变旧 capture_id。单工作者离线工作流，不提供并发导入保证。导出先复核，再同级暂存完整写出后改名，失败仅清理本次暂存并传播 I/O 异常；已有目标拒绝覆盖。

## 样例和回归

samples/normal_case.json 指定有限双帧样例；tests/fixtures.py 以显式 DICOM 元素、JPEG 段和编码比特构造二进制。APP/COM 含伪 EOI，扫描含转义、重启及标记填充。预期来源由独立逐字节原地址枚举得到，未调用生产解析器生成答案。demo 实际采集、重开、重复初始化、重投、复核和导出，随后以新来源核对旧成果固定性。

完整套件保留 v1—v8：test_intake 覆盖接收、多位帧数、单帧缺省、序列层级、精确身份和顺序批次；test_frames 覆盖空/非空偏移表、跨项目交集、尾填充、各偶数切点与非法格式整次拒绝；test_archive 覆盖各写入阶段和提交前失败、重开无残留、重试、重复初始化及 schema 拒绝；test_delivery 覆盖新进程禁止导入提取器时的读取与导出、12 帧数值顺序、固定旧成果、导出失败/目标冲突、原证据/正文/来源/关联损坏。正常断言后才输出 POINTWISE_SCENARIO JSON。

受控故障注入为真实操作阶段回调：ArchiveStore(path, fault=callback) 在 source、capture、frame:N、receipt、before_commit 阶段调用；CaptureExporter(store, fault=callback) 在 frame:N、before_publish 阶段调用。回调抛出异常，生产事务/暂存清理照常执行，测试不替代解析器、数据库或文件写出。
