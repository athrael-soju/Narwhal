"""Write llms.txt from the site navigation and page descriptions."""

from pathlib import Path

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import Files
from mkdocs.structure.nav import Navigation, Section
from mkdocs.structure.pages import Page

_nav: list[Navigation] = []


def on_nav(nav: Navigation, config: MkDocsConfig, files: Files) -> Navigation:
    """Keep the navigation for the post-build step."""
    _nav[:] = [nav]
    return nav


def _title(page: Page) -> str:
    items = page.toc.items
    if items and items[0].level == 1:
        return items[0].title
    return page.title or page.file.src_uri


def _entry(page: Page, site_url: str) -> str:
    line = f"- [{_title(page)}]({site_url}{page.url})"
    description = page.meta.get("description")
    return f"{line}: {description}" if description else line


def _sections(title: str, items: list, site_url: str) -> list[tuple[str, list[str]]]:
    pages = [_entry(item, site_url) for item in items if isinstance(item, Page)]
    found = [(title, pages)] if pages else []
    for item in items:
        if isinstance(item, Section):
            found += _sections(item.title, item.children, site_url)
    return found


def on_post_build(config: MkDocsConfig) -> None:
    """Write llms.txt at the site root."""
    site_url = config.site_url or ""
    lines = [f"# {config.site_name}", "", f"> {config.site_description}", ""]
    for title, entries in _sections(config.site_name, _nav[0].items, site_url):
        lines += [f"## {title}", "", *entries, ""]
    Path(config.site_dir, "llms.txt").write_text("\n".join(lines), encoding="utf-8")
