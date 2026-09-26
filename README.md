# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、双人表决仲裁、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

## 启动

```bash
python app.py
```

默认地址 <http://127.0.0.1:8112>，默认数据库为 `corpus.db`。首次启动会写入两位标注员、三位仲裁员和一个含分歧的示例批次。

```bash
PORT=9002 CORPUS_DB=/tmp/corpus.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：分配、标注、发现分歧、双人一致表决、分歧后第三名仲裁员复核、阻止提前冻结、标注变更作废旧表决并重新发起、计算一致性、冻结和导出；另一条测试验证提交答案前后讨论可见性变化，以及错误角色不能领取标注任务。

## 双人表决流程

每条争议由**两名仲裁员分别独立提交结论标签和理由**（`POST /api/adjudications`，字段 `proposed_label`/`reason`，兼容旧字段名 `final_label`）：

1. 第一票后轮次为 `voting`，批次不能冻结；
2. 两票结论一致（`unanimous`）才生成最终结论（`adjudications`）；
3. 两票结论不一致时两份票都保留，轮次进入 `split`，等待**第三名未参与首轮的仲裁员复核**；复核结论必须从首轮两种意见中二选一（多数决，`method=review`）；
4. 最终结论生成前批次一律不能冻结；
5. 标注员在定稿前（含已定稿后、冻结前）修改标签，原表决轮次标记为 `voided`（票保留可追溯）、最终结论删除，须重新发起新一轮表决；
6. 一名仲裁员在同一轮只能投一票；冻结后不能再投票或改标注。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/annotations`、`POST /api/adjudications`（投票/复核）
- `GET /api/items/{id}?user_id=`（含当前活跃表决 `arbitration` 与 `final_adjudication`）
- `GET /api/items/{id}/arbitration`（全部表决轮次、每轮的票与最终结论，可追溯）
- `GET /api/batches/{id}/disagreements`（每条分歧附 `arbitration` 活跃轮次状态）
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧、没有仍在 `voting`/`split` 的表决轮次；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。
