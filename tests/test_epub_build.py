"""EPUB builder + publication lint + asset check."""

from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from abi.epub import asset_manifest_check, build_epub, publication_lint
from abi.epub.epubcheck import _parse_report
from abi.project import ScaffoldRequest, scaffold_project


def _project(tmp_path: Path):
    project = scaffold_project(
        ScaffoldRequest(
            target_root=tmp_path / "zh-Hans",
            book_slug="t",
            source_lang="en",
            target_lang="zh-Hans",
            source_target="en-zh-Hans",
        )
    )
    project.book_yaml.write_text(
        "title: 草稿标题\nauthors: []\nlanguage: en\nidentifier: draft\n",
        encoding="utf-8",
    )
    project.finalized_book_yaml.write_text(
        "title: 测试书\nauthors: [作者]\nlanguage: zh-Hans\n"
        "identifier: urn:isbn:fixture\nrights: public domain\n"
        "publisher: LifeBook 书坊 译制\n",
        encoding="utf-8",
    )
    (project.chapters_final / "001_intro.md").write_text(
        "# 引言\n\n第一章正文，包含**强调**。\n\n- 甲\n- 乙\n", encoding="utf-8"
    )
    (project.chapters_final / "002_body.md").write_text(
        "# 第二章\n\n表格：\n\n| A | B |\n| - | - |\n| 1 | 2 |\n", encoding="utf-8"
    )
    return project


def test_build_epub_is_wellformed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert publication_lint(project).ok
    assert asset_manifest_check(project).ok
    res = build_epub(project)
    assert res.ok
    assert project.book_epub.exists()

    with zipfile.ZipFile(project.book_epub) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        info = zf.getinfo("mimetype")
        assert info.compress_type == zipfile.ZIP_STORED
        # OPF, nav, and chapters must be well-formed XML.
        opf = zf.read("OEBPS/content.opf")
        etree.fromstring(opf)
        assert "测试书".encode() in opf
        assert "草稿标题".encode() not in opf
        etree.fromstring(zf.read("OEBPS/nav.xhtml"))
        etree.fromstring(zf.read("OEBPS/chap_001_001_intro.xhtml"))


def test_publication_lint_flags_absolute_path(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project.chapters_final / "003_bad.md").write_text(
        "# 第三章\n\n见 /Users/someone/secret.png 中的图。\n", encoding="utf-8"
    )
    res = publication_lint(project)
    assert not res.ok
    assert any("absolute path" in e for e in res.hard_errors)


def test_publication_lint_rejects_ascii_quotes_adjacent_to_cjk(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project.chapters_final / "003_bad.md").write_text(
        '# 第三章\n\n所谓"现金交易"和\'永恒真理\'都应使用全角引号。\n',
        encoding="utf-8",
    )

    res = publication_lint(project)

    assert not res.ok
    assert any("half-width ASCII quotation mark" in error for error in res.hard_errors)


def test_asset_check_flags_missing_image(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project.chapters_final / "004_img.md").write_text(
        "# 第四章\n\n![图](assets/figures/missing.png)\n", encoding="utf-8"
    )
    res = asset_manifest_check(project)
    assert not res.ok


def test_epubcheck_text_summary_does_not_count_words_as_errors(tmp_path: Path) -> None:
    output = """Validating using EPUB version 3.3 rules.
No errors or warnings detected.
Messages: 0 fatals / 0 errors / 0 warnings / 0 infos
EPUBCheck completed
"""

    assert _parse_report(tmp_path / "missing-report.json", output) == (0, 0, 0)
