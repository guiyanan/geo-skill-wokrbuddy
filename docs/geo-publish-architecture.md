# GEO 发布引擎 · 架构技术方案 v0.1

> 状态：**待拾一米评审**。本文档定稿前不写实现代码。
> 目标平台（一期）：CSDN。二期：掘金。
> 上游：`geo-master`（策略/成稿）、`geo-monitor`（监测）。本引擎只补「执行 + 回执」缺口。

---

## 0. 需要你拍板的 5 个决策

| # | 决策 | 我的建议 | 理由 |
|---|---|---|---|
| D1 | 执行引擎 | **Playwright + 系统 Chrome**，不用 agent-browser | 见 §3.1，agent-browser 缺文件上传和 JS 注入能力 |
| D2 | 实现语言 | **Python**（与 geo-master/geo-monitor 一致） | 避免三套运行时并存 |
| D3 | 登录态 | 持久化 user-data-dir，扫码一次长期复用 | 见 §3.3 |
| D4 | 发布闸门 | `draft`（默认）/ `publish`（全自动）/ `dry-run` | 你说要白名单全自动，但第一个 adapter 建议先用 draft 验证 |
| D5 | 封面图 | AI 生成**无文字**主视觉 + 代码叠标题 | 见 §3.9，模型拼中文易错字 |

---

## 1. 系统边界

**做：**
- 从「资料」生成符合 GEO 结构的稿件（成稿层）
- 按平台规则做格式变体
- 浏览器自动投递 + 拿回执（URL / 截图 / 时间）
- 发布记录落盘，回填监测基线

**不做：**
- 不做选题决策（那是 geo-master 的活）
- 不做数据采集（那是 geo-monitor 的活）
- **不做验证码破解、不做账号矩阵、不做洗稿伪原创**（风控与合规红线，硬拒）
- 不管内容分发后的效果归因（那是下个监测周期的事）

---

## 2. 分层架构

```
┌─────────────────────────────────────────────────────┐
│ CLI 层      geopublish write | login | publish | log │
├─────────────────────────────────────────────────────┤
│ 编排层      job runner：队列 → 闸门 → 执行 → 回执    │
├──────────────┬──────────────────┬───────────────────┤
│ 成稿层        │ 适配层            │ 执行层             │
│ write/       │ adapters/csdn     │ session  lock      │
│ facts→brief  │ adapters/juejin   │ inject   verify    │
│ →draft→check │ (统一接口契约)     │ guard    receipt   │
├──────────────┴──────────────────┴───────────────────┤
│ 引擎层      Playwright (channel=chrome)              │
├─────────────────────────────────────────────────────┤
│ 存储层      data/drafts/  data/publish-log.csv       │
│             ~/.geo-publish/profiles/  locks/         │
└─────────────────────────────────────────────────────┘
```

**分层原则**：适配层之上不认识任何平台的 DOM；引擎层之下不认识任何「文章」概念。中间用 `Draft` 和 `Receipt` 两个数据结构通信。

---

## 3. 关键组件设计

### 3.1 引擎选型（为什么不用 agent-browser）

| 能力 | agent-browser CLI | Playwright | 对发布是否必需 |
|---|---|---|---|
| 上传本地文件（封面图） | ✗ 命令集无此能力 | `set_input_files()` | **必需** |
| 执行任意 JS（注入编辑器） | ✗ | `page.evaluate()` | **必需** |
| 登录态持久化 | daemon 内存，close 即丢 | `launch_persistent_context()` | **必需** |
| 精确等待 | 仅 `wait --load` | `locator.wait_for()` + 自动重试 | 必需 |
| 元素探查 | `snapshot -i` 很快 | 需写脚本 | 探索期有用 |
| 调试留证 | screenshot | trace / video / 网络日志 | 排障必需 |

**结论**：agent-browser 定位为**探路工具**——第一次摸 CSDN 页面结构时，用它 `snapshot -i` 快速看元素 ID，把选择器抄进 adapter。生产执行走 Playwright。

> 说明：这不违背你「用 browser use 发文章」的诉求。agent-browser 本身也是 Playwright 驱动的，两者是同一类东西的不同封装层。选 Playwright 是因为发布这个场景对精细控制的要求超出了通用 CLI 的表达能力。

