import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CorpusDB, DomainError


class CorpusFlowTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb1 = self.db.add_user("仲裁一", "arbitrator")
        self.arb2 = self.db.add_user("仲裁二", "arbitrator")
        self.arb3 = self.db.add_user("仲裁三", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g = self.db.add_guideline("v1", "独立标注")
        self.batch = self.db.create_batch("测试批次", self.g)
        self.item1 = self.db.add_item(self.batch, 1, "这个版本很快。")
        self.item2 = self.db.add_item(self.batch, 2, "没有明显变化。")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def annotate_pair(self, item, label1, label2):
        self.db.assign(item, self.a1)
        self.db.assign(item, self.a2)
        self.db.submit_annotation(item, self.a1, label1)
        self.db.submit_annotation(item, self.a2, label2)

    def test_full_annotation_disagreement_adjudication_freeze_flow(self):
        self.annotate_pair(self.item1, "正向", "中性")
        self.annotate_pair(self.item2, "中性", "中性")
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        with self.assertRaisesRegex(DomainError, "分歧"):
            self.db.freeze_batch(self.batch, self.mgr)
        first = self.db.submit_adjudication_vote(self.item1, self.arb1, "正向", "速度描述构成明确正向倾向")
        self.assertEqual("awaiting_second", first["status"])
        self.assertEqual(1, first["round"])
        with self.assertRaisesRegex(DomainError, "表决进行中"):
            self.db.freeze_batch(self.batch, self.mgr)
        second = self.db.submit_adjudication_vote(self.item1, self.arb2, "正向", "同样认为情绪倾向为正")
        self.assertEqual("resolved", second["status"])
        self.assertEqual("正向", second["final_label"])
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertIsNotNone(result["metrics"]["pairwise_agreement"])
        exported = self.db.export_gold(self.batch)
        self.assertEqual(2, len(exported["records"]))
        self.assertEqual("adjudication", exported["records"][0]["source"])
        self.assertEqual("正向", exported["records"][0]["label"])

    def test_split_votes_require_third_arbitrator_review(self):
        self.annotate_pair(self.item1, "正向", "负向")
        self.annotate_pair(self.item2, "中性", "中性")
        self.db.submit_adjudication_vote(self.item1, self.arb1, "正向", "整体表达明显积极")
        split = self.db.submit_adjudication_vote(self.item1, self.arb2, "负向", "措辞实际偏向负面")
        self.assertEqual("awaiting_third", split["status"])
        self.assertIsNone(split["final_label"])
        with self.assertRaisesRegex(DomainError, "分歧"):
            self.db.freeze_batch(self.batch, self.mgr)
        with self.assertRaisesRegex(DomainError, "只能表决一次"):
            self.db.submit_adjudication_vote(self.item1, self.arb1, "中性", "重复表决应被拒绝")
        third = self.db.submit_adjudication_vote(self.item1, self.arb3, "中性", "复核后认定为中性")
        self.assertEqual("resolved", third["status"])
        self.assertEqual("中性", third["final_label"])
        record = self.db.adjudication_votes(self.item1)
        self.assertEqual(3, len(record["votes"]))
        self.assertTrue(all(v["status"] == "active" for v in record["votes"]))
        self.assertEqual("中性", record["final"]["final_label"])
        self.assertEqual(self.arb3, record["final"]["arbitrator_id"])
        with self.assertRaisesRegex(DomainError, "最终结论"):
            self.db.submit_adjudication_vote(self.item1, self.arb1, "正向", "结论已生成不能再表决")
        self.db.freeze_batch(self.batch, self.mgr)

    def test_annotation_change_voids_votes_and_restarts_voting(self):
        self.annotate_pair(self.item1, "正向", "中性")
        self.annotate_pair(self.item2, "中性", "中性")
        self.db.submit_adjudication_vote(self.item1, self.arb1, "正向", "第一位仲裁员理由")
        resolved = self.db.submit_adjudication_vote(self.item1, self.arb2, "正向", "第二位仲裁员理由")
        self.assertEqual("resolved", resolved["status"])
        # 只改备注不影响已生成的结论
        self.db.submit_annotation(self.item1, self.a1, "正向", "补充说明但不改标签")
        self.assertIsNotNone(self.db.adjudication_votes(self.item1)["final"])
        # 标注员改标签：原表决作废，结论移除
        self.db.submit_annotation(self.item1, self.a2, "负向", "重新判断为负向")
        record = self.db.adjudication_votes(self.item1)
        self.assertIsNone(record["final"])
        self.assertEqual(2, len(record["votes"]))
        self.assertTrue(all(v["status"] == "void" for v in record["votes"]))
        with self.assertRaisesRegex(DomainError, "分歧"):
            self.db.freeze_batch(self.batch, self.mgr)
        # 重新发起表决，进入第二轮
        first = self.db.submit_adjudication_vote(self.item1, self.arb1, "负向", "重新表决第一位理由")
        self.assertEqual(2, first["round"])
        second = self.db.submit_adjudication_vote(self.item1, self.arb2, "负向", "重新表决第二位理由")
        self.assertEqual("resolved", second["status"])
        self.assertEqual(2, second["round"])
        self.db.freeze_batch(self.batch, self.mgr)
        exported = self.db.export_gold(self.batch)
        self.assertEqual("负向", exported["records"][0]["label"])
        self.assertEqual("adjudication", exported["records"][0]["source"])

    def test_vote_role_and_annotation_prerequisites(self):
        self.annotate_pair(self.item1, "正向", "负向")
        with self.assertRaisesRegex(DomainError, "仲裁员无效"):
            self.db.submit_adjudication_vote(self.item1, self.a1, "正向", "标注员不能参与表决")
        with self.assertRaisesRegex(DomainError, "至少5个字符"):
            self.db.submit_adjudication_vote(self.item1, self.arb1, "正向", "短")
        with self.assertRaisesRegex(DomainError, "两份标注"):
            self.db.submit_adjudication_vote(self.item2, self.arb1, "中性", "标注不足不能表决")

    def test_answer_isolation_and_role_validation(self):
        self.db.assign(self.item1, self.a1)
        self.db.assign(self.item1, self.a2)
        self.db.add_discussion(self.item1, self.a2, "我认为是正向", True)
        secret = self.db.get_item_for_user(self.item1, self.a1)
        self.assertTrue(secret["discussions"][0]["hidden"])
        self.db.submit_annotation(self.item1, self.a1, "负向")
        visible = self.db.get_item_for_user(self.item1, self.a1)
        self.assertFalse(visible["discussions"][0].get("hidden", False))
        with self.assertRaisesRegex(DomainError, "标注员"):
            self.db.assign(self.item2, self.arb1)


if __name__ == "__main__":
    unittest.main()
