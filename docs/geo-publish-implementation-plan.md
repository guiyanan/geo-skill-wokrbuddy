# GEO 发布引擎 · 实施方案（Implementation Plan）v1.1

| | |
|---|---|
| 依据 | `docs/geo-publish-PRD.md` v1.3（R1–R18、R18.1、N1–N6） |
| 文档性质 | 怎么实现 + 怎么测 + 怎么修 bug |
| 组织方式 | 21 张工单（T-01 ~ T-21），每张可独立验收、独立回滚 |
| 核心要求 | 每个步骤带单元测试；**bug 必须先稳定复现，再改代码** |
| 一致性 | 已核对，见 `geo-publish-alignment-review.md`（v1.1 补齐 G1–G8） |

---

## 0. 为什么"稳定复现"要先于写功能

浏览器自动化有三个天然的不确定性来源，不先解决，任何测试都会"有时过有时不过"，等于没测：

| 不确定性 | 表现 | 本方案的对策 |
|---|---|---|
| 时序 | 元素有时在有时不在 | **禁止 sleep**，全部改条件等待（`wait_for` 带超时） |
| 网络 | 页面依赖 CDN，慢/挂 | **网络拦截**：测试期所有外部请求走本地 fixture，一条不通就挂 |
| 环境 | 平台改版、A/B 实验 | **DOM 快照存档**，测试跑快照不跑线上 |

再加三条确定性约束，写进代码规范：

1. **时间可注入**：所有 `now()` 走 `core.clock`，测试用固定时钟
2. **随机可固定**：涉及随机的（发布间隔抖动）用 `random.Random(seed)`，seed 来自配置
3. **失败必留现场**：任何异常自动归档「现场包」（见 §2）

---

## 1. 测试分层（四层，越往下越慢越少）

| 层 | 标记 | 跑什么 | 耗时 | 依赖 |
|---|---|---|---|---|
| L1 单元 | `-m unit` | 纯函数：状态机、幂等键、自检、选题、日报渲染 | 秒级 | 无 |
| L2 契约 | `-m contract` | adapter 对 **DOM 快照**断言选择器可命中 | 秒级 | 无网络 |
| L3 集成 | `-m integration` | 真浏览器 + **本地假站点**（用快照起的静态页） | 分钟级 | Chrome |
| L4 端到端 | `-m e2e` | 真 CSDN，真账号 | 数分钟 | 登录态 |

```bash
pytest -m unit          # 每次改代码都跑，必须 < 5s
pytest -m contract      # 每次改 adapter 都跑
pytest -m integration   # 提交前跑
pytest -m e2e           # 只在 P0/P1 验收时手动跑
```

**分配原则**：一个功能，逻辑写 L1，平台交互写 L2，串起来写 L3。L4 只用于里程碑验收，不进 CI。

---

## 2. 「现场包」机制：bug 怎么稳定复现

这是整个方案里最重要的一条工程约定。

### 2.1 失败时自动归档什么

任何一次执行失败，`core/incident.py` 自动落一个目录：

```
data/incidents/<ts>_<ticket>_<stage>/
├── bundle.json        # 失败阶段、adapter 版本、选择器版本、PRD 需求号
├── page.html          # 页面 DOM 快照（失败瞬间）
├── screenshot.png     # 失败截图
├── network.har        # 网络请求记录
├── console.log        # 浏览器控制台
├── input.json         # 本次输入（稿件元信息，脱敏后）
└── repro.sh           # 一键复现脚本
```

### 2.2 复现三步

```bash
# 1. 只跑这一个现场，得到确定性失败
pytest --replay data/incidents/20261021_100312_T-07_inject/

# 2. 确认稳定复现（连跑 5 次，5 次全红才算复现成功）
pytest --replay data/incidents/xxx/ --repeat 5

# 3. 修完再跑，转绿后固化为永久回归用例
pytest --promote data/incidents/xxx/ -> tests/regression/test_T07_xxx.py
```

**规则：复现不出来就不许改代码。** 因为改了你也不知道是修好了还是碰巧。这条是硬纪律，我会在每次修 bug 时严格执行，也会在报告里注明"已复现/未复现"。

### 2.3 回归集只增不减

`tests/regression/` 里的用例来自真实事故，**永不删除**。可以标记 skip（附原因），但不能删。

---

## 3. 仓库结构

