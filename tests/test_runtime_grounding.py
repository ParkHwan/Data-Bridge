"""Unit tests for line-level grounding (no live LLM needed)."""

from __future__ import annotations

import pytest

from databridge.agents.runtime import (
    NoEvidenceError,
    _bind_claims,
    _ground_answer,
    _to_grounded_answer,
)

DOC = {
    1: {
        "ref": 1,
        "source_id": "doc-ops-runbook",
        "title": "Deployment Runbook",
        "heading": "Release cadence",
        "breadcrumb": "Handbook > Engineering > Operations",
        "content": "Production deploys happen every Tuesday and Thursday.",
    },
    2: {
        "ref": 2,
        "source_id": "doc-risk-log",
        "title": "Risk Log",
        "heading": "R-01 Dual-write drift",
        "breadcrumb": None,
        "content": "Severity: high.",
    },
}


def test_markers_map_to_document_citations() -> None:
    ga = _to_grounded_answer("Deploys are twice weekly. [1]", DOC, [])
    assert [c.source_id for c in ga.citations] == ["doc-ops-runbook"]
    assert ga.answer == "Deploys are twice weekly."


def test_unmarked_claim_is_dropped_but_grounded_line_survives() -> None:
    grounded, dropped = _ground_answer(
        "Deploys are twice weekly. [1]\nThis line is unsupported.", DOC, []
    )
    assert grounded.answer == "Deploys are twice weekly."
    assert dropped == ("This line is unsupported.",)
    assert [c.source_id for c in grounded.citations] == ["doc-ops-runbook"]


def test_all_unmarked_lines_are_refused() -> None:
    with pytest.raises(NoEvidenceError):
        _to_grounded_answer("Confident but uncited claim.\nAnother claim.", DOC, [])


def test_markers_referencing_unknown_refs_are_refused() -> None:
    with pytest.raises(NoEvidenceError):
        _to_grounded_answer("Answer. [9]", DOC, [])


def test_no_evidence_at_all_is_refused() -> None:
    with pytest.raises(NoEvidenceError):
        _to_grounded_answer("Answer. [1]", {}, [])


def test_report_table_drops_unresolved_rows_and_keeps_structure() -> None:
    text = "\n".join(
        [
            "## Action items",
            "| Owner | Action | Due | Source |",
            "| --- | --- | --- | --- |",
            "| Alice | Fix retry logic | 2026-08-01 | [2] |",
            "| Bob | Guess at a task | 2026-08-02 | [9] |",
        ]
    )
    answer, citations, dropped = _bind_claims(text, DOC)
    assert "## Action items" in answer
    assert "| Owner | Action | Due | Source |" in answer
    assert "| --- | --- | --- | --- |" in answer
    assert "Alice" in answer
    assert "[2]" not in answer
    assert "Bob" not in answer
    assert dropped == ("| Bob | Guess at a task | 2026-08-02 | [9] |",)
    assert [c.source_id for c in citations] == ["doc-risk-log"]


def test_table_scans_only_source_cell_and_preserves_brackets_in_other_cells() -> None:
    text = "\n".join(
        [
            "| Owner | Action | Due | Source |",
            "| --- | --- | --- | --- |",
            "| Alice | Fix array access [1] | 2026-08-01 | [2] |",
        ]
    )
    answer, citations, dropped = _bind_claims(text, DOC)
    assert "Fix array access [1]" in answer
    assert "| [2] |" not in answer
    assert dropped == ()
    assert [c.source_id for c in citations] == ["doc-risk-log"]


def test_bigquery_evidence_cites_sql_automatically() -> None:
    bq = [
        {
            "sql": "SELECT count(*) FROM `bigquery-public-data.thelook_ecommerce.orders`",
            "referenced_tables": ["bigquery-public-data.thelook_ecommerce.orders"],
            "row_count_returned": 1,
        }
    ]
    ga = _to_grounded_answer("There are N orders.", {}, bq)
    assert ga.answer == "There are N orders."
    assert ga.citations[0].kind == "bigquery"
    assert ga.citations[0].sql is not None and "SELECT" in ga.citations[0].sql


def test_mixed_evidence_combines_citations() -> None:
    bq = [
        {
            "sql": "SELECT 1",
            "referenced_tables": ["p.d.t"],
            "row_count_returned": 1,
        }
    ]
    grounded, dropped = _ground_answer(
        "Document risk is high. [2]\nUnsupported document claim.", DOC, bq
    )
    kinds = sorted(c.kind for c in grounded.citations)
    assert kinds == ["bigquery", "document"]
    assert grounded.answer == "Document risk is high."
    assert dropped == ("Unsupported document claim.",)


