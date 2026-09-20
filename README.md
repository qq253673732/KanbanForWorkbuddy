# KanbanForWorkbuddy · 看板任务池

单进程完成三件事：看板 UI、内嵌调度器（拉起 WorkBuddy CLI 无头执行卡片）、文件移动实现的状态流转。

启动：`python serve.py`（默认 8770；`python serve.py 9000` 换端口）。

> **本机文件不入库**：根目录的 `config.json`、`projects.json`（含绝对路径与 CLI 安装位置）
> 已加入 `.gitignore`，首次启动会从同名 `.example.json` 模板自动生成。详见下方「配置项」章节。

---

## 架构：单进程，两条触发路径，一份 prompt

```
serve.py（常驻）
├─ HTTP 服务  ── 看板 UI（127.0.0.1:8770）
└─ 内嵌调度器 ── 每 N 秒扫一次 1-待开始/，有空闲槽位就领卡，每张卡一条后台线程拉起 CLI
```

启动：`python serve.py`（默认 8770；`python serve.py 9000` 换端口）。

工具本体和运行数据分两处：

| 位置 | 内容 |
|---|---|
| 项目根（`serve.py`、`automation/`、`example/`） | 代码、prompt、示例卡片 |
| 项目根（`config.json`、`projects.json`） | **本机配置，不入库**（模板是 `*.example.json`） |
| `runtime/`（`.runs/`、`.run-state.json`） | 运行数据：执行转录、调度器状态 |
| **`config.json` 的 `tasks_dir`** | **卡片目录，可以指向别处（比如业务项目的 `task/`），默认 `runtime/tasks`** |
| **`config.json` 的 `assets_dir`** | **贴图目录，默认 `<tasks_dir>/assets`** |

卡片、贴图都可以和工具分家：卡片想放在哪个项目里就放哪，贴图跟着卡片走。
正文里的图片写成**相对卡片文件**的路径 `![](../assets/<卡片ID>/xxx.png)`——
绝对路径的反斜杠在 Markdown 里是转义符，编辑器会把它吃掉导致图显示不出来；
相对路径则换机器、换盘符、卡片在状态目录间移动都不失效。

接新项目时照抄 `example/tasks/`（内含各状态的示例卡片，见 `example/README.md`）。

启动即同时启动调度器，不需要 Windows 计划任务，也不依赖 WorkBuddy 的自动化。

| 触发方式 | 说明 |
|---|---|
| **自动轮询** | 内嵌线程，默认每 60 秒扫一次 `1-待开始/`，每轮把空闲并发槽位填满 |
| **手动触发** | 看板「立即执行一轮」按钮 → 立刻唤醒调度器，不等下一个周期 |
| 新建卡片 / 打回 | 也会立刻唤醒调度器 |
| 任务跑完 | 也会唤醒调度器，立刻补位领下一张 |

这几条路径最终都走同一个函数 `scan_once()`，行为完全一致。prompt 只有一份：`executor.prompt.txt`。

> 轮询关闭时「立即执行一轮」依然有效：调度器在 `auto_scan=false` 时不再按周期扫描，
> 但仍会响应唤醒信号跑一轮。


---

## 状态即目录

```
<tasks_dir>/          ← config.json 里配置，例如 D:\Code\eshop\task（或直接用页面上的「＋ 新建项目…」选目录）
├─ 0-待确认/   人工闸门：调度器永不拾取，需人点「开始」才入队（草稿 / 待定口径）
├─ 1-待开始/   新任务与返工任务的唯一入口队列
├─ 2-进行中/   Agent 正在处理
├─ 3-待审核/   Agent 已完成，等人审核
├─ 4-已发布/   终态
└─ 5-需介入/   执行失败、超时、被中断，或 Agent 报告受阻
```

**卡片 frontmatter 里没有 `status` 字段**——目录才是状态的唯一权威来源，避免双写不一致。

### 流转规则

| 动作 | 谁执行 | 变化 |
|---|---|---|
| 新建卡片 · 开始 | 你（看板） | 落入 `1-待开始/` |
| 新建卡片 · 保存草稿 | 你（看板） | 落入 `0-待确认/`，调度器永不拾取，人点「开始」才入队 |
| 领取 | 调度器 | `os.rename` 原子移动到 `2-进行中/`，写 `claimed_at`，清空 `outcome` |
| 完成 | 调度器 | 退出码 0 **且** `outcome: done` → `3-待审核/`，`review_round = comment_round` |
| 打回 | 你（看板） | 追加意见、`comment_round + 1`、移回 `1-待开始/` |
| 通过 | 你（看板） | 移动到 `4-已发布/` |
| 失败/受阻 | 调度器 | 移入 `5-需介入/`，并把原因写进「执行结果」 |
| 退回重试 | 你（看板） | 「需介入」卡片可一键退回 `1-待开始/` |