```
geo-publish/
├── SKILL.md
├── pyproject.toml
├── geopublish/
│   ├── cli.py                 # 入口
│   ├── core/
│   │   ├── clock.py           # 可注入时间
│   │   ├── config.py          # 配置加载与校验
│   │   ├── models.py          # Draft / Meta / Receipt / LogEntry
│   │   ├── state.py           # 幂等状态机
│   │   ├── lock.py            # 文件锁
│   │   ├── incident.py        # 现场包归档
│   │   ├── session.py         # 登录态
│   │   ├── guard.py           # 合规自检
│   │   └── notify.py          # 邮件推送
│   ├── write/                 # 产品库 → 稿件
│   ├── select/                # 选题（调 geo-master）
│   ├── adapters/
│   │   ├── base.py            # 七方法契约
│   │   └── csdn/
│   │       ├── meta.json
│   │       ├── selectors.json
│   │       └── flow.py
│   └── report/                # 日报
├── tests/
│   ├── unit/  contract/  integration/  regression/
├── fixtures/
│   ├── csdn/                  # DOM 快照（登录页/编辑器/成功页/验证码页）
│   └── fakesite/              # 本地假站点
└── data/  products/ drafts/ runs/ incidents/ reports/
```

---

## 4. 工单总览

> 执行顺序看「依赖」列，**不是编号顺序**。每张工单完成后在下方 §4.1 勾选。

| # | 工单 | 需求 | 依赖 | 阶段 |
|---|---|---|---|---|
| T-01 | 骨架 + 测试框架 + 配置 | — | — | P0 |
| T-02 | 数据模型与幂等状态机 | R9 | T-01 | P0 |
| T-03 | 自检引擎（纯逻辑） | R4 | T-01 | P0 |
| T-04 | 现场包与快照存档 | — | T-01 | P0 |
| T-05 | adapter 契约 + fixture 回放 | R7 | T-04 | P0 |
| T-06 | 登录态管理 | R6 | T-02 | P0 |
| T-07 | 正文注入（三级降级） | R7 | T-05 | P0 |
| T-08 | 元信息与封面 | R7 | T-07 | P0 |
| T-09 | 草稿投递 + verify | R7 | T-08 | **P0 里程碑** |
| T-10 | 发布与回执 | R8 R10 | T-09 | P1 |
| T-11 | 幂等与并发锁（落地） | R9 | T-02 | P1 |
| T-12 | 失败分类与红线 | R12 R13 | T-10 | P1 |
| T-13 | 产品库入库 | R15 | T-03 | P2 |
| T-14 | 成稿五步 | R1 R2 R3 | T-13 | P2 |
| T-15 | 封面生成 | R5 | T-01 | P2 |
| T-16 | 自检闸门串联 | R4 | T-14 | P2 |
| T-17 | 选题串联 geo-master | R17 | T-14 | P3 |
| T-18 | 每日调度 | R16 | T-10 T-17 | P3 |
| T-19 | 日报与邮件推送 | R18 | T-18 | P3 |
| T-20 | 节奏控制 | R14 | T-18 | P3 |
| T-21 | 监测回填 | R11 | T-10 | P4 |
| T-22 | 干预通道（hold / edit / pause） | R18.1 | T-18 | P3 |

### 4.1 工单勾选清单

**P0 · 打通**
```
- [ ] T-01  骨架 + 测试框架 + 配置        ← 仍有遗留：cli.py / config.py
- [x] T-02  数据模型与幂等状态机
- [ ] T-03  自检引擎（纯逻辑）
- [ ] T-04  现场包与快照存档
- [ ] T-05  adapter 契约 + fixture 回放
- [ ] T-06  登录态管理
- [ ] T-07  正文注入（三级降级）
- [ ] T-08  元信息与封面
- [ ] T-09  草稿投递 + verify          ← P0 里程碑
```

**P1 · 全自动**
```
- [ ] T-10  发布与回执
- [ ] T-11  幂等与并发锁（落地）
- [ ] T-12  失败分类与红线
```

**P2 · 能成稿**
```
- [ ] T-13  产品库入库
- [ ] T-14  成稿五步
- [ ] T-15  封面生成
- [ ] T-16  自检闸门串联
```

**P3 · 无人值守**
```
- [ ] T-17  选题串联 geo-master
- [ ] T-18  每日调度（两阶段）
- [ ] T-19  日报与邮件推送
- [ ] T-20  节奏控制
- [ ] T-22  干预通道
```

**P4 · 闭环**
```
- [ ] T-21  监测回填
```

每张工单完成标准：交付物齐全 + 对应用例全绿 + 现场包可回放。

---

## 5. 工单明细

> 每张工单：目标 → 交付物 → 单元测试（编号 + 断言）→ 完成标准 → 已知失败模式

### T-01 骨架 + 测试框架 + 配置

**交付**：`pyproject.toml`、`geopublish/cli.py`（仅 `--version`）、`core/config.py`、`core/clock.py`、`tests/` 四层目录、`pytest.ini` 的 marker 定义

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-01-01 | 缺必填配置项时抛 `ConfigError`，信息含缺失字段名 |
| UT-01-02 | 阈值越界（如 `daily_limit = -1`）被拒绝 |
| UT-01-03 | 固定时钟下 `now()` 返回注入值，不取系统时间 |
| UT-01-04 | `pytest -m unit` 在无浏览器环境下可运行 |

**DoD**：`pytest -m unit` 全绿且 < 2s；`geopublish --version` 可执行

