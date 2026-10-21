#!/usr/bin/env python3
"""Publish one ordered Markdown manuscript as EPUB and a static reading site.

Configuration: book/metadata.json. Content: book/chapters/*.md. Only Python's
standard library and Pandoc are required. No network requests occur here.
"""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import quote, unquote, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / 'site'
BUILD = ROOT / '.build'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def run(args: list[str], text: str | None = None, cwd: Path = ROOT) -> str:
    result = subprocess.run(args, input=text, cwd=cwd, check=True,
                            capture_output=True, text=True, env=os.environ.copy())
    if result.stderr.strip():
        print(result.stderr.strip())
    return result.stdout


def metadata() -> dict:
    path = ROOT / 'book/metadata.json'
    if not path.exists():
        raise ValueError('Create book/metadata.json before building.')
    meta = json.loads(path.read_text())
    for key in ['title', 'author', 'lang', 'repository']:
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise ValueError(f'Missing metadata field: {key}')
    meta['repository'] = re.sub(r'^https://github\.com/', '', meta['repository']).removesuffix('.git').strip('/')
    meta.setdefault('slug', meta['repository'].split('/')[-1])
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', meta['slug']):
        raise ValueError('metadata.slug must be a lowercase URL-safe name.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', meta['repository']):
        raise ValueError('metadata.repository must be owner/repository.')
    meta.setdefault('subtitle', '')
    meta.setdefault('version', '1.0.0')
    meta.setdefault('description', meta['title'])
    meta.setdefault('date', date.today().isoformat())
    meta.setdefault('rights', 'Copyright © 2026 Aias00. 原创内容与第三方引用的授权范围见仓库说明。')
    meta.setdefault('include_code', True)
    owner, repository = meta['repository'].split('/')
    default_site = f'https://{owner.lower()}.github.io/'
    if repository.lower() != owner.lower() + '.github.io':
        default_site += repository + '/'
    meta.setdefault('site_url', default_site)
    meta['site_url'] = meta['site_url'].rstrip('/') + '/'
    return meta


def lab_files() -> list[Path]:
    suffixes = {'.py', '.json', '.toml', '.txt', '.md', '.sh', '.yaml', '.yml'}
    return sorted(p for folder in ['lab', 'tests'] for p in (ROOT / folder).rglob('*')
                  if p.is_file() and p.suffix in suffixes
                  and not any(part.startswith('.') or part == '__pycache__'
                              for part in p.relative_to(ROOT).parts))


def chapters(meta: dict) -> list[dict]:
    paths = sorted((ROOT / 'book/chapters').glob('*.md'),
                   key=lambda p: (int(re.match(r'\d+', p.stem)[0])
                                  if re.match(r'\d+', p.stem) else 10**6, p.stem))
    if not paths:
        raise ValueError('No Markdown chapters found in book/chapters/.')
    result = []
    for path in paths:
        if not re.fullmatch(r'\d+[a-z0-9_-]*', path.stem):
            raise ValueError(f'Use a numbered URL-safe chapter filename: {path.name}')
        text = path.read_text()
        heading = re.search(r'^# (.+)$', text, re.M)
        if not heading:
            raise ValueError(f'Chapter needs a level-one heading: {path.name}')
        title = re.sub(r'\s*\{#[^}]+\}\s*$', '', heading[1]).strip()
        result.append(dict(slug=path.stem, title=title, markdown=text,
                           path=path, generated=False))
    files = lab_files()
    if meta['include_code'] and files:
        text = '# 附录：随书实验完整代码\n\n'
        text += ('以下文件在构建时从随书实验与测试目录读取，与下载区实验包同源。'
                 '它们是本书教学实现；实际验证范围以实验章节和验证记录为准。\n\n')
        for path in files:
            language = {'.py': 'python', '.json': 'json', '.sh': 'bash',
                        '.yaml': 'yaml', '.yml': 'yaml'}.get(path.suffix, 'text')
            text += f'## {path.relative_to(ROOT).as_posix()}\n\n'
            text += f'~~~~~~{language}\n{path.read_text().rstrip()}\n~~~~~~\n\n'
        result.append(dict(slug='99-generated-lab', title='附录：随书实验完整代码',
                           markdown=text, path=ROOT / 'book/chapters/99-generated-lab.md',
                           generated=True))
    slugs = [c['slug'] for c in result]
    if len(set(slugs)) != len(slugs):
        raise ValueError('Generated appendix slug collides with an authored chapter.')
    return result