**领取靠 `os.rename` 的原子性**：同一文件系统内，两个进程不可能同时移动同一个文件，抢输的那个拿到 `FileNotFoundError`。不需要额外的锁文件。

---

## 并发执行（同时跑 N 张卡）

看板右上角的「同时任务数」下拉框（`1`–`8`，默认 **3**，存在 `config.json` 的 `max_concurrent`）。

调度器的一轮扫描会把空闲槽位填满：只要 `运行中任务数 < max_concurrent`，就继续 `pick_next()` 领下一张，
每领到一张就 `claim()`（原子改名到 `2-进行中/`）并起一条后台线程跑它，**不等它跑完**。

```
scanner_loop ──▶ scan_once(limit)
                   ├─ 槽位没满? ──▶ pick_next() ──▶ claim() ──▶ 起线程 ──┐
                   ├─ 槽位没满? ──▶ pick_next() ──▶ claim() ──▶ 起线程 ──┤ 循环到满或没卡
                   └─ 满了 ──▶ 返回 "full"                               │
                                                                        ▼
                              run_task(线程) ──▶ run_process ──▶ finalize ──▶ _scan_wake.set()（腾位即补）
```

| 关注点 | 做法 |
|---|---|
| 会不会重复领同一张卡 | 不会。领取靠 `os.rename` 原子移动，且 `pick_next()` 只从 `1-待开始/` 里取 |
| 运行表 | `_state["tasks"]`：`card_id -> {log, model, started_at, title}`，每个任务一个独立转录文件 |
| 转录文件重名 | 文件名精确到毫秒（`%Y%m%d-%H%M%S-%f`），同一秒起两个任务也不冲突 |
| 状态栏 / 按钮 | 显示 `执行中 2/3：T-001（已跑 12 秒）、T-002（…）`；槽位满时按钮变成「并发已满 2/3」并禁用 |
| 调低并发 | 下拉框里低于「当前运行数」的选项会被禁用，避免把上限压到正在跑的任务数以下 |
| 「立即执行一轮」 | 只在还有空槽时接受，`active >= limit` 时直接提示并拒绝 |

**每张卡的实时反馈是独立的**：`GET /api/live?card=<ID>` 各自维护游标，
看板上「进行中」的每张卡都有自己的 **AI 实时反馈** 面板，互不串流。

---

## 分工：谁写什么

| 角色 | 独占 |
|---|---|
| 调度器（serve.py） | `comment_round`、`review_round`、`claimed_at`、`updated_at`、文件位置 |
| Agent | 「执行结果」小节、`outcome` 字段 |
| 你 | 「需求」小节、追加「处理意见」、点击通过/打回 |

Agent **完全不碰状态**——它既不知道也不需要知道卡片在哪个目录。这是刻意的：执行者不该有权决定自己的产物是否合格。

---

## outcome：由 Agent 声明执行结果

Agent 完成后必须写一个字段：

| 值 | 含义 | 调度器动作 |
|---|---|---|
| `done` | 任务已按要求完成 | 移入 `3-待审核/` |
| `blocked` | 未能完成（缺信息 / 有阻塞 / 需人确认） | 移入 `5-需介入/` |

**为什么不能只看退出码**：CLI 即使内部报错也返回 0。所以「成功」需要**退出码 0 且 outcome=done** 双条件。否则一个如实报告"我做不完"的 Agent 会被误判成完成，直接送进待审核。

---

## 手动执行的实际命令

```
<node> <WorkBuddy安装目录>/resources/app.asar.unpacked/cli/bin/codebuddy \
  --print \
  --output-format stream-json \
  --include-partial-messages \
  --permission-mode bypassPermissions \
  --model <config.json 里的 model> \
  --fallback-model <config.json 里的 fallback_model>   # 主模型不可用时自动降级
  --max-turns 80 \
  "<executor.prompt.txt 的内容，{{CARD_ID}} / {{CARD_PATH}} 已替换>"
```

模板变量由 Python 用 `str.replace` 替换（**不要用 `format`/f-string**，prompt 里有其他花括号）。若替换后仍残留 `{{...}}`，调度器会直接报错而不是带着坏 prompt 去跑。