### 3.2 适配器契约（adapter 到底是啥）

任何平台的发布流程都逃不出这 7 个动作，所以接口就定这 7 个：

```python
class PlatformAdapter(Protocol):
    name: str

    def check_auth(self, page) -> AuthState:
        """返回 logged_in / expired / unknown。只读判断，不做登录动作。"""

    def open_editor(self, page, draft: Draft) -> None:
        """导航到新建文章页，等编辑器就绪。"""

    def fill_content(self, page, draft: Draft) -> None:
        """注入正文。内部自带三级降级，见 §3.4。"""

    def fill_meta(self, page, draft: Draft) -> None:
        """标题、分类、标签、摘要、原创声明、AI 标注。"""

    def upload_cover(self, page, path: Path) -> None:
        """上传封面，等缩略图出现。"""

    def submit(self, page) -> SubmitResult:
        """点发布/存草稿。返回 submitted/unknown。"""

    def verify(self, page) -> VerifyResult:
        """回读确认：拿到真实 URL 和文章 id。没验证过的不算成功。"""
```

**新增一个平台 = 实现这 7 个方法 + 两份配置**，主程序一行不改。

每个 adapter 目录：

```
adapters/csdn/
├── meta.json       # 域名、编辑器 URL、封面规格、字数/标签限制
├── selectors.json  # 所有选择器，改版只改这里
├── flow.py         # 实现上述 7 方法
└── rules.md        # 平台规则与红线（人看 + 出稿时参考）
```

**关键纪律**：选择器**全部**放 `selectors.json`，`flow.py` 里不准出现裸选择器字符串。这样平台改版时改一个 JSON 就能修，不用读代码。

### 3.3 登录态管理

```python
ctx = pw.chromium.launch_persistent_context(
    user_data_dir = "~/.geo-publish/profiles/csdn",
    channel      = "chrome",     # 复用系统 Chrome 155，不下载 Chromium
    headless     = False,        # 必须 headed：扫码要人看，且 headless 更易被判定
    viewport     = {"width": 1440, "height": 900},
    locale       = "zh-CN",
)
```

**为什么不用默认 profile**：独立 `user_data_dir` 可以和日常 Chrome 并存，不抢 profile 锁，也不污染你平时的浏览记录。

- 目录权限 `700`，不进 git，不进任何同步盘。
- 探活：访问创作者后台，若 302 到 `passport.csdn.net` → `AUTH_EXPIRED`，**立即停止本次任务，不做任何重试**（反复撞登录页是风控最敏感的信号）。
- 失效后 CLI 输出：`需重新授权：python -m geopublish login csdn`，然后退出码 2。

### 3.4 内容注入（这个平台最容易翻车的地方）

CSDN Markdown 编辑器是 CodeMirror。直接 `type()` 3000 字会逐字符触发 input 事件，慢且可能丢字。方案是**三级降级**：

**L1 — 模拟粘贴事件**（首选，最快）
```js
const dt = new DataTransfer();
dt.setData('text/plain', md);
const el = document.querySelector('.cm-content');
el.focus();
el.dispatchEvent(new ClipboardEvent('paste', {
  clipboardData: dt, bubbles: true, cancelable: true
}));
```
CodeMirror 原生实现了 paste handler，这是最"合法"的注入方式。

**L2 — 直接操作 CodeMirror 实例**
```js
const el = document.querySelector('.cm-editor');
const view = el && el.cmView && el.cmView.view;   // CM6 内部挂载点
if (view) view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: md}});
```
不同版本的挂载路径不同，需要实测确认。

**L3 — 分段 type 兜底**
按 500 字一段，`locator.press_sequentially()`，段间 300ms。慢（约 25–40s）但几乎不会失败。

**验证方式**：注入后立即回读编辑器内容长度，与源文对比，差异 > 2% 则降级。**不验证就提交，等于盲发。**

> 这一步必须在真实账号上做一次「探针测试」，确认 L1 在当前版本可用。测试脚本单独跑，不混在发布流程里。

### 3.5 幂等与并发锁

**幂等键**：`sha256(platform | account | slug | content_sha)`

**状态机**（`publish-log.csv` 每行一个状态）：

