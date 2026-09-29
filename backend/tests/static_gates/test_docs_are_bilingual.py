"""The docs site is bilingual, and stays that way (2026-09-28).

Why this gate exists
--------------------
``mkdocs.yml`` declares two locales (``en`` default, ``zh``) and the i18n
plugin is configured with ``fallback_to_default: true``. That fallback is
what makes the docs usable while a translation is being written — and it is
also what makes a *missing* translation invisible.

Add a page called ``docs/foo.md`` with no ``foo.zh.md`` and the build
succeeds, ``--strict`` passes, the language selector still appears, and a
Chinese reader who clicks through gets **English**. Nothing fails. The
promise made by the selector is that the other locale exists; nothing in
the toolchain checks it.

So this gate checks it, in the same spirit as the rest of
``static_gates/``: a claim the project makes about itself, enforced rather
than asserted.

What it pins
------------
1. **Pairing.** Every English page has a Chinese sibling and vice versa.
   A page that exists in one locale only is a page that silently falls back.
2. **The mechanism.** ``docs/requirements.txt`` must carry the i18n plugin,
   and ``mkdocs.yml`` must declare both locales. Removing either would let
   the *pairing* rule pass while the site quietly stopped being bilingual —
   the pairs would still be on disk, just not built.

Scope, stated honestly: the gate checks that a translation **exists**, not
that it is any good, current, or complete. A stale Chinese page next to an
updated English one passes. Keeping the two in step is a review job, and
that is worth saying out loud rather than letting a green run imply more
than it proves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS = _REPO_ROOT / "docs"
_MKDOCS_YML = _REPO_ROOT / "mkdocs.yml"
_DOCS_REQUIREMENTS = _DOCS / "requirements.txt"

#: Locale suffix for the non-default language, as configured in
#: ``mkdocs.yml`` (``docs_structure: suffix``).
_ZH_SUFFIX = ".zh.md"

#: Files under ``docs/`` that are not site pages and therefore have no
#: translation: the dependency list and any future assets.
_NON_PAGE_NAMES = frozenset({"requirements.txt"})


def english_pages() -> list[Path]:
    return sorted(
        p for p in _DOCS.rglob("*.md")
        if not p.name.endswith(_ZH_SUFFIX) and p.name not in _NON_PAGE_NAMES
    )


def chinese_pages() -> list[Path]:
    return sorted(p for p in _DOCS.rglob(f"*{_ZH_SUFFIX}"))


def _zh_sibling(page: Path) -> Path:
    return page.with_name(page.name[: -len(".md")] + _ZH_SUFFIX)


def _en_sibling(page: Path) -> Path:
    return page.with_name(page.name[: -len(_ZH_SUFFIX)] + ".md")


def test_every_english_page_has_a_chinese_translation() -> None:
    missing = [
        p.relative_to(_REPO_ROOT) for p in english_pages()
        if not _zh_sibling(p).exists()
    ]
    assert not missing, (
        "these pages have no Chinese translation, so a reader who switches "
        "locale silently gets English — the build succeeds and `--strict` "
        "passes, which is why this needs a gate rather than a build error. "
        "Add the `.zh.md` sibling, or drop the page:\n  "
        + "\n  ".join(str(p) for p in missing)
    )


def test_every_chinese_page_has_an_english_base() -> None:
    """The reverse direction: a stray translation of a deleted page would
    render as a page that exists in one locale only."""
    orphaned = [
        p.relative_to(_REPO_ROOT) for p in chinese_pages()
        if not _en_sibling(p).exists()
    ]
    assert not orphaned, (
        "these translated pages have no English original, so they are only "
        "reachable from one locale:\n  "
        + "\n  ".join(str(p) for p in orphaned)
    )


def test_the_bilingual_mechanism_is_configured() -> None:
    """Pairing on disk is not enough — the site has to build both.

    Without this, deleting the plugin (or a locale entry) would leave every
    ``.zh.md`` file present and unbuilt, and the pairing rules above would
    keep passing.
    """
    requirements = _DOCS_REQUIREMENTS.read_text(encoding="utf-8")
    assert "mkdocs-static-i18n" in requirements, (
        "docs/requirements.txt no longer pins mkdocs-static-i18n; CI would "
        "install a toolchain that cannot build the Chinese locale"
    )

    config = _MKDOCS_YML.read_text(encoding="utf-8")
    assert "i18n:" in config, "mkdocs.yml no longer configures the i18n plugin"
    assert "locale: en" in config and "locale: zh" in config, (
        "mkdocs.yml must declare both an `en` and a `zh` locale"
    )
    assert "docs_structure: suffix" in config, (
        "the pairing rules here assume the `suffix` structure (`page.md` + "
        "`page.zh.md`); another structure would make them check the wrong thing"
    )


def test_the_scan_is_not_empty() -> None:
    """Both walks must find something, or the pairing rules pass vacuously."""
    en, zh = english_pages(), chinese_pages()
    assert en, f"no English pages found under {_DOCS} — the walk is broken"
    assert zh, (
        f"no `{_ZH_SUFFIX}` pages found under {_DOCS} — every pairing rule "
        f"above would pass without checking anything"
    )


def test_readme_links_into_the_docs_tree_still_resolve() -> None:
    """The README is the front door to the site, and nothing else checks it.

    MkDocs never reads the repository README, so a link from it into
    ``docs/`` is invisible to ``--strict``. A rename inside the docs tree
    would leave the front door pointing at a 404.
    """
    import re

    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    link_re = re.compile(r"\[[^\]]*\]\((docs/[^)#]+)\)")
    targets = {m.group(1) for m in link_re.finditer(readme)}
    assert targets, "README no longer links into docs/ — update this test or restore the links"

    dangling = [t for t in sorted(targets) if not (_REPO_ROOT / t).exists()]
    assert not dangling, (
        "README links point at files that do not exist (the docs site would "
        "still build, so only this catches it):\n  " + "\n  ".join(dangling)
    )


@pytest.mark.parametrize("name", ["index", "workflow", "security"])
def test_known_pages_are_paired(name: str) -> None:
    """A cheap canary on three pages that definitely exist.

    If someone restructures ``docs/`` and both walks end up empty, the
    non-empty assertion above fires — this pins the specific pages so the
    failure message says *which* ones vanished rather than only that the
    count dropped.
    """
    assert (_DOCS / f"{name}.md").exists(), f"docs/{name}.md is missing"
    assert (_DOCS / f"{name}{_ZH_SUFFIX}").exists(), (
        f"docs/{name}{_ZH_SUFFIX} is missing — the locale selector would "
        f"fall back to English for this page"
    )