**关键点：必须是 `--output-format stream-json`。** 默认的 `text` 输出格式下，`--print` 只在进程结束时才吐一次最终文本——这就是「进行中看不到任何 AI 反馈」的根本原因。

---

## 实时反馈（进行中能看到 AI 在做什么）

调度器不再等进程结束，而是**边跑边读** CLI 的 stdout：

```
CLI stdout ──逐行──▶ 读取线程 ──▶ StreamTranslator ──┬──▶ .runs/<ts>-<card>.log   可读转录（看板展示 / 人看）
                                                     └──▶ .runs/<ts>-<card>.jsonl 原始事件流（排查用）
                                                              │
看板前端 ──每 1.2s──▶ GET /api/live?offset=<游标>&card=<ID> ──┘  只回传增量
```

转录长这样（`【思考】` / `▶ 调用工具` / `⤷ 工具结果` / `【回复】` / `■ 本轮结束`）：

```
[11:06:33] ▶ 调用 Write  {"file_path": "…\\proj\\f05.txt", "content": "1\n2\n3\n"}
[11:06:33] ⤷ Write 完成 · Successfully created and wrote to new file: …\proj\f05.txt
[11:06:37] 【回复】
Now verifying line counts for each file.
[11:06:39] ▶ 调用 PowerShell  {"command": "foreach ($i in 1..8) { … }"}
[11:06:39] ⤷ PowerShell 完成 · f01.txt: 3 lines f02.txt: 3 lines …
```

看板上有三处能看到它：

| 位置 | 内容 |
|---|---|
| 「2-进行中」卡片内的 **AI 实时反馈** 面板 | 当前任务的逐字流式转录，自动跟随滚动（并发时每张卡各一个面板） |
| 顶部 **AI 实时转录** 折叠区 | 跟随「最近启动且仍在跑」的那条转录 |
| 卡片上的 **AI 执行日志** 按钮 | 任意历史卡片弹窗看完整转录（按卡片 ID 找最近一次的 `.runs/*-<ID>.log`） |

### `GET /api/live`

| 参数 | 说明 |
|---|---|
| `offset` | 前端持有的字符游标，服务端只回传 `offset` 之后的内容；**每张卡各自维护一个游标** |
| `card` | 指定卡片 ID（并发时必须传，否则只会拿到其中一张）。不传则落在最早启动的正在跑的卡 |

返回 `{card, running, size, offset, text, reset, started_at, log}`。
`reset: true` 表示前端落后太多（或转录被截断），应清空后整体替换。

### 相关配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `live_partial` | `true` | 传 `--include-partial-messages`，思考/正文**逐字**回传。关掉则退化为「每条消息」粒度，流量小很多 |
| `live_show_thinking` | `true` | 是否把模型的思考过程也写进转录（`【思考】` 段） |

---

## 贴图：聊天框那样的附件标签

需求/处理意见里粘贴的截图，上传后存到 `<assets_dir>/<卡片ID>/`，Markdown 引用写成
相对路径 `![](../assets/<卡片ID>/xxx.png)`，在页面上**不再显示成一串路径文本**，而是渲染成一个附件标签：

| 交互 | 效果 |
|---|---|
| 静止 | 一个胶囊标签：图标 + 文件名（`design-shot.png`） |
| 悬停 | 旁边浮出缩略预览（fixed 定位、自动避让视口边缘，页面一滚动就收起） |
| 点击 / 回车 | 弹出大图（最大 74vh），带「在新标签打开」 |
| 文件不存在 | 标成灰态 + `已失效` 徽标，不可点，免得点了没反应还不知道为什么 |

服务端 `render_rich()` 做文本渲染：普通文本转义后原样输出，
只有 `![...](...)` 被替换成标签，所以正文里的 `<b>` 之类不会被当 HTML 解析。
取图仍走 `GET /api/image?path=`，该接口只允许贴图目录内的文件，防任意文件读取。
路径解析见 `resolve_image_ref()`：相对路径以 `tasks_dir` 为基准，绝对路径（老卡片）照旧兼容。


### 为什么不能把 AI 反馈发到 WorkBuddy 对话框

查证过：WorkBuddy 桌面端的会话索引在 `~/.workbuddy/app/sessions.json`（`conversationId` + `workDir`），而 CLI 无头进程的会话落在 `~/.codebuddy/projects/<cwd 转义>/<session_id>.jsonl`。
**两套存储不互通**——桌面端那些 `conversationId` 在 CLI 的项目目录里找不到对应文件。所以没有官方通道让一个外部进程往你正在聊的那个对话框里插消息；硬改 app 的内部状态既不可靠（要重启才刷新），也可能把会话记录搞坏。

