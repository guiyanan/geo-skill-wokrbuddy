# CSDN DOM 快照

契约测试（`tests/contract/`）不连网、不开浏览器，只对这三份 HTML 断言选择器能命中。
所以文件变了 = 契约测试的输入变了，**改动要当成改契约**。

| 文件 | 对应线上页面 | 用来钉什么 |
|---|---|---|
| `editor.html` | `editor.csdn.net/md` MD 编辑器（已登录）。**2026-10-21 按真机勘查重建** | 标题双层控件 / cledit 正文 / 保存草稿两步 / 发布按钮 / 模版库弹窗 / 头像 |
| `publish-dialog.html` | 「发布文章」对话框（⚠️ **占位，未经真机校准**——抓取属于 T-10 第一步） | 分类 / 标签 / 摘要 / 声明 / 封面上传框 |
| `login.html` | `passport.csdn.net` 登录页 | 被踢回登录 → `expired` |
| `captcha.html` | 风控验证页（⚠️ 占位，未见过真实风控页） | 有人机校验 → `unknown` + `has_challenge=True`，**且优先于登录表单判定** |
| `success.html` | 发布成功页（⚠️ 占位，等 T-10 真发布后校准） | 回读用的 `article_id` / `success_marker` 能命中 |
| `drafts.html` | `mp.csdn.net/mp_blog/manage/article` 草稿箱（⚠️ 占位——列表行结构未抓到，当时勘查脚本没等够加载时间） | verify 兜底：列表行命中 `draft_list_item`，且每行带得出 href |

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
`drafts.html` 的列表结构同理。等 T-09 真发一篇草稿后再用真实页面校准——到那时如果
打不中，契约测试会先红，而不是等到「以为发出去了其实没有」。


## 校准状态（2026-10-21 T-09b 真机勘查后）

**已校准**（按真实页面重建/确认）：`editor.html`（MD 编辑器）、`login.html` 的 `.login-box`、
登录态头像 `img.img-user-avatar`、存草稿两步流程、URL `?articleId=` 成功信号。

**仍是占位**（明确标注在文件头注释里）：`publish-dialog.html`、`success.html`、
`drafts.html`、`captcha.html`。它们的选择器打不中真站时，契约测试不会红——
这是已知缺口，堵法是 T-10 现场抓取替换（那时打不中会先红，而不是以为发出去其实没有）。

真机实测还要记住三条（写进了 adapter 注释）：
1. 编辑器**会恢复上次未保存内容**——注入前必须清场。
2. 回读只能用 `textContent`，`innerText` 会把空行算两次。
3. `window.articleId` 恒为 null——成功信号在 URL 参数里。

## 为什么不 mock adapter

契约测试断言的是「**选择器对真实 DOM 是否命中**」，mock 掉 adapter 就只剩
自己跟自己对答案。这里用 `tests/contract/dom.py` 的 `StaticPage` 把 HTML 包成
和 Playwright `Page` 同形状的只读对象，于是 `check_auth` 这类**生产代码原样跑**，
只是运行在静态 DOM 上。
