# 示例卡片目录

接新项目时，把这个 `tasks/` 整个拷到你的项目里（比如 `E:/Code/你的项目/task`），
然后在根目录的 `config.json` 里指过去即可：

```json
{
  "tasks_dir": "E:/Code/你的项目/task",
  "assets_dir": "E:/Code/你的项目/task/assets",
  "project_root": "E:/Code/你的项目"
}
```

`assets_dir` 不配也行，默认就是 `<tasks_dir>/assets`。

## 目录就是状态

卡片没有 `status` 字段，**放在哪个子目录就是什么状态**：

| 目录 | 含义 | 谁会动它 |
| --- | --- | --- |
| `0-待确认/` | 人工闸门：调度器永不拾取，人点「开始」才入队 | 你 |
| `1-待开始/` | 调度器的唯一入口队列 | 调度器领取 |
| `2-进行中/` | Agent 正在处理 | 调度器 |
| `3-待审核/` | Agent 已交卷，等你通过或打回 | 你 |
| `4-已发布/` | 终态 | — |
| `5-需介入/` | 执行失败 / 超时 / Agent 报告受阻 | 你 |

空目录（`2-进行中` `5-需介入`）里的 `.gitkeep` 只是为了让 git 能提交空目录，可删。

## 卡片格式

文件名为 `<卡片ID>.md`（如 `T-102.md`），正文是 frontmatter + 三个固定小节：

```markdown
---
id: T-102
title: "一句话说清要干什么"
priority: 高          # 高 / 中 / 低
model: ""            # 可选：单卡指定模型，留空跟随全局默认
comment_round: 0     # 被打回的次数
review_round: 0      # Agent 交卷的次数
claimed_at: ""       # 调度器领取时间
updated_at: "2026-09-20T16:00:00"
outcome: ""          # 最近一轮的结果：done / blocked / failed ...
---

## 需求

## 处理意见

## 执行结果
```

`model` 是唯一的可选字段，`comment_round` / `review_round` / `claimed_at` / `outcome`
由调度器维护，新建时给上面的默认值就行。

## 贴图

看板里粘贴 / 拖入的截图会存到 `assets/<卡片ID>/`，正文里写成**相对路径**：

```markdown
![](../assets/T-102/20260920-160000-a1b2c3.png)
```

用相对路径（而不是 `E:\...` 绝对路径）是为了让图片在 VS Code / Typora 一类的
Markdown 编辑器里也能正常显示——绝对路径里的反斜杠会被当成转义符吃掉。
所有状态目录都是 `tasks/` 的一级子目录，深度一致，所以卡片在状态间搬动时
`../assets/...` 依然成立。