结论：走「看板页面实时显示」这条路，也就是上面这套。

---

## 配置项（根目录 config.json）

> **本机文件，不入库。** 仓库里只有脱敏模板 `config.example.json` / `projects.example.json`；首次启动 `serve.py` 会自动用模板生成本机的
> `config.json` / `projects.json`，之后怎么改都不会污染远端。
> 换机器 clone 下来 → 跑一遍 `python serve.py` → 在页面上填项目目录即可。

| 字段 | 说明 |
|---|---|
| `tasks_dir` | **卡片目录**，可指向任意位置（默认 `runtime/tasks`）。改完重启生效 |
| `assets_dir` | **贴图目录**（默认 `<tasks_dir>/assets`，即跟卡片走）。卡片正文用相对路径 `../assets/...` 引用 |
| `project_root` | Agent 读写的项目根，务必显式配置；没配时取 `tasks_dir` 的上一级 |
| `cli_path` | WorkBuddy CLI 路径，应用升级后可能需要更新 |
| `node_path` | 留空则自动探测（PATH → `.workbuddy/binaries/node`） |
| `permission_mode` | 传给 CLI 的权限模式，无人值守需要 `bypassPermissions` |

### 多项目（projects.json）

页头「项目」下拉可切换 / 新建项目。项目清单存在根目录的 `projects.json`（同样不入库）：

```json
{
  "active": "eshop",
  "projects": {
    "eshop": { "root": "D:/Code/eshop", "tasks_dir": "D:/Code/eshop/task", "assets_dir": "D:/Code/eshop/task/assets" }
  }
}
```

- 切换项目 = 改 `active`，并把该项目的路径同步写回 `config.json`（兼容字段），卡片、贴图、Agent 读写范围全部跟随
- 新建项目在页面上走「＋ 新建项目…」：服务端弹原生文件夹选择框（浏览器拿不到本地绝对路径）→ 填名字、选 task 目录位置（项目下 / 看板 `projects/` 下）→ 自动建目录并初始化六个状态目录
- 老安装无 `projects.json` 时，首次启动自动从 `config.json` 迁移出第一个项目
| `model` | 看板下拉框选中项，也是每次执行的模型 |
| `fallback_model` | 主模型不可用时自动降级（`--fallback-model`） |
| `available_models` | 下拉框可选项 |
| `max_turns` | 单轮最大 agentic 轮数 |
| `timeout_sec` | 单轮超时秒数，超时强杀（退出码 -1）；**每个任务各算各的** |
| `auto_scan` | 自动轮询开关，看板右上角可切换，改完立即生效 |
| `scan_interval_sec` | 轮询间隔，默认 60 秒（下限 10 秒） |
| `max_concurrent` | 同时执行的任务数，默认 3（上限 8），看板右上角可调 |

---

## 退出码与失败处理

| 退出码 | 含义 |
|---|---|
| `0` | 正常结束（**仍需 outcome=done 才算成功**） |
| `-1` | 超过 `timeout_sec`，被强制终止 |
| `-2` | CLI 启动失败或异常 |

失败时，调度器会把以下内容追加到卡片的「执行结果」，并移入 `5-需介入/`：

- 退出码或超时说明
- `outcome` 无效时的提示
- **日志末行**（例如 provider 报错原文），不用翻日志就能看到原因

### 启动时的孤儿清扫

`2-进行中/` 里如果有残留卡片（说明上次进程被中断），启动时自动移入 `5-需介入/` 并注明，不会静默卡死。

---

## 已知边界

1. **依赖本机 WorkBuddy 客户端与 CLI**，复用其登录态
2. 每次执行是独立进程，冷启动约 7–10 秒
3. **并发数受机器和 CLI 承受能力限制**：每个任务都是一个独立的 node 进程（各自 100–300 MB 内存）。
   默认 3 是保守值；不建议长时间开满 8。
4. CLI 启动时会报一条远程配置拉取失败（`fetch config failed ... 400`），**不影响执行**
5. 看板只监听 127.0.0.1，仅本机可用
6. **模型可用性受 provider 影响**——2026-09-10 实测 `deepseek-v4.1-flash` 出现过 500 不可用，`hy3-x` 正常。建议配置 `fallback_model`

### 排查：卡片卡在「进行中」且转录一个字节都没有

症状：卡片进了 `2-进行中`，**AI 实时反馈面板一直空着**，`.runs/*.log` 只有 6 行表头、
`.jsonl` 是 0 字节，状态栏时间一直在涨。

