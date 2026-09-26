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
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)

    def _annotate_demo_disagreement(self):
        """item1 产生分歧（正向/中性），item2 双方一致（中性）。"""
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_unanimous_two_votes_resolve_and_freeze(self):
        self._annotate_demo_disagreement()
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        with self.assertRaisesRegex(DomainError, "分歧"):
            self.db.freeze_batch(self.batch, self.mgr)
        r1 = self.db.cast_vote(self.item1, "正向", "速度描述构成明确正向倾向", self.arb1)
        self.assertFalse(r1["resolved"])
        # 只有一票时不能冻结
        with self.assertRaisesRegex(DomainError, "最终结论"):
            self.db.freeze_batch(self.batch, self.mgr)
        r2 = self.db.cast_vote(self.item1, "正向", "与第一位意见相同，语义确实积极", self.arb2)
        self.assertTrue(r2["resolved"])
        self.assertEqual("unanimous", r2["method"])
        # 最终结论已出，重复投票被拒绝
        with self.assertRaisesRegex(DomainError, "已有最终结论"):
            self.db.cast_vote(self.item1, "中性", "我不同意以上结论", self.arb3)
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertIsNotNone(result["metrics"]["pairwise_agreement"])
        exported = self.db.export_gold(self.batch)
        self.assertEqual(2, len(exported["records"]))
        self.assertEqual("adjudication", exported["records"][0]["source"])
        self.assertEqual("正向", exported["records"][0]["label"])

    def test_split_votes_wait_for_third_arbitrator_review(self):
        self._annotate_demo_disagreement()
        self.db.cast_vote(self.item1, "正向", "速度描述构成明确正向倾向", self.arb1)
        split = self.db.cast_vote(self.item1, "中性", "陈述较客观，没有强烈情绪", self.arb2)
        self.assertEqual("split", split["status"])
        self.assertFalse(split["resolved"])
        # 两份票都保留、可追溯
        state = self.db.arbitration_history(self.item1)
        self.assertEqual("split", state["rounds"][-1]["status"])
        self.assertEqual(2, len(state["rounds"][-1]["votes"]))
        # 分歧待复核，不能冻结
        with self.assertRaisesRegex(DomainError, "最终结论"):
            self.db.freeze_batch(self.batch, self.mgr)
        # 首轮投票人不能复核自己的争议
        with self.assertRaisesRegex(DomainError, "第三名仲裁员"):
            self.db.cast_vote(self.item1, "正向", "我坚持之前的判断意见", self.arb1)
        # 复核结论必须二选一
        with self.assertRaisesRegex(DomainError, "首轮两份分歧意见"):
            self.db.cast_vote(self.item1, "负向", "第三种标签也不被允许提交", self.arb3)
        reviewed = self.db.cast_vote(self.item1, "正向", "按指南应读作积极评价", self.arb3)
        self.assertTrue(reviewed["resolved"])
        self.assertEqual("review", reviewed["method"])
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertIsNotNone(result)
        exported = self.db.export_gold(self.batch)
        self.assertEqual("正向", exported["records"][0]["label"])
        history = self.db.arbitration_history(self.item1)
        self.assertEqual(3, len(history["rounds"][-1]["votes"]))
        self.assertEqual("resolved", history["rounds"][-1]["status"])

    def test_annotation_change_voids_prior_votes_and_restarts(self):
        self._annotate_demo_disagreement()
        self.db.cast_vote(self.item1, "正向", "速度描述构成明确正向倾向", self.arb1)
        self.db.cast_vote(self.item1, "中性", "陈述较客观，没有强烈情绪", self.arb2)
        # 分歧待复核期间，标注员修改标签：原表决作废，重新发起
        self.db.submit_annotation(self.item1, self.a2, "正向", "复核指南后改为正向")
        state = self.db.arbitration_history(self.item1)
        self.assertEqual("voided", state["rounds"][-1]["status"])
        self.assertIsNone(state["rounds"][-1]["adjudication"])
        # 作废轮次不再阻止冻结（此时标注已一致）
        item = self.db.get_item_for_user(self.item1, self.arb1)
        self.assertIsNone(item["arbitration"])
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertIsNotNone(result)

    def test_voting_can_restart_on_a_new_disagreement(self):
        # 另一条目出现分歧后可重新发起全新表决，仲裁员可再次投票
        self.db.submit_annotation(self.item2, self.a1, "中性", "句意平稳")
        self.db.submit_annotation(self.item2, self.a2, "负向", "改判以触发新争议")
        self.db.cast_vote(self.item2, "中性", "句意平稳，没有褒贬色彩", self.arb1)
        again = self.db.cast_vote(self.item2, "中性", "同意中性，无明显情感", self.arb2)
        self.assertTrue(again["resolved"])
        self.assertEqual("unanimous", again["method"])
        history = self.db.arbitration_history(self.item2)
        self.assertEqual(1, history["rounds"][-1]["round_no"])
        self.assertEqual(2, len(history["rounds"][-1]["votes"]))

    def test_unanimous_then_change_reopens_round(self):
        self._annotate_demo_disagreement()
        # 两人一致已出最终结论，标注员后来改标签：结论作废
        self.db.cast_vote(self.item1, "正向", "速度描述构成明确正向倾向", self.arb1)
        self.db.cast_vote(self.item1, "正向", "语义积极，同意前一位", self.arb2)
        self.assertIsNotNone(
            self.db.conn.execute("SELECT id FROM adjudications WHERE item_id=?", (self.item1,)).fetchone()
        )
        self.db.submit_annotation(self.item1, self.a2, "负向", "重新考虑后认为是反讽")
        self.assertIsNone(
            self.db.conn.execute("SELECT id FROM adjudications WHERE item_id=?", (self.item1,)).fetchone()
        )
        history = self.db.arbitration_history(self.item1)
        self.assertEqual("voided", history["rounds"][-1]["status"])
        with self.assertRaisesRegex(DomainError, "分歧"):
            self.db.freeze_batch(self.batch, self.mgr)

    def test_answer_isolation_and_role_validation(self):
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
