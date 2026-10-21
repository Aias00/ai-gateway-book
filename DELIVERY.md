# v1.0.0 交付核验

## 产品与来源

- 书名：AI 网关与模型路由实战。
- 正文 17 章；前言 1 篇；练习答案、来源、术语、自动生成实验源码 4 个附录，共 22 个阅读单元。
- 产品源码提交：`18d84e1e1bf756a2b91b6f8de838c2f7d25e71bf`。
- [GitHub](https://github.com/Aias00/ai-gateway-book)
- [在线阅读](https://aias00.github.io/ai-gateway-book/)
- [在线 EPUB](https://aias00.github.io/ai-gateway-book/downloads/ai-gateway-book.epub)
- [Release v1.0.0](https://github.com/Aias00/ai-gateway-book/releases/tag/v1.0.0)

## 运行与格式证据

[运行 39568610281](https://github.com/Aias00/ai-gateway-book/actions/runs/39568610281) 的 build 与 deploy 均成功：26 项真实 HTTP 集成测试、Pandoc 构建、逐章源稿/网页/EPUB 正文一致性、EPUBCheck 5.4.0 零错误零警告，以及阅读器浏览器检查。

本地完整 demo 记录位于 `lab/evidence/demo.json`；第 13—15 章的 curl、路由开关和故障示例均已实跑。CR-only 完整事件在后续数据到来之前即可派发，且有真实 HTTP 回归；CR 字节计量采取保守上界，临界大帧可能提前拒绝。

部署后使用 `BOOK_URL=https://aias00.github.io/ai-gateway-book/` 再运行浏览器检查：全部 22 个单元在 320/390px 无横向溢出，浅色默认、深色与同域主题键、跨标签同步、目录、搜索、字号、禁用 JavaScript 翻页与存储拒绝均通过。桌面与移动截图已目视复核。

## 产物一致性

Release 直接使用上述成功 CI 的 `book-downloads` artifact，没有用本地重建文件替代。已独立完整下载 Pages 与 Release 的 EPUB 并核验 ZIP、mimetype 和 SHA-256；与 CI 文件一致：

```text
84e7acd05f4a5350d285b1701514e16209bd901e347f62c027579f7c43fab0fa  ai-gateway-book.epub
```

## 范围限制

实验不调用真实模型，不具有真实供应商 tokenizer、价格或 SLA 保证；使用 UTF-8 字节 credit。单进程回环 HTTP、内存账本和审计，不支持 TLS、多副本一致性、持久恢复、生产权限系统与供应商自动对账。正文将这些能力作为需要另行实现与验证的设计。

本文件只记录产品验收与发布证据；八本写作路线的整体状态维护在主页仓库 `WRITING_ROADMAP.md`，该整体目标尚未完成。