def test_code_fence_is_preserved_but_quotes_and_lists_require_markers() -> None:
    text = "\n".join(
        [
            "## Example",
            "```python",
            "print('literal [1] must survive')",
            "```",
            "> Supported quote. [1]",
            "> Unsupported quote.",
            "- Supported bullet. [1]",
            "- Unsupported bullet.",
            "* Supported bullet with punctuation [1]!",
            "1. Supported ordered item [1]?",
            "Grounded conclusion. [1]",
        ]
    )
    answer, citations, dropped = _bind_claims(text, DOC)
    assert "print('literal [1] must survive')" in answer
    assert "> Supported quote." in answer
    assert "> Unsupported quote." not in answer
    assert "- Supported bullet." in answer
    assert "- Unsupported bullet." not in answer
    assert "* Supported bullet with punctuation!" in answer
    assert "1. Supported ordered item?" in answer
    assert "Grounded conclusion." in answer
    assert dropped == ("> Unsupported quote.", "- Unsupported bullet.")
    assert [c.source_id for c in citations] == ["doc-ops-runbook"]


def test_marker_before_sentence_punctuation_is_bound_and_punctuation_survives() -> None:
    answer, citations, dropped = _bind_claims(
        "Deploys happen twice weekly [1].", DOC
    )
    assert answer == "Deploys happen twice weekly."
    assert dropped == ()
    assert [c.source_id for c in citations] == ["doc-ops-runbook"]


def test_multiple_claims_on_one_line_collect_and_remove_every_ref() -> None:
    answer, citations, dropped = _bind_claims(
        "Production deploys happen twice weekly. [1] Hotfix risk is high. [2]", DOC
    )
    assert answer == "Production deploys happen twice weekly. Hotfix risk is high."
    assert "[" not in answer
    assert dropped == ()
    assert [c.source_id for c in citations] == ["doc-ops-runbook", "doc-risk-log"]


def test_whole_line_refs_dedupe_in_first_seen_order_and_strip_unknown_refs() -> None:
    answer, citations, dropped = _bind_claims(
        "Risk first. [2] Deploy next. [1] Duplicate. [2] Unknown. [99]", DOC
    )
    assert answer == "Risk first. Deploy next. Duplicate. Unknown."
    assert dropped == ()
    assert [c.source_id for c in citations] == ["doc-risk-log", "doc-ops-runbook"]


def test_marker_removal_normalizes_spaces_and_punctuation() -> None:
    answer, citations, dropped = _bind_claims(
        "First claim [1] .  Second claim [2] ,  third claim [1] !", DOC
    )
    assert answer == "First claim. Second claim, third claim!"
    assert dropped == ()
    assert [c.source_id for c in citations] == ["doc-ops-runbook", "doc-risk-log"]


def test_inline_refs_dedupe_and_preserve_first_seen_order() -> None:
    answer, citations, dropped = _bind_claims("Risk one. [2][1][2]\nRisk two. [1]", DOC)
    assert answer == "Risk one.\nRisk two."
    assert dropped == ()
    assert [c.source_id for c in citations] == ["doc-risk-log", "doc-ops-runbook"]


def test_inline_refs_tolerate_commas() -> None:
    answer, citations, dropped = _bind_claims("Combined claim. [1], [2]", DOC)
    assert answer == "Combined claim."
    assert dropped == ()
    assert len(citations) == 2


def test_marker_glued_to_korean_josa_leaves_no_stray_space() -> None:
    # "배포를 진행했다 [1]는 결정" must not become "… 진행했다 는 결정" — the space
    # before the removed marker is absorbed when a josa follows (review: Antigravity #4).
    answer, citations, dropped = _bind_claims("배포를 진행했다 [1]는 결정이 있었다.", DOC)
    assert answer == "배포를 진행했다는 결정이 있었다."
    assert dropped == ()
    assert [c.source_id for c in citations] == ["doc-ops-runbook"]


def test_marker_run_glued_to_josa_absorbs_space_once() -> None:
    answer, citations, dropped = _bind_claims("승인 완료 [1], [2]로 종료.", DOC)
    assert answer == "승인 완료로 종료."
    assert dropped == ()
    assert len(citations) == 2


def test_marker_before_spaced_korean_word_keeps_single_space() -> None:
    # A marker followed by a space then a Hangul word is a normal word boundary —
    # the space must survive.
    answer, citations, dropped = _bind_claims("정책 문서 [1] 참고 바랍니다.", DOC)
    assert answer == "정책 문서 참고 바랍니다."
    assert dropped == ()
