"""Size each image in a panel row by its pixel width."""

import posixpath
import re
import struct
from pathlib import Path

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import Files
from mkdocs.structure.pages import Page

_ROW = re.compile(r'(<div class="narwhal-panel-row[^"]*">)(.*?)(</div>)', re.S)
_ITEM = re.compile(r'<p>(?=(?:(?!</p>).)*?<img[^>]*\bsrc="([^"]+\.png)")', re.S)


def _png_width(path: Path) -> int:
    with path.open("rb") as image:
        header = image.read(24)
    return int(struct.unpack(">I", header[16:20])[0])


def on_page_content(html: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    """Give each row image a flex share equal to its pixel width."""
    base = page.url

    def size(item: re.Match[str]) -> str:
        source = posixpath.normpath(posixpath.join(base, item.group(1)))
        width = _png_width(Path(config.docs_dir) / source)
        return f'<p style="flex-grow: {width}">'

    def row(match: re.Match[str]) -> str:
        return match.group(1) + _ITEM.sub(size, match.group(2)) + match.group(3)

    return _ROW.sub(row, html)
