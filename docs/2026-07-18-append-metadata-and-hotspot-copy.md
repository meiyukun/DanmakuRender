# 追加稿件信息回填与热点切片文案优化

## 改动摘要

- 上传中心选择“追加到已有视频”后，会读取目标稿件的完整可编辑信息并回填标题、简介、动态、标签、分区、稿件类型、来源、可见性、定时信息和合集信息。
- 原稿信息默认原样保留；用户可以在追加前编辑，最终稿件编辑请求会同时应用这些修改。
- 读取稿件当前所在的合集、小节和单集标题。保持原值时不调用合集写接口；修改或清空后同步合集归属。
- 手动填写 BV 号时同样需要先成功读取稿件详情，避免空表单意外覆盖原稿信息。
- 优化 AI 一键生成提示词：标题聚焦真实热点事件、反转、反应或金句；简介和动态改为短句，禁止空泛运营套话及素材文件名拼接；封面大字缩短并从真实热点提炼。
- AI 文案上下文加入主播名称；热点分析会为候选片段保存前后约6秒范围内的已有字幕正文。上传中心也会为旧热点任务尝试从仍保留的字幕路径读取正文，不会启动新的语音转写。
- 字幕中的原话可作为标题、动态或封面大字的亮点，但提示词要求忠于字幕，禁止补写不存在的台词。

## 涉及文件

- `DMR/WebService/webapi.py`
- `DMR/WebService/static/uploads.js`
- `DMR/WebService/templates/uploads.html`
- `DMR/Uploader/biliwebapi.py`
- `DMR/Uploader/biliapi/biliapi.py`
- `DMR/Uploader/biliapi/bili_section.py`
- `DMR/WebService/test_webapi.py`
- `DMR/Uploader/test_biliapi.py`
- `DMR/Uploader/test_uploader.py`
- `docs/2026-07-18-append-metadata-and-hotspot-copy.md`
