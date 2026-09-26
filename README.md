# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、双人仲裁表决、一致性指标、金标准冻结与导出，并以"提交前不可查看含答案讨论"的方式隔离讨论区答案。

## 仲裁表决规则

- 每条争议由**两名仲裁员**分别提交结论和理由，两人一致才生成最终结论。
- 两人意见不同时保留两份票，等待**第三名仲裁员**复核后裁定，其结论即为最终结论。
- 最终结论生成前（未表决或表决进行中）批次不能冻结。
- 标注员后续修改标签会使该条目原表决全部作废（作废票保留可追溯），并移除已生成的结论，表决自动进入下一轮重新发起；只改备注不影响表决。
- 同一仲裁员在同一轮只能表决一次；结论生成后该条目不再受理表决，除非标注变更触发重审。

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

测试包括：分配、标注、发现分歧、阻止提前冻结、双人表决（一致定稿 / 分歧由第三人复核）、标注变更作废表决并重新发起、计算一致性、冻结和导出；另一条测试验证提交答案前后讨论可见性变化，以及错误角色不能领取标注任务。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/annotations`、`POST /api/adjudications`（提交一票表决，返回轮次与表决状态）
- `GET /api/items/{id}?user_id=`、`GET /api/items/{id}/votes`（完整表决记录与最终结论）
- `GET /api/batches/{id}/disagreements`（含每条分歧的表决进度）
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、所有分歧均已生成最终仲裁结论；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。
