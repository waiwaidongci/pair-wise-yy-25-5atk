from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


class CorpusDB:
    """A small multi-annotator corpus governance service."""

    def __init__(self, path: str = "corpus.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('annotator','arbitrator','manager'))
            );
            CREATE TABLE IF NOT EXISTS guidelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version TEXT NOT NULL UNIQUE,
              rules TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','annotating','frozen')),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL,
              text TEXT NOT NULL,
              UNIQUE(batch_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'assigned' CHECK(status IN ('assigned','submitted')),
              UNIQUE(item_id, annotator_id)
            );
            CREATE TABLE IF NOT EXISTS annotations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(item_id, annotator_id, guideline_id)
            );
            CREATE TABLE IF NOT EXISTS arbitration_rounds (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              round_no INTEGER NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('voting','split','resolved','voided')),
              created_at TEXT NOT NULL,
              resolved_at TEXT,
              UNIQUE(item_id, round_no)
            );
            CREATE TABLE IF NOT EXISTS arbitration_votes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              round_id INTEGER NOT NULL REFERENCES arbitration_rounds(id) ON DELETE CASCADE,
              arbitrator_id INTEGER NOT NULL REFERENCES users(id),
              proposed_label TEXT NOT NULL,
              reason TEXT NOT NULL,
              stage TEXT NOT NULL CHECK(stage IN ('primary','review')),
              created_at TEXT NOT NULL,
              UNIQUE(round_id, arbitrator_id)
            );
            CREATE TABLE IF NOT EXISTS adjudications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              round_id INTEGER NOT NULL REFERENCES arbitration_rounds(id),
              final_label TEXT NOT NULL,
              reason TEXT NOT NULL,
              method TEXT NOT NULL CHECK(method IN ('unanimous','review')),
              arbitrator_id INTEGER REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discussions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              author_id INTEGER NOT NULL REFERENCES users(id),
              body TEXT NOT NULL,
              contains_answer INTEGER NOT NULL DEFAULT 0 CHECK(contains_answer IN (0,1)),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gold_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              source TEXT NOT NULL CHECK(source IN ('consensus','adjudication')),
              adjudication_id INTEGER REFERENCES adjudications(id),
              frozen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_freezes (
              batch_id INTEGER PRIMARY KEY REFERENCES batches(id),
              metrics_json TEXT NOT NULL,
              frozen_by INTEGER NOT NULL REFERENCES users(id),
              frozen_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        a1 = self.add_user("标注员甲", "annotator")
        a2 = self.add_user("标注员乙", "annotator")
        self.add_user("仲裁员甲", "arbitrator")
        self.add_user("仲裁员乙", "arbitrator")
        self.add_user("仲裁员丙", "arbitrator")
        guideline = self.add_guideline("v1", "标签仅可为 正向/负向/中性；先独立标注，不得查看他人答案。")
        batch = self.create_batch("情感标注示例", guideline)
        item1 = self.add_item(batch, 1, "这个更新让工作流畅了很多。")
        item2 = self.add_item(batch, 2, "功能没有变化，但也没有明显问题。")
        self.assign(item1, a1)
        self.assign(item1, a2)
        self.assign(item2, a1)
        self.assign(item2, a2)
        self.submit_annotation(item1, a1, "正向", "整体表达积极")
        self.submit_annotation(item1, a2, "中性", "描述较克制")
        self.submit_annotation(item2, a1, "中性")
        self.submit_annotation(item2, a2, "中性")

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"annotator", "arbitrator", "manager"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def add_guideline(self, version: str, rules: str) -> int:
        if not version.strip() or not rules.strip():
            raise DomainError("指南版本和规则不能为空")
        with self.transaction():
            cur = self.conn.execute("INSERT INTO guidelines(version,rules) VALUES(?,?)", (version.strip(), rules.strip()))
        return int(cur.lastrowid)

    def create_batch(self, name: str, guideline_id: int) -> int:
        if not name.strip() or not self.conn.execute("SELECT 1 FROM guidelines WHERE id=? AND active=1", (guideline_id,)).fetchone():
            raise DomainError("批次名称或指南无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO batches(name,guideline_id,created_at) VALUES(?,?,?)",
                (name.strip(), guideline_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def add_item(self, batch_id: int, ordinal: int, text: str) -> int:
        batch = self.conn.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] == "frozen":
            raise DomainError("批次不存在或已经冻结")
        if ordinal <= 0 or not text.strip():
            raise DomainError("序号必须大于0且文本不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO items(batch_id,ordinal,text) VALUES(?,?,?)", (batch_id, ordinal, text.strip()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("该批次序号已存在") from exc
        return int(cur.lastrowid)

    def assign(self, item_id: int, annotator_id: int) -> int:
        item = self.conn.execute("SELECT batch_id FROM items WHERE id=?", (item_id,)).fetchone()
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (annotator_id,)).fetchone()
        if not item or not user or user["role"] != "annotator":
            raise DomainError("条目不存在或用户不是标注员")
        with self.transaction():
            self.conn.execute("UPDATE batches SET status='annotating' WHERE id=? AND status='draft'", (item["batch_id"],))
            try:
                cur = self.conn.execute(
                    "INSERT INTO assignments(batch_id,item_id,annotator_id) VALUES(?,?,?)",
                    (item["batch_id"], item_id, annotator_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一标注员不能重复领取同一条目") from exc
        return int(cur.lastrowid)

    def submit_annotation(self, item_id: int, annotator_id: int, label: str, comment: str = "") -> int:
        if not label.strip():
            raise DomainError("标签不能为空")
        item = self.conn.execute(
            "SELECT i.*, b.guideline_id, b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        assignment = self.conn.execute(
            "SELECT * FROM assignments WHERE item_id=? AND annotator_id=?", (item_id, annotator_id)
        ).fetchone()
        if not item or not assignment:
            raise DomainError("只能提交已分配条目的标注")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能修改标注")
        existing = self.conn.execute(
            "SELECT label FROM annotations WHERE item_id=? AND annotator_id=? AND guideline_id=?",
            (item_id, annotator_id, item["guideline_id"]),
        ).fetchone()
        label_changed = existing is None or existing["label"] != label.strip()
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO annotations(item_id,annotator_id,guideline_id,label,comment,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, annotator_id, item["guideline_id"], label.strip(), comment.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                cur = self.conn.execute(
                    "UPDATE annotations SET label=?,comment=?,created_at=? WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (label.strip(), comment.strip(), datetime.now().isoformat(), item_id, annotator_id, item["guideline_id"]),
                )
                annotation_id = self.conn.execute(
                    "SELECT id FROM annotations WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (item_id, annotator_id, item["guideline_id"]),
                ).fetchone()["id"]
            else:
                annotation_id = int(cur.lastrowid)
            self.conn.execute("UPDATE assignments SET status='submitted' WHERE id=?", (assignment["id"],))
            if label_changed:
                # 标注发生变化，此前的表决与最终结论全部作废，需要重新发起双人表决
                self._void_rounds(item_id)
        return int(annotation_id)

    def _void_rounds(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM adjudications WHERE item_id=?", (item_id,))
        self.conn.execute(
            "UPDATE arbitration_rounds SET status='voided' WHERE item_id=? AND status IN ('voting','split','resolved')",
            (item_id,),
        )

    def add_discussion(self, item_id: int, author_id: int, body: str, contains_answer: bool = False) -> int:
        if not body.strip():
            raise DomainError("讨论内容不能为空")
        if not self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise DomainError("条目不存在")
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (author_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO discussions(item_id,author_id,body,contains_answer,created_at) VALUES(?,?,?,?,?)",
                (item_id, author_id, body.strip(), int(contains_answer), datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def get_item_for_user(self, item_id: int, user_id: int) -> dict:
        item = self.conn.execute(
            "SELECT i.id,i.batch_id,i.ordinal,i.text,g.version AS guideline_version,g.rules "
            "FROM items i JOIN batches b ON b.id=i.batch_id JOIN guidelines g ON g.id=b.guideline_id WHERE i.id=?",
            (item_id,),
        ).fetchone()
        if not item:
            raise DomainError("条目不存在")
        own = self.conn.execute(
            "SELECT id,label,comment,created_at FROM annotations WHERE item_id=? AND annotator_id=?", (item_id, user_id)
        ).fetchone()
        revealed = own is not None
        discussions = []
        for row in self.conn.execute(
            "SELECT d.*,u.name FROM discussions d JOIN users u ON u.id=d.author_id WHERE d.item_id=? ORDER BY d.id", (item_id,)
        ).fetchall():
            if row["contains_answer"] and not revealed:
                discussions.append({"id": row["id"], "author": row["name"], "body": "提交自己的标注后才能查看此讨论", "hidden": True})
            else:
                discussions.append(dict(row))
        payload = dict(item)
        payload["own_annotation"] = dict(own) if own else None
        payload["discussions"] = discussions
        payload["arbitration"] = self._active_round_state(item_id)
        final = self.conn.execute(
            "SELECT a.id,a.final_label,a.reason,a.method,a.round_id,a.created_at,u.name AS reviewer_name "
            "FROM adjudications a LEFT JOIN users u ON u.id=a.arbitrator_id WHERE a.item_id=?",
            (item_id,),
        ).fetchone()
        payload["final_adjudication"] = dict(final) if final else None
        return payload

    def disagreements(self, batch_id: int) -> list[dict]:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        result = []
        for item in self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall():
            rows = self.conn.execute(
                "SELECT a.*,u.name FROM annotations a JOIN users u ON u.id=a.annotator_id "
                "WHERE a.item_id=? AND a.guideline_id=? ORDER BY a.id",
                (item["id"], batch["guideline_id"]),
            ).fetchall()
            labels = {row["label"] for row in rows}
            has_final = self.conn.execute("SELECT 1 FROM adjudications WHERE item_id=?", (item["id"],)).fetchone()
            if len(rows) >= 2 and len(labels) > 1 and not has_final:
                result.append({
                    "item_id": item["id"], "ordinal": item["ordinal"], "text": item["text"],
                    "labels": [dict(row) for row in rows],
                    "arbitration": self._active_round_state(item["id"]),
                })
        return result

    def _active_round_state(self, item_id: int) -> dict | None:
        round_row = self.conn.execute(
            "SELECT * FROM arbitration_rounds WHERE item_id=? AND status IN ('voting','split') ORDER BY round_no DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        if not round_row:
            return None
        votes = self.conn.execute(
            "SELECT v.id,v.arbitrator_id,u.name AS arbitrator_name,v.proposed_label,v.reason,v.stage,v.created_at "
            "FROM arbitration_votes v JOIN users u ON u.id=v.arbitrator_id "
            "WHERE v.round_id=? ORDER BY v.id",
            (round_row["id"],),
        ).fetchall()
        return {
            "round_id": round_row["id"],
            "round_no": round_row["round_no"],
            "status": round_row["status"],
            "votes": [dict(v) for v in votes],
        }

    def cast_vote(self, item_id: int, proposed_label: str, reason: str, arbitrator_id: int) -> dict:
        """两名仲裁员分别投票；一致即出最终结论，分歧则等第三名仲裁员复核。"""
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (arbitrator_id,)).fetchone()
        item = self.conn.execute(
            "SELECT i.*,b.guideline_id,b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        if not item or not user or user["role"] != "arbitrator":
            raise DomainError("条目或仲裁员无效")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能重新仲裁")
        rows = self.conn.execute("SELECT label FROM annotations WHERE item_id=?", (item_id,)).fetchall()
        if len(rows) < 2:
            raise DomainError("至少需要两份标注才能发起仲裁表决")
        if not proposed_label.strip() or len(reason.strip()) < 5:
            raise DomainError("结论标签必填，仲裁理由至少5个字符")
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM adjudications WHERE item_id=?", (item_id,)).fetchone():
                raise DomainError("该争议已有最终结论；标注变更后才会重新发起表决")
            round_row = self.conn.execute(
                "SELECT * FROM arbitration_rounds WHERE item_id=? AND status IN ('voting','split') ORDER BY round_no DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            now = datetime.now().isoformat()
            if round_row is None:
                next_no = self.conn.execute(
                    "SELECT COALESCE(MAX(round_no),0)+1 FROM arbitration_rounds WHERE item_id=?", (item_id,)
                ).fetchone()[0]
                cur = self.conn.execute(
                    "INSERT INTO arbitration_rounds(item_id,round_no,status,created_at) VALUES(?,?,'voting',?)",
                    (item_id, next_no, now),
                )
                round_id = int(cur.lastrowid)
                stage = "primary"
            else:
                round_id = round_row["id"]
                stage = "review" if round_row["status"] == "split" else "primary"
            if stage == "review":
                prior = self.conn.execute(
                    "SELECT arbitrator_id,proposed_label FROM arbitration_votes WHERE round_id=? AND stage='primary'",
                    (round_id,),
                ).fetchall()
                prior_ids = {row["arbitrator_id"] for row in prior}
                if arbitrator_id in prior_ids:
                    raise DomainError("复核必须由未参与首轮表决的第三名仲裁员进行")
                options = {row["proposed_label"] for row in prior}
                if proposed_label.strip() not in options:
                    raise DomainError("复核结论必须从首轮两份分歧意见中选择其一")
            elif self.conn.execute(
                "SELECT COUNT(*) FROM arbitration_votes WHERE round_id=? AND stage='primary'", (round_id,)
            ).fetchone()[0] >= 2:
                raise DomainError("首轮表决已满两票，意见分歧时请由第三名仲裁员复核")
            if self.conn.execute(
                "SELECT 1 FROM arbitration_votes WHERE round_id=? AND arbitrator_id=?", (round_id, arbitrator_id)
            ).fetchone():
                raise DomainError("同一仲裁员在一轮表决中只能投一票")
            cur = self.conn.execute(
                "INSERT INTO arbitration_votes(round_id,arbitrator_id,proposed_label,reason,stage,created_at) VALUES(?,?,?,?,?,?)",
                (round_id, arbitrator_id, proposed_label.strip(), reason.strip(), stage, now),
            )
            vote_id = int(cur.lastrowid)
            primary_votes = self.conn.execute(
                "SELECT v.*,u.name AS arbitrator_name FROM arbitration_votes v JOIN users u ON u.id=v.arbitrator_id "
                "WHERE v.round_id=? AND v.stage='primary' ORDER BY v.id",
                (round_id,),
            ).fetchall()
            result: dict = {"vote_id": vote_id, "round_id": round_id, "resolved": False}
            if stage == "primary":
                if len(primary_votes) < 2:
                    result["status"] = "voting"
                elif primary_votes[0]["proposed_label"] == primary_votes[1]["proposed_label"]:
                    adjudication_id = self._create_unanimous_adjudication(item, round_id, primary_votes, now)
                    result.update({"status": "resolved", "resolved": True, "method": "unanimous",
                                   "adjudication_id": adjudication_id,
                                   "final_label": primary_votes[0]["proposed_label"]})
                else:
                    self.conn.execute("UPDATE arbitration_rounds SET status='split' WHERE id=?", (round_id,))
                    result["status"] = "split"
            else:
                review_vote = self.conn.execute(
                    "SELECT v.*,u.name AS arbitrator_name FROM arbitration_votes v JOIN users u ON u.id=v.arbitrator_id "
                    "WHERE v.round_id=? AND v.stage='review' ORDER BY v.id DESC LIMIT 1",
                    (round_id,),
                ).fetchone()
                adjudication_id = self._create_review_adjudication(item, round_id, primary_votes, review_vote, now)
                result.update({"status": "resolved", "resolved": True, "method": "review",
                               "adjudication_id": adjudication_id, "final_label": review_vote["proposed_label"]})
            return result

    def _create_unanimous_adjudication(self, item, round_id: int, votes, now: str) -> int:
        reason = "两名仲裁员一致：" + "；".join(f"{v['arbitrator_name']}：{v['reason']}" for v in votes)
        cur = self.conn.execute(
            "INSERT INTO adjudications(item_id,guideline_id,round_id,final_label,reason,method,arbitrator_id,created_at) "
            "VALUES(?,?,?,?,?,'unanimous',NULL,?)",
            (item["id"], item["guideline_id"], round_id, votes[0]["proposed_label"], reason, now),
        )
        self.conn.execute(
            "UPDATE arbitration_rounds SET status='resolved',resolved_at=? WHERE id=?", (now, round_id)
        )
        return int(cur.lastrowid)

    def _create_review_adjudication(self, item, round_id: int, primary_votes, review_vote, now: str) -> int:
        detail = "；".join(f"{v['arbitrator_name']}：{v['proposed_label']}（{v['reason']}）" for v in primary_votes)
        reason = f"首轮意见分歧（{detail}）；复核仲裁员{review_vote['arbitrator_name']}裁定：{review_vote['reason']}"
        cur = self.conn.execute(
            "INSERT INTO adjudications(item_id,guideline_id,round_id,final_label,reason,method,arbitrator_id,created_at) "
            "VALUES(?,?,?,?,?,'review',?,?)",
            (item["id"], item["guideline_id"], round_id, review_vote["proposed_label"], reason,
             review_vote["arbitrator_id"], now),
        )
        self.conn.execute(
            "UPDATE arbitration_rounds SET status='resolved',resolved_at=? WHERE id=?", (now, round_id)
        )
        return int(cur.lastrowid)

    def arbitration_history(self, item_id: int) -> dict:
        if not self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise DomainError("条目不存在")
        rounds = []
        for round_row in self.conn.execute(
            "SELECT * FROM arbitration_rounds WHERE item_id=? ORDER BY round_no", (item_id,)
        ).fetchall():
            votes = [dict(v) for v in self.conn.execute(
                "SELECT v.id,v.arbitrator_id,u.name AS arbitrator_name,v.proposed_label,v.reason,v.stage,v.created_at "
                "FROM arbitration_votes v JOIN users u ON u.id=v.arbitrator_id "
                "WHERE v.round_id=? ORDER BY v.id",
                (round_row["id"],),
            ).fetchall()]
            adj = self.conn.execute(
                "SELECT id,final_label,reason,method,arbitrator_id,created_at FROM adjudications WHERE round_id=?",
                (round_row["id"],),
            ).fetchone()
            rounds.append({**dict(round_row), "votes": votes, "adjudication": dict(adj) if adj else None})
        return {"item_id": item_id, "rounds": rounds}

    def consistency(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        items = self.conn.execute("SELECT id,ordinal FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        per_item, agreement_pairs, total_pairs = [], 0, 0
        label_totals: Counter[str] = Counter()
        all_annotation_count = 0
        for item in items:
            labels = [r["label"] for r in self.conn.execute(
                "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
            ).fetchall()]
            if len(labels) < 2:
                per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": None, "annotations": len(labels)})
                continue
            pairs = total = 0
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    total += 1
                    pairs += labels[i] == labels[j]
            agreement = pairs / total
            agreement_pairs += pairs
            total_pairs += total
            all_annotation_count += len(labels)
            label_totals.update(labels)
            per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": round(agreement, 4), "annotations": len(labels)})
        pairwise = agreement_pairs / total_pairs if total_pairs else None
        expected = sum((count / all_annotation_count) ** 2 for count in label_totals.values()) if all_annotation_count else None
        kappa = None
        if pairwise is not None and expected is not None and expected < 1:
            kappa = (pairwise - expected) / (1 - expected)
        return {
            "batch_id": batch_id,
            "items_with_multiple_annotations": total_pairs and sum(1 for row in per_item if row["agreement"] is not None),
            "pairwise_agreement": round(pairwise, 4) if pairwise is not None else None,
            "fleiss_kappa": round(kappa, 4) if kappa is not None else None,
            "items": per_item,
        }

    def freeze_batch(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("批次已冻结")
        items = self.conn.execute("SELECT id FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        if not items:
            raise DomainError("空批次不能冻结")
        disagreements = self.disagreements(batch_id)
        pending = [d["item_id"] for d in disagreements if d["arbitration"] is not None]
        if pending:
            raise DomainError(f"仍有 {len(pending)} 条争议的双人表决未出最终结论: {pending}")
        unresolved = [d["item_id"] for d in disagreements]
        if unresolved:
            raise DomainError(f"仍有 {len(unresolved)} 条分歧未仲裁: {unresolved}")
        open_rounds = [row[0] for row in self.conn.execute(
            "SELECT i.id FROM arbitration_rounds r JOIN items i ON i.id=r.item_id "
            "WHERE i.batch_id=? AND r.status IN ('voting','split')",
            (batch_id,),
        ).fetchall()]
        if open_rounds:
            raise DomainError(f"最终结论未生成，批次不能冻结，待决条目: {open_rounds}")
        missing = []
        for item in items:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
            ).fetchone()[0]
            if count < 2:
                missing.append(item["id"])
        if missing:
            raise DomainError(f"条目缺少至少两份标注: {missing}")
        metrics = self.consistency(batch_id)
        frozen_at = datetime.now().isoformat()
        with self.transaction():
            for item in items:
                labels = [r["label"] for r in self.conn.execute(
                    "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
                ).fetchall()]
                adj = self.conn.execute("SELECT * FROM adjudications WHERE item_id=?", (item["id"],)).fetchone()
                if adj:
                    label, source, adj_id = adj["final_label"], "adjudication", adj["id"]
                else:
                    label, source, adj_id = labels[0], "consensus", None
                self.conn.execute(
                    "INSERT INTO gold_records(batch_id,item_id,guideline_id,label,source,adjudication_id,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, item["id"], batch["guideline_id"], label, source, adj_id, frozen_at),
                )
            self.conn.execute(
                "INSERT INTO batch_freezes(batch_id,metrics_json,frozen_by,frozen_at) VALUES(?,?,?,?)",
                (batch_id, json.dumps(metrics, ensure_ascii=False), manager_id, frozen_at),
            )
            self.conn.execute("UPDATE batches SET status='frozen' WHERE id=?", (batch_id,))
        return {"batch_id": batch_id, "metrics": metrics, "frozen_at": frozen_at}

    def export_gold(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] != "frozen":
            raise DomainError("只有已冻结批次可以导出金标准")
        freeze = self.conn.execute("SELECT * FROM batch_freezes WHERE batch_id=?", (batch_id,)).fetchone()
        rows = self.conn.execute(
            "SELECT g.item_id,i.ordinal,i.text,g.label,g.source,g.frozen_at FROM gold_records g JOIN items i ON i.id=g.item_id "
            "WHERE g.batch_id=? ORDER BY i.ordinal", (batch_id,)
        ).fetchall()
        return {
            "batch_id": batch_id, "batch_name": batch["name"], "frozen_at": freeze["frozen_at"],
            "metrics": json.loads(freeze["metrics_json"]), "records": [dict(row) for row in rows],
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "guidelines": [dict(r) for r in self.conn.execute("SELECT * FROM guidelines ORDER BY id")],
            "batches": [dict(r) for r in self.conn.execute("SELECT * FROM batches ORDER BY id")],
            "items": [dict(r) for r in self.conn.execute("SELECT * FROM items ORDER BY batch_id,ordinal")],
            "arbitration_rounds": [dict(r) for r in self.conn.execute("SELECT * FROM arbitration_rounds ORDER BY id")],
            "arbitration_votes": [dict(r) for r in self.conn.execute("SELECT * FROM arbitration_votes ORDER BY id")],
            "adjudications": [dict(r) for r in self.conn.execute("SELECT * FROM adjudications ORDER BY id")],
        }