---

### T-02 数据模型与幂等状态机

**交付**：`core/models.py`（Draft/Meta/Receipt/LogEntry）、`core/state.py`

**状态机**（PRD R9 的落地核心）
```
pending → submitting → published
                     → failed
                     → needs_human
```
合法转换白名单固定；`published` 是终态，不可逆。

**单元测试**
| 编号 | Given | Then |
|---|---|---|
| UT-02-01 | 同 platform+account+slug+content_sha | 幂等键相同 |
| UT-02-02 | 内容改一个字 | 幂等键不同（允许发布） |
| UT-02-03 | 状态 `published` 试图转 `submitting` | 抛 `IllegalTransition` |
| UT-02-04 | 状态 `submitting` 且 `url` 为空（模拟崩溃） | 恢复逻辑判定为 `needs_human`，**不是 pending** |
| UT-02-05 | 状态 `submitting` 且 `url` 非空 | 恢复逻辑判定为 `published` |
| UT-02-06 | 写日志时字段缺失 | 抛异常，不写半行 |

**DoD**：状态机 100% 分支覆盖；UT-02-04 是防重复发文的关键，必须绿

**已知失败模式**：忘记在提交前落盘 `submitting` → 崩溃后重发一篇。UT-02-04 就是钉这个的。

**交付记录（2026-10-20 完成）**
| 项 | 结果 |
|---|---|
| 文件 | `core/state.py`、`core/models.py`、`core/clock.py`（补 T-01 遗留）、`tests/unit/test_state_machine.py`、`test_models.py`、`test_clock.py` |
| 用例 | UT-02-01~06 全绿；unit+contract 合计 91 条，全套 1.6s |
| 覆盖率 | `state.py` 89 语句 / 16 分支，`models.py` 141 语句 / 34 分支 —— 语句+分支 **100%** |
| 状态机白名单 | pending→{submitting,failed,needs_human}；submitting→{published,failed,needs_human}；failed→{submitting,pending,needs_human}；needs_human→{pending}；published→∅；同态视为幂等空操作 |
| 幂等键 | `sha256(platform\|account\|slug\|content_sha)`，字段先 strip+lower，分隔符用 `\x1f` 防拼接歧义 |
| 日志 | 追加一行 = 一次 write(整行)+fsync；字段缺失/多余/状态非法 → `IncompleteLogEntry`，文件零字节变化 |
| 顺带修 | 红线门禁 `solve_` → `\bsolve_`（裸子串误伤 `resolve_*`，见 `tests/unit/test_precommit_gate.py`） |

**T-01 遗留（未勾选，需补课）**：`geopublish/cli.py`（`--version`）与 `core/config.py` 当时未落地，
UT-01-01/02 无从执行。T-02 只补了 clock，T-01 的 DoD（`geopublish --version` 可执行）目前仍不满足。

**门禁现状（需决策）**：`.pre-commit-config.yaml` 写了但本机没装 `pre-commit`，CI 也只跑 pytest、
不跑两个静态门禁 —— 也就是说「禁裸选择器」和「禁验证码代码」这两条红线**目前处于无人执行状态**。
建议把两个脚本挂进 CI 的 unit job（几十毫秒），别只靠本机钩子。

---

### T-03 自检引擎（纯逻辑，无浏览器）

**交付**：`core/guard.py` + `references/banned-words.json`

**检查项**（PRD R4 的五条）
1. 绝对化用语 = 0
2. 营销句式 = 0
3. 数字类主张 source 覆盖率 = 100%
4. 标题/标签长度符合平台限制
5. AI 参与度字段非空

**单元测试**
| 编号 | Given | Then |
|---|---|---|
| UT-03-01 | 含"最好的文档解析工具" | `passed=False`，命中项含"最好" |
| UT-03-02 | 含"欢迎咨询" | `passed=False` |
| UT-03-03 | "支持 12 种格式"但无 source | `passed=False`，标记 `unsourced_number` |
| UT-03-04 | 同一句同时命中 3 个词 | 全部列出，不只报第一个 |
| UT-03-05 | 全文干净 | `passed=True`，`hits=[]` |
| UT-03-06 | 空稿件 | `passed=False`（不允许空发） |
| UT-03-07 | 标题超平台上限 | `passed=False` **且** `status=rejected`（补 G5） |
| UT-03-08 | AI 参与度字段为空 | `passed=False` **且** `status=rejected`（补 G5） |

**DoD**：任何一条不满足即拒；**不存在 `skip` 参数**（PRD §9.5 红线）；
UT-03-07/08 确保 R4 五项**全部**与 `rejected` 联动，而不只是前三项

---

### T-04 现场包与快照存档

**交付**：`core/incident.py`、`geopublish snapshot <url> <name>`