```
pending → submitting → published
                     → failed
                     → needs_human
```

- **先落盘 `submitting` 再点发布**。若进程在提交瞬间崩溃，下次运行看到 `submitting` 且无 URL → 直接进 `needs_human`，**绝不自动重发**。这是防重复发文的关键。
- 同一幂等键已 `published` → 跳过，日志记 `SKIPPED_DUPLICATE`。
- **文件锁**：`~/.geo-publish/locks/csdn.lock` 用 `fcntl.flock`，同一平台同时只允许一个进程。防手滑双击跑出两个浏览器。

### 3.6 失败分类与处理矩阵

| 现象 | 判定 | 动作 |
|---|---|---|
| 跳转 passport 登录页 | 登录失效 | 停任务，退出码 2，不重试 |
| 弹出滑块/点选验证 | 触发风控 | 停，截图存证，置 `needs_human`，**不做任何绕过尝试** |
| 提示敏感词/违规 | 内容问题 | 停，提取提示原文，回写 `selfcheck.json`，交人工改稿 |
| 提交后 URL 未变、无成功提示 | 未知 | 等 5s → 去文章列表页按标题查 → 查到=成功，查不到=重试 1 次 |
| 网络超时 | 瞬时 | 指数退避 2/4/8s，最多 3 次 |
| 编辑器未加载 | 前端抖动 | 刷新重试 1 次，仍失败则 `failed` |

**全局上限**：单次运行对同一篇文章的重试次数 ≤ 2。超过即停，不管原因。

### 3.7 回执与回填（这里有个必须修的坑）

发布成功必须落：`url` / `article_id` / `published_at` / `screenshot.png` / `content_sha` / `indexed_at`（待检，初始 null）。

**发现的坑**：CSDN 的域名是 `blog.csdn.net`，**不是 JOTO 自有域名**。`geo-monitor` 现在只有 `brand.domains` 和 `owned_channels` 两个白名单，而 `owned_channels` 只写了 `mp.weixin.qq.com`（也是共享域名）。

如果只按域名匹配，**CSDN 上发一百篇，监测里的 SOC 可能一点不动**——你会误判成「发布没用」。

所以 `geo-monitor/config/joto.json` 的 `owned_channels` 需要升级为二元组：

```json
"owned_channels": [
  {"domain": "mp.weixin.qq.com", "path_hint": null},
  {"domain": "blog.csdn.net",    "path_hint": "/joto_xxx"}
]
```

判定逻辑：域名命中 **且** URL 路径包含 `path_hint` 才算自有渠道。这条改动是本方案对监测侧的唯一侵入，必须一起改，否则闭环是假的。

### 3.8 成稿层（五步，含硬闸门）

| 步 | 输入 | 输出 | 硬规则 |
|---|---|---|---|
| 1 资料解析 | 你给的资料 | `facts.json` | 每条事实带 `source` + `tier(A/B/C/D)`；无出处的**不进稿** |
| 2 锚定问句 | geo-monitor 的 C/D/S 类问句 | `brief.json` | **一稿只锚一个问句**，贪多必失 |
| 3 骨架 | brief + facts | 大纲 | 结论前置；H2 问句化；每节 200–400 字可独立切片 |
| 4 变体 | 大纲 + facts | `csdn.md` / `juejin.md` | 同源不同稿，非复制粘贴（避原创冲突） |
| 5 自检 | 稿件 | `selfcheck.json` | 任一不过 → 不进入发布队列 |

**自检拦截项**（全自动意味着这是最后一道闸）：
- 绝对化用语（最好/第一/唯一/领先/保证/100%）→ 广告法风险
- 营销句式（欢迎咨询/联系我们/立即购买）→ 掘金尤其致命
- 数字类 claim 无 `source` → 直接判不合格
- 字数 / 标题长度 / 标签数量越界
- AI 参与度标记（平台勾选项按真实情况填）

### 3.9 封面图方案

**可以自己生成**，但建议分两层：

1. **主视觉用 AI 生成，且不带文字** —— 模型拼中文标题容易出现错字/怪字，生成出来不能用。
2. **标题文字用代码叠加**（HTML + 无头浏览器截图）：字体、位置、字号全部可控，改标题不用重新生图。

