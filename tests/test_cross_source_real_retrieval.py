"""使用真实文件解析、切片、BM25 检索；不模拟检索结果，也不连接数据库。"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.tools.cross_source_demo import CrossSourceDemoTool, _DocumentEvidenceUnavailable, _RunBudget
from eval.cross_source_reference_baseline import StageBReferenceBaseline

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "eval" / "cross_source" / "boundary"


class RealRetrievalTests(unittest.TestCase):
    def test_actual_bm25_first_query_misses_and_targeted_query_recovers(self) -> None:
        docs = FIXTURES / "retry_documents"
        with patch.dict(os.environ, {"CROSS_SOURCE_DEMO_DOCS_DIR": str(docs), "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT": "2"}):
            baseline = StageBReferenceBaseline(dsn="unused", docs_dir=docs)
            limits, doc_ref, attempt = baseline._retrieve_once("HT-EVAL-001")
            self.assertIsNone(limits)
            self.assertIsNone(doc_ref)
            self.assertEqual(attempt["status"], "required_metric_not_found")
            tool = CrossSourceDemoTool(dsn="unused")
            ref, limits, count, attempts = tool._retrieve_contract_limits("HT-EVAL-001", _RunBudget(5000))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status"], "required_metric_not_found")
        self.assertEqual(attempts[1]["status"], "required_metric_found")
        self.assertEqual(count, 8)
        self.assertEqual(limits["service_restored"], ("hours", 8))
        self.assertIn("10 修复计时约定", ref.section_path)
        self.assertIn("8 小时内", ref.snippet)

    def test_actual_bm25_exhausts_two_attempts_when_limit_is_absent(self) -> None:
        with patch.dict(os.environ, {"CROSS_SOURCE_DEMO_DOCS_DIR": str(FIXTURES / "missing_documents"), "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT": "2"}):
            with self.assertRaises(_DocumentEvidenceUnavailable) as caught:
                CrossSourceDemoTool(dsn="unused")._retrieve_contract_limits("HT-EVAL-001", _RunBudget(5000))
        self.assertEqual(len(caught.exception.attempts), 2)
        self.assertTrue(all(item["status"] == "required_metric_not_found" for item in caught.exception.attempts))

    def test_actual_bm25_finds_explicitly_undefined_repair_limit(self) -> None:
        docs = FIXTURES / "explicit_missing_documents"
        with patch.dict(os.environ, {"CROSS_SOURCE_DEMO_DOCS_DIR": str(docs), "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT": "2"}):
            ref, limits, count, attempts = CrossSourceDemoTool(dsn="unused")._retrieve_contract_limits(
                "HT-EVAL-001", _RunBudget(5000)
            )
        self.assertGreater(count, 0)
        self.assertNotIn("service_restored", limits)
        self.assertEqual(attempts[-1]["status"], "required_metric_absence_explicit")
        self.assertIn("不约定服务恢复总时限", ref.snippet)

    def test_repair_limit_citation_keeps_the_threshold_visible(self) -> None:
        docs = ROOT / "eval" / "cross_source_heldout" / "2026Q4_v0.2" / "data" / "documents"
        with patch.dict(os.environ, {"CROSS_SOURCE_DEMO_DOCS_DIR": str(docs), "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT": "2"}):
            ref, limits, _count, attempts = CrossSourceDemoTool(dsn="unused")._retrieve_contract_limits(
                "HT-HOLD-102", _RunBudget(5000)
            )

        self.assertEqual(limits["service_restored"], ("hours", 10))
        self.assertEqual(attempts[-1]["status"], "required_metric_found")
        self.assertIn("| 修复完成 | service_restored | 10小时内 |", ref.snippet)
        self.assertIsNotNone(ref.char_start)
        self.assertEqual(ref.char_end - ref.char_start, len(ref.snippet))

    def test_special_repair_limit_requires_matching_scope_and_explicit_precedence(self) -> None:
        docs = ROOT / "eval" / "cross_source_heldout" / "2026Q4_v0.3" / "data" / "documents"
        with patch.dict(os.environ, {"CROSS_SOURCE_DEMO_DOCS_DIR": str(docs)}):
            ref, limits, _count, attempts = CrossSourceDemoTool(dsn="unused")._retrieve_contract_limits(
                "HT-MED-107", _RunBudget(5000), "超声影像一体机"
            )

        self.assertEqual(attempts[-1]["status"], "required_metric_found")
        self.assertEqual(limits["service_restored"], ("hours", 4))
        self.assertIn("| 修复完成 | service_restored | 6小时内 |", ref.snippet)
        self.assertIn("| 修复完成 | service_restored | 4小时内 |", ref.snippet)
        self.assertIn("特别约定优先于上列标准服务时限", ref.snippet)
        self.assertEqual(ref.char_end - ref.char_start, len(ref.snippet))

    def test_service_limit_citation_includes_all_metrics_used_in_answer(self) -> None:
        docs = ROOT / "eval" / "cross_source_heldout" / "2026Q4_v0.3" / "data" / "documents"
        with patch.dict(os.environ, {"CROSS_SOURCE_DEMO_DOCS_DIR": str(docs)}):
            ref, limits, _count, _attempts = CrossSourceDemoTool(dsn="unused")._retrieve_contract_limits(
                "HT-MED-115", _RunBudget(5000), "ICU 数据网关"
            )

        self.assertEqual(limits["first_response"], ("minutes", 20))
        self.assertEqual(limits["onsite_arrival"], ("hours", 3))
        self.assertEqual(limits["service_restored"], ("hours", 6))
        self.assertIn("| 首次响应 | first_response | 20分钟内 |", ref.snippet)
        self.assertIn("| 现场到达 | onsite_arrival | 3小时内 |", ref.snippet)
        self.assertIn("| 修复完成 | service_restored | 6小时内 |", ref.snippet)


if __name__ == "__main__":
    unittest.main()
