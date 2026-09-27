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
8. 同一语言版本需要发给不同帧率平台时，负责人为每个平台建立交付规格：按项目成片帧率（`frame_rate`，默认 25）换算到目标帧率，保存换算后的起止时间和平台片长。规格页面把句号、原时码、新时码和文字并排展示；换算后出现重叠、越界或空时长会标出句号并把规格标记为 `blocked`，阻止该规格交付。规格生成即快照，原版本之后再改字幕不影响已保存结果（规格记录当时的 `source_revision`），同一平台可多次生成递增序号的规格，多个规格可分别查询和交付。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机。
- `POST /api/versions/{id}/specs`：为平台建立交付规格（`platform`、`target_fps`，可选 `media_duration_ms`），保存帧率和换算后的起止时间快照。
- `GET /api/versions/{id}/specs`、`GET /api/specs/{id}`：分别查询多个规格；单条规格返回原时码、新时码和文字并排的逐句结果及阻断问题。
- `POST /api/specs/{id}/deliver`：交付规格；存在重叠、越界或空时长问题时返回 409 并指出句号。
- `GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`：查看结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用和人员权限，以及交付规格的帧率换算、快照保留、重叠/越界/空时长阻断和多规格分别查询。
