#!/usr/bin/env python3
"""Verify publishing integrity; this does not judge factual or editorial quality."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import posixpath
import re
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
import zipfile

import build

ROOT, SITE = build.ROOT, build.SITE


def normalized(text):
    return re.sub(r'\s+', '', text)


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.content = set(), [], []
        self.in_article = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            assert attrs['id'] not in self.ids, ('duplicate HTML id', attrs['id'])
            self.ids.add(attrs['id'])
        if tag == 'article':
            self.in_article = True
        for attr in ['href', 'src', 'poster']:
            if attrs.get(attr):
                self.links.append(attrs[attr])

    def handle_endtag(self, tag):
        if tag == 'article':
            self.in_article = False

    def handle_data(self, data):
        if self.in_article:
            self.content.append(data)


def main():
    meta = build.metadata()
    expected = build.parse_chapters(build.chapters(meta))
    info = json.loads((SITE / 'build-info.json').read_text())
    assert info['chapter_count'] == len(expected), 'Chapter inventory differs from source.'
    assert [c['slug'] for c in info['chapters']] == [c['slug'] for c in expected]
    merged = '\n\n'.join(c['markdown'] for c in expected)
    assert build.sha(merged.encode()) == info['manuscript_sha256']
    assert (SITE / 'downloads/book.md').read_text() == merged, 'Downloaded manuscript differs.'
    for chapter, record in zip(expected, info['chapters']):
        assert build.sha(chapter['markdown'].encode()) == record['sha256']
    docs = {}
    for path in SITE.rglob('*.html'):
        if path.parent.name == 'downloads':
            continue
        document = Document()
        document.feed(path.read_text())
        docs[path.resolve()] = document
    for path, document in docs.items():
        for link in document.links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                continue
            target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            assert target.is_relative_to(SITE.resolve()), ('link escapes site', path, link)
            assert target.is_file(), ('missing local resource', path, link)
            if parsed.fragment and target in docs:
                assert unquote(parsed.fragment) in docs[target].ids, ('missing HTML anchor', path, link)
    epub = SITE / 'downloads' / (meta['slug'] + '.epub')
    assert build.sha(epub.read_bytes()) == info['epub_sha256']
    with zipfile.ZipFile(epub) as archive:
        assert archive.infolist()[0].filename == 'mimetype'
        assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED
        assert archive.read('mimetype') == b'application/epub+zip'
        names = set(archive.namelist())
        xml = {name: ET.fromstring(archive.read(name)) for name in names
               if name.endswith(('.xhtml', '.opf', '.xml', '.ncx', '.svg'))}
        ids = {name: {node.get('id') for node in doc.iter() if node.get('id')}
               for name, doc in xml.items()}
        for name, document in xml.items():
            for node in document.iter():
                for attr in ['href', 'src']:
                    link = node.get(attr)
                    if not link:
                        continue
                    parsed = urlsplit(link)
                    if parsed.scheme or parsed.netloc:
                        continue
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path))) if parsed.path else name
                    assert target in names, ('missing EPUB resource', name, link)
                    if parsed.fragment:
                        assert unquote(parsed.fragment) in ids.get(target, set()), ('missing EPUB anchor', name, link)
        package_path = next(node.get('full-path') for node in xml['META-INF/container.xml'].iter()
                            if node.tag.endswith('}rootfile'))
        package = xml[package_path]
        dc = '{http://purl.org/dc/elements/1.1/}'
        assert package.find('.//' + dc + 'title').text == meta['title']
        assert package.find('.//' + dc + 'creator').text == meta['author']
        assert package.find('.//' + dc + 'language').text == meta['lang']
        manifest = {node.get('id'): node for node in package.iter() if node.tag.endswith('}item')}
        assert any('nav' in (node.get('properties') or '').split() for node in manifest.values()), 'EPUB navigation missing.'
        assert any('cover-image' in (node.get('properties') or '').split() for node in manifest.values()), 'EPUB cover missing.'
        for node in package.iter():
            if node.tag.endswith('}itemref'):
                assert node.get('idref') in manifest, 'Broken EPUB spine.'
        for chapter in expected:
            matches = [node for name, doc in xml.items() if name.endswith('.xhtml')
                       for node in doc.iter() if node.get('id') == chapter['anchor']]
            assert len(matches) == 1, ('missing or duplicated EPUB chapter', chapter['slug'])
            online = docs[(SITE / 'chapters' / (chapter['slug'] + '.html')).resolve()]
            epub_text = normalized(''.join(matches[0].itertext()))
            online_text = normalized(''.join(online.content))
            # Render the original source independently, before link/id transforms.
            source_text = normalized(build.plain(build.render(chapter['ast'])))
            assert source_text == online_text == epub_text, ('full chapter text differs', chapter['slug'])
    search = json.loads((SITE / 'search-index.json').read_text())
    assert [c['slug'] for c in search] == [c['slug'] for c in expected]
    for record, chapter in zip(search, expected):
        assert normalized(record['text']) == normalized(''.join(docs[(SITE / 'chapters' / (chapter['slug'] + '.html')).resolve()].content))
    with zipfile.ZipFile(SITE / 'downloads/lab.zip') as archive:
        for name, digest in info['lab_files'].items():
            assert build.sha((ROOT / name).read_bytes()) == digest == build.sha(archive.read(name))
    ET.fromstring((SITE / 'assets/cover.svg').read_text())
    print(f'PASS: {len(expected)} chapters match source, EPUB and static pages; metadata, navigation, local links, search and experiment archive valid.')
    print('This validates publishing integrity. Factual review and runtime experiment evidence remain separate checks.')


if __name__ == '__main__':
    main()
