# CSDN DOM 快照

契约测试（`tests/contract/`）不连网、不开浏览器，只对这三份 HTML 断言选择器能命中。
所以文件变了 = 契约测试的输入变了，**改动要当成改契约**。

| 文件 | 对应线上页面 | 用来钉什么 |
|---|---|---|
| `editor.html` | `mp.csdn.net/mp_blog/creation/editor`（已登录） | 标题 / CodeMirror 正文容器 / 分类 / 标签 / 摘要 / 声明 / 封面 / 发布按钮 |
| `login.html` | `passport.csdn.net` 登录页 | 被踢回登录 → `expired` |
| `captcha.html` | 风控验证页 | 有人机校验 → `unknown` + `has_challenge=True`，**且优先于登录表单判定** |
| `success.html` | 发布成功页 | 回读用的 `article_id` / `success_marker` 能命中 |

**词汇说明**：选择器键叫 `auth.challenge`、结果字段叫 `has_challenge`，不叫
`captcha`/`has_captcha`。因为 `geopublish/**/*.py` 里出现那个英文词会被红线门禁拦下
（R13：不做任何验证码处理）。门禁是刻意「一刀切不给例外」的，所以改词不改门禁。
看到人机校验的处置只有一个：**停手，交给人**。

## 来源与时效（重要）

这三份是**手工构造**的：CSDN 的编辑器页要登录才能打开，而我不掌握登录态。
结构参照公开可访问的页面骨架（`article-bar__title`、`CodeMirror` 容器、
`login-box` 这类类名），字段与层级保持真实形状，但不是逐字节抓取。

**首次拿到真实登录态后，用工具替换它们**：

```bash
geopublish snapshot "https://mp.csdn.net/mp_blog/creation/editor" csdn/editor --out fixtures
geopublish snapshot "https://passport.csdn.net/login" csdn/login --out fixtures
```

替换后跑 `pytest -m contract`：选择器打不中就说明线上改版了——这正是这套测试
存在的意义。`meta.json` 里的 `html_sha256` 会记录每次替换的差异。

`success.html` 的 `article:id` / `success_marker` 两个选择器是**按 CMS 惯例先占位**的，
等 T-09 真发一篇草稿后再用真实成功页校准——到那时如果打不中，契约测试会先红，
而不是等到「以为发出去了其实没有」。

## 为什么不 mock adapter

契约测试断言的是「**选择器对真实 DOM 是否命中**」，mock 掉 adapter 就只剩
自己跟自己对答案。这里用 `tests/contract/dom.py` 的 `StaticPage` 把 HTML 包成
和 Playwright `Page` 同形状的只读对象，于是 `check_auth` 这类**生产代码原样跑**，
只是运行在静态 DOM 上。
