# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见和交付快照。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 翻译/时间轴成员提交复核，分配的非创建人复核人批准或退回。
6. 负责人锁定已批准版本，再执行交付。
7. 交付时生成确定性的 SHA-256 快照；同语言的新交付会把旧版本标记为 `superseded`，但旧快照不会删除或覆盖。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## 平台交付规格

同一语言版本要同时发往 25 帧（PAL）和 23.976 帧（NTSC）平台时，为每个平台生成独立交付规格，无需复制整版手改：

- 采用帧保持换算（`24→25` 成片加速 4%、`24→23.976` 放慢 0.1%），保存源帧率、目标帧率和每句换算后的起止帧/毫秒。
- 规格详情把**原时码、新时码（SMPTE `HH:MM:SS:FF`）和文字**逐句并排显示。
- 换算后逐句校验：句子之间**重叠**、末帧**越界**（超过成片末帧）、起止落在同一帧的**空时长**；命中时返回问题句号和说明，规格标记为 `blocked`，交付时返回 409 并指出问题句数量。
- 规格生成时把当时的字幕、时间轴和版本修订号完整快照入库（`source_revision` + SHA-256）。原内容之后再改，已保存规格仍按当时时间轴保留。
- 同版本可建多个规格（平台名 + 目标帧率唯一），可分别查询与交付；交付要求关联版本已批准/锁定，且规格无问题句。

平台规格接口（均使用 `X-User`/`X-Role`）：

- `POST /api/versions/{id}/platform-specs`：负责人建立规格，body 含 `platform_name`、`source_fps`（默认 24）、`target_fps`（如 `25`、`23.976`）。
- `GET /api/platform-specs?version_id={id}`：列出版本下全部规格及其状态。
- `GET /api/platform-specs/{id}`：查询单个规格，含逐句原/新时码与问题列表。
- `POST /api/platform-specs/{id}/deliver`：交付规格；阻断状态或版本未通过复核时拒绝。


## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机。
- `GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`：查看结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用和人员权限。