def walk(value):
    if isinstance(value, dict):
        if 't' in value:
            yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def parse_chapters(items: list[dict]) -> list[dict]:
    for chapter in items:
        chapter['ast'] = json.loads(run(['pandoc', '-f', 'markdown', '-t', 'json'], chapter['markdown']))
        headers = [node for node in walk(chapter['ast']['blocks']) if node['t'] == 'Header']
        if not headers or headers[0]['c'][0] != 1:
            raise ValueError(f'First heading must be level one: {chapter["slug"]}')
        if sum(node['c'][0] == 1 for node in headers) != 1:
            raise ValueError(f'Use one level-one heading per chapter: {chapter["slug"]}')
        ids = {}
        for index, node in enumerate(headers):
            old = node['c'][1][0]
            ids[old] = ('chapter-' + chapter['slug']) if index == 0 else chapter['slug'] + '--' + old
        chapter['ids'] = ids
        chapter['anchor'] = 'chapter-' + chapter['slug']
    return items


def prepare(chapter: dict, items: list[dict], meta: dict, output: str) -> dict:
    ast = copy.deepcopy(chapter['ast'])
    by_slug = {c['slug']: c for c in items}
    for node in walk(ast['blocks']):
        if node['t'] == 'Header':
            node['c'][1][0] = chapter['ids'][node['c'][1][0]]
        if node['t'] not in ['Link', 'Image']:
            continue
        target = node['c'][2][0]
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        decoded = unquote(parsed.path)
        if node['t'] == 'Image':
            candidates = [chapter['path'].parent / decoded, ROOT / 'book' / decoded, ROOT / decoded]
            source = next((p.resolve() for p in candidates if p.is_file()), None)
            if source is None or not source.is_relative_to(ROOT):
                raise ValueError(f'Missing or external image: {chapter["slug"]}: {target}')
            relative = Path('assets') / source.relative_to(ROOT)
            for destination in [BUILD / relative, SITE / relative]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            node['c'][2][0] = ('../' if output == 'web' else '') + relative.as_posix()
            continue
        destination = by_slug.get(Path(decoded).stem) if decoded.endswith('.md') else None
        if not decoded:
            destination = chapter
        if destination:
            anchor = destination['ids'].get(unquote(parsed.fragment)) if parsed.fragment else destination['anchor']
            if anchor is None:
                raise ValueError(f'Unknown chapter anchor: {chapter["slug"]}: {target}')
            node['c'][2][0] = ((destination['slug'] + '.html') if output == 'web' and decoded else '') + '#' + anchor
        elif decoded:
            candidate = (chapter['path'].parent / decoded).resolve()
            if not candidate.is_file() or not candidate.is_relative_to(ROOT):
                raise ValueError(f'Unknown local link: {chapter["slug"]}: {target}')
            relative = quote(candidate.relative_to(ROOT).as_posix())
            node['c'][2][0] = 'https://github.com/' + meta['repository'] + '/blob/main/' + relative
    ast['meta'] = {}
    return ast


def render(ast: dict) -> str:
    return run(['pandoc', '-f', 'json', '-t', 'html5', '--section-divs', '--wrap=none'], json.dumps(ast))


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(fragment: str) -> str:
    parser = PlainText()
    parser.feed(fragment)
    return ' '.join(parser.parts)