先绕开看板手工验一次 CLI（换你自己的项目目录跑，`<node>` / `<cli>` 按 config.json 填）：

```bash
<node> <cli> --print --output-format stream-json --permission-mode bypassPermissions --model hy3 "只回答两个字：收到"
```

- **几秒内就有输出** → 问题在看板侧（看 `serve.py` 的控制台输出、检查 `.runs/*.jsonl`）
- **同样一个字都不吐、挂住** → 问题在 CLI / 桌面端，跟看板无关。
  2026-09-11 遇到过这种：CLI 起得来但连不上桌面端宿主（`~/.workbuddy/sessions/<pid>.json` 里
  没有 `endpoint` 字段，对比正常会话是有 `url` / `endpoint` / `version` 的）。
  **重启 WorkBuddy 桌面端**后恢复。看板侧无需改动。

---

## 注意：与 WorkBuddy 自动化的冲突

WorkBuddy 里那条「看板任务池执行器」自动化**存的是旧版 prompt**（让 Agent 自己扫描 `tasks/*.md`、自己改 `status` 字段）。目录化改造后这套逻辑已经失效，**启用它会和调度器互相干扰**。建议停用或删除。

调度器不需要它——轮询已经由 serve.py 内嵌实现。

---

## 变更记录

### 2026-09-11 · 并发执行 + 附件标签

新增：

- **并发执行**：`max_concurrent`（默认 3，看板右上角可调 1–8）。
  `scan_once(limit)` 填满空闲槽位，每张卡一条后台线程；`_state["tasks"]` 记录运行表，
  `finalize()` 按任务摘表、腾槽后唤醒调度器补位。旧的 `_agent_lock` 串行锁已移除。
- **每卡独立的实时反馈**：`/api/live` 按 `card` 分别取数，前端 `liveOffsets` 按卡维护游标，
  进行中的卡片各有一个「AI 实时反馈」面板。
- **转录文件名精确到毫秒**，避免同秒起两个任务时日志互相覆盖。
- **附件标签**：`render_rich()` 把 `![](路径)` 渲染成胶囊标签（悬停浮层预览、点击看大图、
  文件缺失显示「已失效」）。删掉了原来的 `.thumbs` 缩略图网格（被标签取代）。
- 状态栏 / 按钮支持多任务：`执行中 2/3：T-001（已跑 12 秒）、T-002（…）`，满了显示「并发已满 2/3」。

> 验证方式：并发调度用桩替换 `run_process` 做隔离测试（4 张卡、上限 2，确认峰值并发=2、
> 最终两张都落 `3-待审核`、日志不重名）；页面用 CDP 驱动无头 Chrome 实测
> 悬停浮层 / 点击大图 / 两路实时面板，控制台零报错。

### 2026-09-11 · 实时反馈 + 两个既有 bug

新增：

- CLI 改为 `--output-format stream-json [--include-partial-messages]`，stdout 由读取线程逐行翻译成可读转录
- `.runs/` 下同时产出可读 `.log` 和原始 `.jsonl`
- 新增 `GET /api/live`（游标增量）
- 看板：进行中卡片内嵌 **AI 实时反馈** 面板、顶部 **AI 实时转录**、每张卡片的 **AI 执行日志** 弹窗
- 配置项 `live_partial` / `live_show_thinking`

修掉的两个既有 bug（都不是本次新引入的）：

1. **整个页面脚本语法错误、前端全死**
   `HTML_TEMPLATE` 用的是普通三引号字符串，JS 里的 `'\n'` 被 Python 提前转义成真换行，
   页面 `<script>` 一开头就 `SyntaxError` —— 于是轮询、新建卡片、审核通过/打回、立即执行一轮
   **全部静默失效**（服务端渲染的卡片还在，所以表面看不出来）。
   修法：`HTML_TEMPLATE = r"""…"""`（raw 字符串）。
2. **轮询关闭时「立即执行一轮」空转**
   `scanner_loop` 在 `auto_scan=false` 分支里直接 `continue`，唤醒信号被吃掉却不扫描。
   修法：该分支收到唤醒时照样跑一轮。

> 复盘教训：这个页面的 JS 一旦有语法错误就整段不执行且**控制台之外无任何提示**。
> 改动 `HTML_TEMPLATE` 后，建议用 node 做一次语法自检：
> `python -c "import serve;open('t.js','w',encoding='utf-8').write(__import__('re').findall(r'<script>(.*?)</script>',serve.render_page(),16)[0])" && node --check t.js`
