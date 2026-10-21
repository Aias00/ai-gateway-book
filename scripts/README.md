# 构建与验收

本管线只需要 Python 3.10+ 与 Pandoc 3.12.1。网页正文在构建时生成，不依赖 JavaScript 加载。搜索、字号、主题和阅读位置是渐进增强功能。主题偏好使用同域共享的 `aias-reader-theme`，字号与阅读位置按书籍分开保存。

```sh
python3 scripts/build.py
python3 scripts/verify.py
sh scripts/epubcheck.sh
python3 -m http.server 8767 --bind 127.0.0.1 --directory site
```

`epubcheck.sh` 默认下载 EPUBCheck 5.4.0 至被忽略的 `.tools/`，需要 Java 17+。已有工具可用 `EPUBCHECK_JAR=/absolute/path/to/epubcheck.jar` 与 `JAVA_BIN=/absolute/path/to/java` 指定。验证日志与对应 EPUB 的 SHA-256 保存在 `site/downloads/epubcheck.txt`，警告也会使检查失败。

另开终端执行浏览器验收；可复用现有 Playwright，或临时安装，不需要把依赖提交到仓库：

```sh
npm install --no-save --package-lock=false playwright@1.64.1
npx playwright install chromium
node scripts/browser-check.cjs
```

`PLAYWRIGHT_MODULE` 可指定已安装 Playwright 的绝对目录，`CHROMIUM_PATH` 可指定浏览器。`BOOK_URL` 可切换到实际部署的完整站点地址，默认 `http://127.0.0.1:8767/`。`BOOK_SCREENSHOTS` 可指定截图目录，默认使用系统临时目录。

## 书稿约定

- 元数据：`book/metadata.json`。必填 `title`、`author`、`lang`、`repository`；`repository` 支持 `owner/repository` 或完整 GitHub URL。
- 下载文件名：`slug`，缺省为仓库名。仅使用小写字母、数字和连字符。
- 可选：`subtitle`、`description`、`version`、`date`、`rights`、`site_url`、`short_title`、`cover_label`、`cover_lines`、`cover_subtitle`、`include_code`。`site_url` 默认由 GitHub 仓库名推导。
- 正文：自动发现 `book/chapters/*.md`，按文件名数字排序。使用 `00-preface.md`、`01-topic.md` 等文件名，每个文件有且只有一个一级标题。二级及以下标题用于章节内目录。
- 跨章链接使用 `[说明](02-topic.md)` 或 `[说明](02-topic.md#小节锚点)`；构建器会分别生成网页链接与 EPUB 内部锚点。每章标题会加独立前缀，避免多章“练习”等同名标题冲突。
- 本地图片须位于仓库内，可相对章节文件或 `book/` 引用；管线将其复制到站点并打入 EPUB。原生 SVG 可直接使用。
- `include_code` 默认为 `true`：自动把 `lab/` 与 `tests/` 中的文本源码生成独立代码附录。它计为附录，不计正文章数；实验 ZIP 与该附录读取相同文件。

生成目录 `site/`、`.build/` 与 `.tools/` 不提交。`site/downloads/` 包含 EPUB、离线 HTML、完整 Markdown、实验 ZIP 与 EPUB SHA-256。若有 `book/sources.json`、`validation.json`、`lab-results.json`，构建时会一并复制到下载区。

## 检查范围

`verify.py` 检查源文件指纹、完整逐章文字、EPUB 元数据/目录/封面/资源、网页与 EPUB 内部链接、搜索索引与实验包。它不判断章节是否讲透、外部引用是否正确，也不以章节数量认定书稿完成。事实审阅与实验实跑记录由作者分别完成。

浏览器检查覆盖暗色系统下默认浅色、手动主题与跨标签同步、字号保留、搜索、全部章节 320/390px 布局、原生目录、无 JavaScript 阅读/翻页、拒绝本地存储时的主题切换和下载入口。

## 发布

GitHub Pages 应设置为 GitHub Actions。推送 `main` 或手动运行工作流后，CI 执行实验单测、构建、结构验证、EPUBCheck 与浏览器检查，再部署整个 `site/`。

工作流也保留 `book-downloads` artifact。经人工确认整书内容和运行证据后，可用 GitHub CLI 手动发布 Release；以下命令仅为维护说明，不会由构建脚本自动执行：

```sh
gh release create v1.0.0 site/downloads/ai-gateway-book.epub site/downloads/SHA256SUMS \
  --title 'AI 网关与模型路由实战 v1.0.0' --notes-file /path/to/release-notes.md
```

后续其他书复用时，修改元数据、书稿、实验与该发布命令的文件名即可；不需要继承本书正文或来源清单。