def cover(meta: dict) -> str:
    lines = meta.get('cover_lines')
    if not lines:
        lines, current, width = [], '', 0
        for char in meta['title']:
            unit = .55 if ord(char) < 128 else 1
            if width + unit > 8:
                lines.append(current.strip()); current, width = '', 0
            current += char; width += unit
        if current:
            lines.append(current.strip())
    title = ''.join(f'<text x="98" y="{380+i*116}" font-size="86" font-weight="650">{esc(line)}</text>'
                    for i, line in enumerate(lines))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1800" viewBox="0 0 1200 1800" role="img" aria-labelledby="title desc">
<title id="title">{esc(meta['title'])}</title><desc id="desc">浅色背景、青绿色路由图与中文书名组成的电子书封面。</desc>
<rect width="1200" height="1800" fill="#f7f7f3"/><rect x="60" y="60" width="1080" height="1680" rx="6" fill="none" stroke="#d8ded7" stroke-width="2"/>
<g fill="#292e34" font-family="PingFang SC,Noto Sans CJK SC,Microsoft YaHei,sans-serif">
<text x="98" y="168" font-size="25" letter-spacing="5" fill="#28685c">{esc(meta.get('cover_label', 'ENGINEERING NOTES'))}</text>
<path d="M98 215H1102" stroke="#28685c" stroke-width="3"/>{title}
<g fill="none" stroke="#28685c" stroke-width="6"><rect x="390" y="850" width="420" height="210" rx="30"/>
<path d="M98 955H390M810 955H920M920 955V770H1102M920 955H1102M920 955V1140H1102"/>
<circle cx="150" cy="955" r="24" fill="#f7f7f3"/><circle cx="1050" cy="770" r="24" fill="#f7f7f3"/><circle cx="1050" cy="955" r="24" fill="#f7f7f3"/><circle cx="1050" cy="1140" r="24" fill="#f7f7f3"/>
<path d="M460 920H740M460 955H670M460 990H710" stroke-width="4" stroke="#8ca79b"/></g>
<text x="98" y="1445" font-size="32" fill="#626b74">{esc(meta.get('cover_subtitle', '原理 · 实验 · 验证'))}</text>
<path d="M98 1535H1102" stroke="#d8ded7" stroke-width="2"/>
<text x="98" y="1640" font-size="38">{esc(meta['author'])}</text><text x="1102" y="1640" font-size="24" text-anchor="end" fill="#626b74">{esc(meta['version'])} · 中文版</text>
</g></svg>'''


def shell(meta: dict, items: list[dict], content: str, current: str = '') -> str:
    prefix = '../' if current else ''
    index = next((i for i, item in enumerate(items) if item['slug'] == current), -1)
    title = items[index]['title'] if index >= 0 else meta['title']
    navigation = ''.join('<a href="' + prefix + 'chapters/' + c['slug'] + '.html"'
                         + (' aria-current="page"' if c['slug'] == current else '')
                         + '>' + esc(c['title']) + '</a>' for c in items)
    pager = ''
    if index >= 0:
        previous = ('<a rel="prev" href="' + items[index-1]['slug'] + '.html">← 上一章：' + esc(items[index-1]['title']) + '</a>') if index else '<a href="../index.html">← 全书首页</a>'
        following = ('<a rel="next" href="' + items[index+1]['slug'] + '.html">下一章：' + esc(items[index+1]['title']) + ' →</a>') if index + 1 < len(items) else '<a href="../index.html">返回全书目录 →</a>'
        pager = '<nav class="pager" aria-label="章节翻页">' + previous + following + '</nav>'
    data = json.dumps([dict(slug=c['slug'], title=c['title']) for c in items], ensure_ascii=False).replace('<', '\\u003c')
    position = f'第 {index + 1} / {len(items)} 篇' if index >= 0 else '中文工程读本'
    canonical = meta['site_url'] + ('chapters/' + current + '.html' if current else '')
    return f'''<!doctype html>
