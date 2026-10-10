"""Size each image in a panel row by its pixel width."""

import posixpath
import re
import struct
from pathlib import Path

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import Files
from mkdocs.structure.pages import Page

_ROW = re.compile(r'(<div class="narwhal-panel-row([^"]*)">)(.*?)(</div>)', re.S)
_ITEM = re.compile(r'<p>(?=(?:(?!</p>).)*?<img[^>]*\bsrc="([^"#]+\.png)(?:#[^"]*)?")', re.S)


def _png_width(path: Path) -> int:
    with path.open("rb") as image:
        header = image.read(24)
    return int(struct.unpack(">I", header[16:20])[0])


def on_page_content(html: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    """Give each row image a flex share equal to its pixel width.

    Stats rows on a page share one scale, set by the widest stats row. No image
    grows past its pixel width.
    """
    base = page.url

    def width(source: str) -> int:
        return _png_width(Path(config.docs_dir) / posixpath.normpath(posixpath.join(base, source)))

    rows = list(_ROW.finditer(html))
    widest = max(
        (
            sum(width(item.group(1)) for item in _ITEM.finditer(match.group(3)))
            for match in rows
            if "stats" in match.group(2).split()
        ),
        default=0,
    )

    def row(match: re.Match[str]) -> str:
        stats = "stats" in match.group(2).split()

        def size(item: re.Match[str]) -> str:
            pixels = width(item.group(1))
            if stats and widest:
                return f'<p style="flex: 0 1 {100 * pixels / widest:.3f}%; max-width: {pixels}px">'
            return f'<p style="flex-grow: {pixels}; max-width: {pixels}px">'

        return match.group(1) + _ITEM.sub(size, match.group(3)) + match.group(4)

    return _ROW.sub(row, html)