**能力**
- `snapshot` 命令：打开 URL → 存 `page.html` + `screenshot.png` + `meta.json`
- 失败钩子：异常时自动打包（§2.1 结构）

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-04-01 | 打包后目录含七件套，缺一件则打包失败并报错 |
| UT-04-02 | `input.json` 已脱敏：不含 cookie / token / 手机号 |
| UT-04-03 | `repro.sh` 可直接执行且退出码非 0（因为必然复现失败） |
| UT-04-04 | 目录名含 ticket 号与失败阶段，便于检索 |

**DoD**：手动制造一次失败，产出包能被 `--replay` 加载

---

### T-05 adapter 契约 + fixture 回放

**交付**：`adapters/base.py`（七方法 Protocol）、`adapters/csdn/selectors.json`、`tests/contract/`

**契约**（PRD 技术方案 §3.2）
`check_auth / open_editor / fill_content / fill_meta / upload_cover / submit / verify`

**单元测试（契约层）**
| 编号 | Given（fixture） | Then |
|---|---|---|
| CT-05-01 | `csdn/editor.html` | `title` 选择器命中 1 个元素 |
| CT-05-02 | `csdn/editor.html` | `content` 选择器命中 CodeMirror 容器 |
| CT-05-03 | `csdn/login.html` | `check_auth` 返回 `expired` |
| CT-05-04 | `csdn/editor.html`（已登录） | `check_auth` 返回 `logged_in` |
| CT-05-05 | `csdn/captcha.html` | `check_auth` 返回 `unknown` 且 `has_captcha=True` |
| CT-05-06 | 删除 `selectors.json` 中任一键 | 加载即报错（fail fast，不留默认值） |

**DoD**：所有选择器只出现在 `selectors.json`；`flow.py` 中 grep 不到裸 CSS 选择器

---

### T-06 登录态管理

**交付**：`core/session.py`、`geopublish login csdn`
**注意**：`channel` 必须可配置（本地 `chrome` / CI 默认），见 §8.5

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-06-01 | `check_auth` 遇 `expired` → 抛 `AuthRequired`，**调用次数为 1**（不重试） |
| UT-06-02 | `AuthRequired` 时 CLI 退出码 = 2 |
| UT-06-03 | 错误信息含可执行的修复命令 |
| UT-06-04 | profile 目录权限为 700 |
| UT-06-05 | profile 路径不在 git 追踪范围内（`.gitignore` 校验） |

**DoD**：扫码一次后 7 天内可连续使用（L4 手工验证）

**已知失败模式**：把重试写在 auth 里 → 反复撞登录页触发风控。UT-06-01 钉死"只调一次"。

---

### T-07 正文注入（三级降级）

**交付**：`adapters/csdn/flow.py::fill_content`

**降级链**（技术方案 §3.4）
`L1 模拟 paste` → `L2 CodeMirror dispatch` → `L3 分段 type`

**单元测试**
| 编号 | Given | Then |
|---|---|---|
| UT-07-01 | L1 成功后回读长度 | 与源文差异 < 2% |
| UT-07-02 | L1 注入后长度偏差 50% | 自动降级到 L2，不直接失败 |
| UT-07-03 | L1、L2 都失败 | 降级到 L3，日志记录两次降级 |
| UT-07-04 | 三级全失败 | 抛 `InjectFailed`，归档现场包，**不提交** |
| UT-07-05 | 5000 字长文 | L1 不超时（< 10s） |
| UT-07-06 | 内容含 Markdown 表格与代码块 | 回读内容结构完整（表格行数、代码块数一致） |

**DoD**：真实 CSDN 上跑通一篇 3000 字（L4 手工），且草稿正文与源文 diff < 2%

---

### T-08 元信息与封面

**交付**：`fill_meta`、`upload_cover`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-08-01 | 标题超长时截断并告警，不静默丢弃 |
| UT-08-02 | 标签数超上限时按优先级取前 N 个 |
| UT-08-03 | 分类不存在时抛错并列出可选项 |
| UT-08-04 | 封面上传后等待缩略图出现，超时 10s 抛错 |
| UT-08-05 | 封面尺寸不符平台要求时**在本地就拒绝**，不上传后才失败 |

**DoD**：草稿里标题/分类/标签/封面全部正确

---

### T-09 草稿投递 + verify（**P0 里程碑**）

**交付**：`submit` + `verify` + `geopublish publish --mode draft`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-09-01 | `verify` 拿不到 article_id → 判 `failed`，不算成功 |
| UT-09-02 | 提交后 URL 未变但列表页能查到同标题 → 判 `published` |
| UT-09-03 | 提交前必有一次 `content_length_check`（调用序列断言） |
| UT-09-04 | `--mode draft` 绝不点"发布"按钮（断言点击元素集合不含 publish） |

**DoD（P0 验收）**：一条命令，CSDN 草稿箱出现正确文章，正文 diff < 2%，截图留证

---

### T-10 发布与回执