<html lang="{esc(meta['lang'])}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{esc(meta['description'])}"><title>{esc(title)}{(' · ' + esc(meta['title'])) if current else ''}</title>
<link rel="canonical" href="{esc(canonical)}"><meta property="og:type" content="book"><meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(meta['description'])}"><meta property="og:url" content="{esc(canonical)}">
<meta name="theme-color" content="#f7f7f3"><link rel="icon" href="{prefix}assets/favicon.svg"><link rel="stylesheet" href="{prefix}style.css">
<script>try{{document.documentElement.dataset.theme=localStorage.getItem('aias-reader-theme')==='dark'?'dark':'light'}}catch{{}}</script>
</head><body data-root="{prefix}" data-chapter="{esc(current)}" data-book="{esc(meta['slug'])}">
<a class="skip" href="#content">跳到正文</a>
<header class="site-header"><a class="brand" href="{prefix}index.html">{esc(meta.get('short_title', meta['title']))}</a><a class="download" href="{prefix}downloads/{esc(meta['slug'])}.epub" download>EPUB ↓</a><button id="theme" class="aias-reader-theme" hidden aria-label="切换为深色模式">深色</button></header>
<aside id="sidebar"><details id="contents" open><summary>全书目录</summary><div class="contents-body"><p class="edition">{esc(meta['author'])} · {esc(meta['version'])}</p>
<form id="search-form" hidden><label for="search">搜索全书</label><div class="search-row"><input id="search" type="search" placeholder="概念、代码或章节" maxlength="120"><button type="submit">查找</button></div></form><p id="search-status" role="status" aria-live="polite"></p><div id="search-results"></div><nav aria-label="全书目录">{navigation}</nav></div></details></aside>
<main><div class="reading-tools"><span>{position}</span><button id="smaller" hidden aria-label="减小字号">A−</button><button id="larger" hidden aria-label="增大字号">A＋</button><a id="continue" hidden>继续阅读 →</a></div>
<article id="content" tabindex="-1">{content}</article>{pager}
<footer class="site-footer"><a href="{prefix}index.html">全书首页</a><a href="https://github.com/{esc(meta['repository'])}">GitHub 书稿与实验</a><span>© {esc(meta['author'])}</span></footer></main>
<script type="application/json" id="book-navigation">{data}</script><script src="{prefix}app.js" defer></script></body></html>'''


def main():
    meta = metadata()
    items = parse_chapters(chapters(meta))
    for directory in [SITE, BUILD]:
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir()
    for folder in ['chapters', 'assets', 'downloads']:
        (SITE / folder).mkdir()
    for name in ['style.css', 'app.js']:
        shutil.copy2(ROOT / 'web' / name, SITE / name)
    svg = cover(meta)
    (BUILD / 'cover.svg').write_text(svg)
    (SITE / 'assets/cover.svg').write_text(svg)
    (SITE / 'assets/favicon.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#28685c"/><path d="M12 32H24M40 20H52M40 44H52M24 20V44H40V20Z" fill="none" stroke="#f7f7f3" stroke-width="4"/></svg>')
    prepared = []
    search = []
    for chapter in items:
        fragment = render(prepare(chapter, items, meta, 'web'))
        fragment = re.sub(r'(<table\b.*?</table>)', r'<div class="table-wrap">\1</div>', fragment, flags=re.S)
        (SITE / 'chapters' / (chapter['slug'] + '.html')).write_text(shell(meta, items, fragment, chapter['slug']))
        search.append(dict(slug=chapter['slug'], title=chapter['title'], text=plain(fragment)))
        prepared.append(prepare(chapter, items, meta, 'book'))
    merged = '\n\n'.join(c['markdown'] for c in items)
    (SITE / 'downloads/book.md').write_text(merged)
    combined = copy.deepcopy(prepared[0])
    combined['blocks'] = [block for doc in prepared for block in doc['blocks']]
    (BUILD / 'book.json').write_text(json.dumps(combined))
    args = ['pandoc', str(BUILD / 'book.json'), '-f', 'json', '--standalone', '--toc', '--toc-depth=2',
            '--section-divs', '--data-dir=' + str(ROOT / 'epub'), '--resource-path=' + str(BUILD)]
    for key in ['title', 'subtitle', 'author', 'lang', 'date', 'rights']:
        args += ['--metadata=' + key + ':' + meta[key]]
    args += ['--metadata=identifier:urn:sha256:' + sha(merged.encode())]
    epub = SITE / 'downloads' / (meta['slug'] + '.epub')
    run(args + ['-t', 'epub3', '--split-level=1', '--css=' + str(ROOT / 'epub/style.css'),
                '--epub-cover-image=' + str(BUILD / 'cover.svg'), '-o', str(epub)], cwd=BUILD)
    run(args + ['-t', 'html5', '--embed-resources', '--css=' + str(ROOT / 'epub/style.css'),
                '-o', str(SITE / 'downloads' / (meta['slug'] + '.html'))], cwd=BUILD)
    with zipfile.ZipFile(SITE / 'downloads/lab.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in lab_files():
            archive.write(path, path.relative_to(ROOT).as_posix())
        for name in ['README.md', 'LICENSE']:
            if (ROOT / name).exists():
                archive.write(ROOT / name, name)
    source_links = ''
    for name in ['sources.json', 'validation.json', 'lab-results.json', 'metadata.json']:
        path = ROOT / 'book' / name
        if path.exists():
            shutil.copy2(path, SITE / 'downloads' / name)
            source_links += f'<a href="downloads/{name}">{esc(name)}</a> '
    authored = [c for c in items if not c['generated']]
    body_count = sum(bool(re.match(r'^第\s*[\d一二三四五六七八九十百]+\s*章', c['title'])) for c in authored)
    appendix_count = sum(c['title'].startswith('附录') for c in items)
    home = f'''<div class="hero"><div><p class="eyebrow">{esc(meta.get('cover_label', 'ENGINEERING NOTES'))}</p><h1>{esc(meta['title'])}</h1><p class="subtitle">{esc(meta['subtitle'])}</p><p>{esc(meta['description'])}</p><p class="book-stats">{body_count} 章正文 · {appendix_count} 个附录 · {esc(meta['author'])}</p><div class="hero-actions"><a class="primary" href="chapters/{items[0]['slug']}.html">开始阅读 →</a><a class="secondary" href="downloads/{esc(meta['slug'])}.epub" download>下载 EPUB</a></div></div><img class="cover" src="assets/cover.svg" width="1200" height="1800" alt="{esc(meta['title'])}封面"></div><h2 id="all-chapters">全书目录</h2><ol class="chapter-list">'''
    home += ''.join(f'<li><a href="chapters/{c["slug"]}.html">{esc(c["title"])}</a></li>' for c in items)
    home += f'''</ol><h2 id="downloads">下载与离线阅读</h2><p><a href="downloads/{esc(meta['slug'])}.epub">EPUB 电子书</a> · <a href="downloads/{esc(meta['slug'])}.html">离线 HTML</a> · <a href="downloads/book.md">完整 Markdown</a> · <a href="downloads/lab.zip">随书实验与测试</a></p><p class="evidence-links">{source_links}</p>'''
    (SITE / 'index.html').write_text(shell(meta, items, home))
    (SITE / 'search-index.json').write_text(json.dumps(search, ensure_ascii=False))
    chapter_info = [dict(slug=c['slug'], title=c['title'], anchor=c['anchor'], generated=c['generated'],
                         sha256=sha(c['markdown'].encode())) for c in items]
    info = dict(title=meta['title'], author=meta['author'], lang=meta['lang'], version=meta['version'],
                download_slug=meta['slug'], chapter_count=len(items), body_chapters=body_count,
                appendices=appendix_count, chapters=chapter_info,
                manuscript_sha256=sha(merged.encode()), epub_sha256=sha(epub.read_bytes()),
                epub_bytes=epub.stat().st_size,
                lab_files={p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in lab_files()})
    (SITE / 'build-info.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n')
    (SITE / 'downloads/SHA256SUMS').write_text(sha(epub.read_bytes()) + '  ' + epub.name + '\n')
    (SITE / '.nojekyll').touch()
    print(json.dumps({key: info[key] for key in ['title', 'chapter_count', 'body_chapters', 'appendices', 'epub_bytes']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
