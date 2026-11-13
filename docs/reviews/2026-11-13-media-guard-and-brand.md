# 媒体守卫误伤与品牌收口

日期：2026-11-13。关联 FR-08、ENG-01。

## 媒体守卫误伤

`source_capture.py` 的 `_reject_unsupported_media` 用于在采集阶段拒绝含图片／附件
的记录，避免上游文本解析器静默丢弃媒体块后仍被当成完整导入。原实现按「任一 dict
的 `type` 属于媒体类型」或「任一 dict 有非空 `attachments`」判定，会误伤真实会话：

- 工具结果里内嵌的 JSON Schema 片段，`type` 常是列表（如 `["object", "null"]`），
  会直接抛 `TypeError` 并折成 `source_record_invalid`，整批采集中断；
- JSON Schema 的属性名可以叫 `attachments`，被当成真实附件。

新增 `_is_media_block`：只接受字符串类型且属于媒体集合，`attachments` 必须是
非空列表且同层不含 schema 关键字。新增 `tests/test_media_guard.py` 覆盖 3 个真实
媒体拒绝与 7 个非媒体放行（含两种 schema `type` 与 schema 属性名
`attachments`）。

验证：`test_media_guard.py` 与 `test_source_capture.py` 合计 31 通过。

## 品牌收口

`index.html` 的 `<title>` 是 `MemHub`，在 JS 加载前显示；控制台侧栏品牌是
`MemoryHub`。两者与产品名不一致。

补丁 `0054-hub-brand-alignment.patch` 把页面标题与控制台侧栏统一为
`Cloud Memory Hub`。上游 `LICENSE`／`NOTICE` 与代码署名保持不变，符合 Apache-2.0
要求；旧桌面壳（`App.tsx` 等）中的 `MemHub` 按其上游身份保留，不属本产品界面。

验证：`HubConsole.test.tsx` 22 通过；54 个补丁从干净检出准备通过。

## 未覆盖

`One Memory` 等其它上游残留文案存在于非控制台组件，随旧壳一起保留；若将来彻底移除
旧壳入口，应一并清理。