**交付**：`submit(mode=publish)`、`core/receipt.py`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-10-01 | 回执含 url/article_id/published_at/screenshot 四字段 |
| UT-10-02 | `publish-log.csv` 追加一行且列顺序固定 |
| UT-10-03 | 回执写入失败（磁盘满模拟）→ 不掩盖发布成功状态，单记 `receipt_failed` |
| UT-10-04 | 时间字段用注入时钟，非系统时间 |
| IT-10-01 | 真浏览器跑完一篇 | 端到端耗时 < 5 分钟，超时即失败（补 G8 / N1） |

**DoD（P1 验收）**：一条命令 5 分钟内出可访问 URL；无痕窗口可打开

---

### T-11 幂等与并发锁（落地）

**交付**：`core/lock.py` + 发布流程接入状态机

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-11-01 | 同稿连跑两次，第二次输出 `SKIPPED_DUPLICATE`，浏览器**未启动**（断言 session 未创建） |
| UT-11-02 | 两个进程并发：第二个拿锁失败，退出码 3，不启动浏览器 |
| UT-11-03 | `submitting` 态崩溃后重跑 → `needs_human`，不发 |
| UT-11-04 | 锁文件残留但进程已死 → 可检测并接管（含 stale lock 超时） |
| UT-11-05 | 正常结束释放锁，二次运行可正常进入 |

**DoD**：PRD R9 三条全过

---

### T-12 失败分类与红线

**交付**：`core/errors.py` 错误映射表 + 失败处理矩阵

**单元测试**
| 编号 | Given | Then |
|---|---|---|
| UT-12-01 | 页面含验证码 fixture | 判定 `CAPTCHA`，状态 `needs_human`，**零次重试** |
| UT-12-02 | 页面跳到登录页 | 判定 `AUTH_EXPIRED`，退出码 2 |
| UT-12-03 | 页面含"含敏感词"提示 | 判定 `CONTENT_REJECTED`，回写提示原文 |
| UT-12-04 | 网络超时 | 重试 3 次后 `failed`，间隔为 2/4/8s（用假时钟断言） |
| UT-12-05 | 同一篇总重试次数 ≥ 3 | 强制停止（全局上限） |
| UT-12-06 | 代码库 grep `captcha\|solve\|bypass` | **无任何实现代码**（元测试，防后人加） |
| UT-12-07 | 注入一次提交后失败 | 草稿箱无残留；若有残留必须标记 `failed_residue` 并写进日报（补 G8 / N5） |

**DoD**：UT-12-06 这条元测试是红线守门人，必须存在且绿

---

### T-13 产品库入库

**交付**：`geopublish ingest <path>`、`data/products/<id>/`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-13-01 | 入库后 `facts.json` 每条含 claim/source/tier |
| UT-13-02 | 二次入库同产品：旧事实保留，新增追加，`source` 不被覆盖 |
| UT-13-03 | 解析出的事实数为 0 → 入库失败并报错（不产生空产品） |
| UT-13-04 | 同一事实重复入库 → 按 claim 去重，不产生两条 |
| UT-13-05 | 喂 ≥15 页的 fixture 资料 | `len(facts) >= 12` 且 `facts` 非空（补 G1，防空列表过检） |
| UT-13-06 | 任一事实 `source` 为空 | 入库失败并报错，不产生半份产品（补 G1） |

**DoD**：一份真实 PPT 入库后，成稿层可直接取用，无需人工整理

**人工抽检（补 G2，不可跳过）**
入库完成后输出 `data/products/<id>/REVIEW.md`，列出待人工确认的三项：
1. 随机抽 5 条事实，对照原始资料确认「未越界」（要求 0 条越界）
2. `tier` 分级是否合理（A/B/C/D）
3. 是否有明显遗漏的核心卖点

人工确认后回填 `reviewed_by` 与 `reviewed_at`；**未回填的产品不进入自动成稿队列**。

---

### T-14 成稿五步

**交付**：`write/pipeline.py`（解析→锚定→骨架→变体→自检）

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-14-01 | `anchor_query_id` 恰好 1 个，且存在于 config |
| UT-14-02 | 锚定 B/P 类品牌词 → 拒绝（需求侧才有增量） |
| UT-14-03 | 产出稿件 H2 ≥ 3 且均为问句（以问号结尾或含疑问词） |
| UT-14-04 | 每节字数落在 200–400 区间 |
| UT-14-05 | 全文含 ≥3 条 FAQ |
| UT-14-06 | 稿件中出现的每个数字都能在 facts 中找到 source |
| UT-14-07 | 首段 ≤ 80 字且直接回答锚定问句 |
| UT-14-08 | 产出稿件正文 | `3000 <= len(body) <= 5000`（补 G3，R3 第 4 条） |
| UT-14-09 | 产出稿件正文 | 品牌/产品实体出现 ≥3 次，且**全文只用一种全称写法**（补 G3，R3 第 6 条） |

**DoD**：给定产品库条目，产出一篇通过 T-03 自检的稿件