尺寸以 CSDN 后台上传框的实际提示为准（常见 1140×640 一类），adapter 里读 DOM 拿，不硬编码猜。

备用方案：从你的资料里抽图 + 统一裁切加边，比 AI 出图更"像官方物料"。

---

## 4. 数据模型

**`meta.json`**（每篇稿件）
```json
{
  "slug": "deepparser-table-extraction",
  "anchor_query_id": "S01",
  "target_reader": "企业 IT / 数据治理负责人",
  "platforms": ["csdn"],
  "title": "...",
  "summary": "...",
  "tags": ["文档解析", "PDF", "OCR"],
  "category": "人工智能",
  "cover": "assets/cover.png",
  "ai_involvement": "assisted",
  "content_sha": "..."
}
```

**`publish-log.csv`**
```
ts,platform,account,slug,idem_key,status,url,article_id,screenshot,indexed_at,note
```

---

## 5. 一次全自动发布的时序

```
1  CLI 读队列 → 取下一篇 pending
2  计算 idem_key → 查 log → 已发则跳过
3  取平台锁（拿不到就退出）
4  启动 Chrome（持久 profile）
5  check_auth → expired 则停
6  open_editor → 等编辑器 ready
7  fill_content → 注入 → 回读校验长度
8  upload_cover → 等缩略图
9  fill_meta → 标题/分类/标签/摘要/声明
10 截图存证（发布前）
11 写 log=submitting → 落盘
12 submit → 等跳转
13 verify → 拿 url + article_id
14 写 log=published → 截图存证（发布后）
15 释放锁 → 关闭浏览器
```

第 10 步的「发布前截图」很重要：万一平台事后说违规，你有当时页面状态的证据。

---

## 6. 风险登记表

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R1 | CSDN 编辑器改版，adapter 失效 | 高 | 中 | 选择器全外置；每次失败自动截图，改版可快速定位 |
| R2 | 扫码登录态过期 | 中 | 中 | 探活即停，不重试；提示重新授权 |
| R3 | 新号触发验证码 | **高** | 高 | 见下方说明 |
| R4 | 内容审核不通过 | 中 | 中 | 自检闸门前置；提示原文回写 |
| R5 | 全自动误发/重发 | 低 | **高** | 幂等锁 + submitting 状态机 + 文件锁 |
| R6 | 新号被判营销号限流 | 高 | 高 | 节奏控制：前两周每天 ≤1 篇 |

**R3 需要提前跟你说明**：新号 + 自动化操作，触发滑块验证的概率不低。触发后本引擎的**唯一动作是停下来交给人**，不尝试任何绕过。这意味着全自动不是"永远无人值守"，而是"正常情况下无人值守，遇风控立刻转人工"。如果你要的是"完全不需要人盯"，那这个目标在平台侧风控下不成立，我不做假承诺。

---

## 7. 分期计划

| 期 | 范围 | 验收标准 |
|---|---|---|
| **P0** | CSDN adapter + 探针测试（登录、注入、上传） | 用一篇真实稿子跑到草稿箱，截图留证 |
| **P1** | 全自动发布 + 回执 + 幂等 | 同一篇跑两次，第二次被正确跳过 |
| **P2** | 成稿层（资料 → 稿件 → 自检） | 给一份资料，产出通过自检的稿件 |
| **P3** | 掘金 adapter + 双平台变体 | 同源两稿都能发，URL 互不重复 |
| **P4** | 队列 / 定时 / 监测回填 | 与 geo-monitor 打通，SOC 能反映自有渠道 |

**为什么 P0 先只到草稿箱**：你说要全自动，我尊重。但 adapter 的正确性必须先用最便宜的方式验证——草稿箱跑通了，改成真发布只是一行配置，反过来则可能把错误的稿子发出去。

---

## 8. 待你补充的信息

1. **CSDN 用户名 / 主页路径**（形如 `blog.csdn.net/xxx`）——回填监测白名单要用
2. **资料格式**（PPT / Word / 官网链接）
3. **封面偏好**：AI 生成主视觉 + 代码叠字，还是从现有物料抽图？
4. 第 0 节 D1–D5 五个决策是否认可

---

*本文档定稿后，实现按 P0→P4 推进。任何一步不达标就停在那一步修，不往下跳。*