> UT-14-09 是全套用例里对 GEO 效果影响最直接的一条：实体密度不足或别名混用，
> AI 无法把内容和品牌绑定，发了等于白发。

---

### T-15 封面生成

**交付**：`write/cover.py`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-15-01 | 输出图片尺寸 = 配置尺寸 |
| UT-15-02 | 改标题后重新渲染，主视觉文件哈希不变（证明没重新生图） |
| UT-15-03 | 文件大小 < 平台上限 |
| UT-15-04 | 字体缺失时 fail fast，不产出空白图 |

**DoD**：生成的封面人工看一次无错别字，标题可改

**人工抽检（补 G2）**
封面生成后输出预览页，人工确认两项：
1. 标题文字无错别字、无怪字
2. 主视觉无水印、无多余文字叠加

确认后写入 `cover.approved=true`；**未确认的封面不进入发布流程**。

---

### T-16 自检闸门串联

**交付**：发布前置检查

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-16-01 | 自检未过 → 发布流程在启动浏览器**之前**中止 |
| UT-16-02 | 自检未过 → 状态 `rejected`，不进队列 |
| UT-16-03 | CLI 不存在 `--skip-guard` 参数（参数表断言） |
| UT-16-04 | `selfcheck.json` 记录每项检查的命中明细 |

**DoD**：PRD §9.5 红线成立

---

### T-17 选题串联 geo-master

**交付**：`select/picker.py`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-17-01 | 输入缺口清单 → 调用 `gap_prioritizer.prioritize()`，输出档位与脚本一致 |
| UT-17-02 | 只取 P0/P1 档，P2/P3 不进候选 |
| UT-17-03 | 同问句×同平台 30 天内已发 → 排除 |
| UT-17-04 | 候选为空 → 返回 `no_topic`，当日跳过并落日报（不硬写） |
| UT-17-05 | 选题能映射到产品库事实；映射不上 → 排除 |
| UT-17-06 | 问句 id 全部来自 `joto.json`，不存在自建问句 |

**DoD**：每日选题可回溯到打分输入

---

### T-18 每日调度

**交付**：`geopublish schedule` + launchd/cron 配置

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-18-01 | 非工作日不触发 |
| UT-18-02 | 多平台错峰：两平台发车时间不重叠 |
| UT-18-03 | 前一日 `needs_human` 未处理 → 当日仍尝试，但日报标注未解决项 |
| UT-18-04 | 进程异常退出 → 日报仍产出（`finally` 保证） |
| UT-18-05 | 调度自动触发且当日已达阈值 | 跳过发布，**并在日报写明原因**（补 G6，防调度绕过节奏检查） |
| IT-18-01 | 注入假时钟快进 5 个工作日 | 产出 5 份日报，每份「应发/实发」字段完整；第 3 天注入平台拒绝，当日日报必须体现（补 G4） |

**DoD（P3 验收）**：连续 5 个工作日无人干预，每天有日报，且 `IT-18-01` 通过

**已知失败模式**：调度进程挂了没人知道 → UT-18-04 强制 finally 出日报；再加"心跳"，超过 26 小时无运行记录则告警

---

### T-19 日报与邮件推送

**交付**：`report/daily.py`、`core/notify.py`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-19-01 | 日报四问齐全（缺任一项则渲染失败） |
| UT-19-02 | 主题行格式 `[JOTO GEO] MM-DD 发布 X/Y · 平台` |
| UT-19-03 | 推送失败 → 发布状态仍 `published`，另记 `notify_failed` |
| UT-19-04 | 零发布日：日报仍生成，明确写"今日无发布"及原因 |
| UT-19-05 | 日报中的 URL 可点击（Markdown 链接格式） |
| UT-19-06 | 存在 `needs_human` 条目 | 触发即时告警（补 G7） |
| UT-19-07 | 连续两日零发布 | 触发即时告警（补 G7） |
| UT-19-08 | `hold <slug>` 后到次日发车 | 该篇不被发布，状态保持 `held` |
| UT-19-09 | 稿件被编辑但未 `recheck` | 发车时被拦截，`status=needs_recheck`（防绕过 R4） |
| UT-19-10 | `pause --until D` | D 之前全跳过，D 当天自动恢复发车 |
| UT-19-11 | 任一干预动作 | 日志含操作人 / 时间 / 目标 slug / 动作类型（补 N4） |

**DoD**：收件箱收到一封能一眼看出结果的邮件

---

### T-20 节奏控制

**交付**：`core/guard.py::check_rate`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-20-01 | 24h 内已发 1 篇（阈值 1）→ 第二次被拒，退出码 4 |
| UT-20-02 | 阈值改为 2 → 允许 |
| UT-20-03 | 计数按平台独立（CSDN 不影响掘金） |
| UT-20-04 | 被拒时日报记录原因，不静默 |

---

### T-21 监测回填

**交付**：更新 `geo-monitor/config/joto.json` 的 `owned_channels`

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-21-01 | 写入结构为 `{domain, path_hint}` 二元组 |
| UT-21-02 | 重复回填同平台 → 不产生重复条目 |
| UT-21-03 | `blog.csdn.net` 无 `path_hint` 时 → cited 判定不成立（防共享域名误判） |
| UT-21-04 | 备份原配置后再改（可回滚） |

---

### T-22 干预通道（对应 R18.1）

**交付**：`geopublish hold|edit|recheck|pause|console`、队列状态文件

**状态流转**
```
queued → held          (hold)
held   → queued        (unhold)
queued → dirty         (edit 修改后)
dirty  → queued        (recheck 自检通过)
dirty  → needs_recheck (recheck 未执行就发车 → 拦截)
```

**单元测试**
| 编号 | 断言 |
|---|---|
| UT-22-01 | `hold` 后状态 = `held`，发车时被跳过 |
| UT-22-02 | `unhold` 后状态回 `queued`，可被发布 |
| UT-22-03 | `edit` 后状态 = `dirty` |
| UT-22-04 | `dirty` 状态下发车 → 拦截，状态转 `needs_recheck` |
| UT-22-05 | `recheck` 自检通过 → 状态回 `queued` |
| UT-22-06 | `recheck` 自检未过 → 状态 `rejected`，不可恢复排队 |
| UT-22-07 | `pause --until D`：D 前全跳过，D 当天自动恢复 |
| UT-22-08 | 干预日志含操作人/时间/slug/动作，缺一即失败 |
| UT-22-09 | `console` 生成的 HTML 含全部 `queued` 条目与各自复选框 |

**DoD**：三种干预均可用；UT-22-04 证明编辑后不自检会被拦下

**DoD**：下期监测中该文 URL 命中时 `cited=true`

---

## 6. 缺陷处理流程（硬纪律）

```
① 发现 → 自动归档现场包（T-04 保证）
② 复现 → pytest --replay ... --repeat 5，5 次全红
③ 未复现 → 停止，回去补现场包能力（不许猜着改）
④ 写红测 → 把现场转成一条 failing test
⑤ 修代码 → 直到该测试变绿 + 全量回归不红
⑥ 固化 → 移入 tests/regression/，永久保留
⑦ 记录 → 工单下追加「事故条目」：症状/根因/修复/回归用例编号
```

**每个 bug 修复的交付物固定为三样**：复现用例、修复代码、回归测试。少一样不算修完。

---

## 7. 里程碑与验收

| 里程碑 | 包含工单 | 验收（对应 PRD §7） |
|---|---|---|
| **P0 打通** | T-01 ~ T-09 | 草稿箱出现正确文章（R6+R7） |
| **P1 全自动** | T-10 ~ T-12 | 一条命令出 URL，复跑不重发（R8+R9+R10） |
| **P2 能成稿** | T-13 ~ T-16 | 产品库出稿且过自检（R1–R5） |
| **P3 无人值守** | T-17 ~ T-20、T-22 | 连续 5 工作日 + 每日日报 + 干预可用（R15–R18） |
| **P4 闭环** | T-21 | 监测认出自有渠道（R11） |

---

## 8. 本方案不做的事

- 不做 UI（命令行足够，加 UI 是负担）
- 不做多账号并发（合规红线）
- 不做自动改稿修复审核问题（那是人的判断）
- 不做「测试覆盖率 100%」这种指标——只追**关键路径分支覆盖**：状态机、幂等、自检、失败分类四处必须全覆盖

---

## 8.5 Git 工作流与提交纪律

### 完整闭环

```
① 需求（PRD Rxx）
   ↓
② 先写测试（必须看到它变红）
   ↓
③ 写实现（直到变绿）
   ↓
④ 本地门禁：pytest -m "unit or contract"
   ↓
⑤ 最小粒度提交 commit
   ↓
⑥ 推送 → GitHub Actions 再跑一遍
   ↓
⑦ Actions 绿 → 这一步才算完成，勾 §4.1 的 checkbox
```

**任何一步不绿，不进入下一步。**

### 提交粒度：什么算"最小可提交"

一个 commit 只做一件事，且满足三条：

1. **可独立回滚**——撤掉它，其余功能不受影响
2. **自带测试**——要么加测试要么让已有测试变绿，不允许"只改实现不碰测试"
3. **能一句话说清**——commit subject 写不出一句人话，说明粒度太大

**标准提交序列**（每个工单照此拆）：

```
test(T-02): 幂等键计算 — 先给 failing test      ← 红，正常
feat(T-02): 实现幂等键计算                        ← 绿
test(T-02): 状态机非法转换 — failing test          ← 红
feat(T-02): 实现状态机与 IllegalTransition        ← 绿
refactor(T-02): 抽出状态转换白名单                ← 必须绿
```

**允许红的只有一种情况**：TDD 第一步的 failing test。其它 commit 一律必须绿。

### 为什么"先红再绿"不能省

写完测试直接是绿的，只有两种可能：

- 这个需求已经实现了（那就不需要这个 commit）
- **这个测试没在测新东西**（那它是废测试）

不先确认红，就没法区分这两种。实践里第二种占大多数——看着有测试，其实什么都没测。

### commit message 规范

```
<type>(<ticket>): <subject>

type: test | feat | fix | refactor | docs | chore
ticket: T-01 ~ T-23
```

例：`test(T-07): add failing test for L1 paste injection`

### 本地门禁

`.pre-commit-config.yaml` 在提交前自动跑 `pytest -m "unit or contract"`，外加两条静态检查：
- `adapters/*.py` 里不准出现裸 CSS 选择器
- `geopublish/*.py` 里不准出现验证码处理代码（红线元测试）

### CI 分层

`.github/workflows/test.yml`：

| job | 跑什么 | 触发 |
|---|---|---|
| `unit` | `-m unit` + `-m contract` | 每次 push / PR |
| `integration` | `-m integration` | unit 通过后 |

**e2e 不进 CI**——它需要真实登录态，CI runner 上跑不了，只在里程碑验收时手动触发。

### 实施中发现的一个必须处理的问题

架构方案定为 `channel="chrome"`（复用系统 Chrome），但 **GitHub Actions 的 runner 上系统 Chrome 版本不可控**，会让 integration 测试飘。

所以 `core/session.py` 必须支持 channel 可配置：

| 环境 | channel | 理由 |
|---|---|---|
| 本地 | `chrome` | 复用系统 Chrome 155，登录态可长期保存 |
| CI | 默认（`None`） | 用 playwright 自带 chromium，版本锁定 |

这一条已写进 T-06 的交付要求。

---

## 9. 需求覆盖索引（双向追溯）

**从需求查工单**：

| PRD 需求 | 工单 | 测试用例 |
|---|---|---|
| R1 资料解析为事实清单 | T-13 | UT-13-01/02/03/04/**05**/**06** |
| R2 锚定单一问句 | T-14 | UT-14-01/02 |
| R3 生成 CSDN 版稿件 | T-14 | UT-14-03/04/05/06/07/**08**/**09** |
| R4 合规自检硬闸门 | T-03 T-16 | UT-03-01~06/**07**/**08**、UT-16-01~04 |
| R5 封面图生成 | T-15 | UT-15-01~04 + **人工抽检** |
| R6 登录态持久化 | T-06 | UT-06-01~05（含 L4 手工验证 7 天） |
| R7 草稿投递 | T-07 T-08 T-09 | UT-07-01~06、UT-08-01~05、UT-09-01~04 |
| R8 全自动发布 | T-10 | UT-10-01~04 + L4 |
| R9 幂等防重发 | T-02 T-11 | UT-02-01~06、UT-11-01~05 |
| R10 回执落盘 | T-10 | UT-10-01~04 |
| R11 监测回填 | T-21 | UT-21-01~04 |
| R12 失败可诊断 | T-04 T-12 | UT-04-01~04、UT-12-01~07 |
| R13 风控即停 | T-12 | UT-12-05/06 |
| R14 节奏控制 | T-20 | UT-20-01~04 |
| R15 产品库 | T-13 | UT-13-01~06 + **人工抽检** |
| R16 每日调度 | T-18 | UT-18-01~05、**IT-18-01** |
| R17 选题串联 | T-17 | UT-17-01~06 |
| R18 日报 | T-19 | UT-19-01~**07** |

**非功能需求**：

| PRD | 工单 | 用例 | 状态 |
|---|---|---|---|
| N1 单篇 < 5 分钟 | T-10 | **IT-10-01** | ✅ 已补（G8） |
| N2 权限 700 / 不入 git | T-06 | UT-06-04/05 | ✅ |
| N3 选择器不硬编码 | T-05 | CT-05-06 | ✅ |
| N4 可追溯 | T-10 T-14 | UT-10-01、UT-14-06 | ⚠️ 部分 |
| N5 失败不留半成品 | T-12 | **UT-12-07** | ✅ 已补（G8） |
| N6 Python | — | 天然满足 | ✅ |

**尚存缺口（需后续补齐）**：
- **N4 可追溯**只有局部用例，缺一条端到端断言：给定线上 URL → 反查到源资料 + 稿件版本 + 执行日志（建议加 `IT-04-02`）
- **R18 第 4 项「提前喊停」**只写了日报含预告，**缺「怎么停」的机制与用例**（建议加 `UT-19-08`）

---

*配套文档：`geo-publish-PRD.md` v1.2（要什么）、`geo-publish-architecture.md` v0.1（怎么分层）、`geo-publish-alignment-review.md`（一致性核对）。本文档回答：按什么顺序做、每步怎么测、坏了怎么查。*
*变更记录：v1.0 初稿；v1.1 补齐一致性缺口 G1–G8，新增 §9 需求覆盖索引。*
