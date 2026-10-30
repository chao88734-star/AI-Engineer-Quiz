# Keepstack Quiz Submission

- **Fork**: https://github.com/chao88734-star/AI-Engineer-Quiz
- **Branch**: `assignment/chao88734-star`
- **Final commit SHA**: `97e402571a37b29065ff22a11793d5e2ade4d129`
- **Based on upstream**: `3aa137ccde6f981981582f6f71b81448c72a9dab` (2026-10-29)

---

## 一、运行与验证步骤

### 环境准备
```bash
cd AI-Engineer-Quiz-main
pip install -r requirements.txt
```

### 运行全部测试（含原有 22 + 新增 12）
```bash
python -m pytest -v
```
预期输出：`34 passed, 2 skipped`（2 个 skip 是本地 CLIP 和 OCR 模型——原仓库就有，非本次改动）

### 启动服务器 + seed 演示数据
```bash
python -c "from keepstack.seed import run_seed; run_seed()"
python -m uvicorn keepstack.app:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000 登录 admin/admin
```

### 业务场景复现（Web 端）
1. 登录后进入 Library 视图
2. 顶部搜索框输入 `harbor`
3. Include tags 输入框输入 `approved` 和 `campaign`（分别回车）
4. Exclude tags 输入框输入 `internal`
5. Collection 下拉选择任一（或 Any）
6. 生效条件 chips 实时显示：`keyword "harbor"`、`+approved`、`+campaign`、`−internal`
7. 网格只展示同时含 approved+campaign 且不含 internal 的 harbor 素材
8. 底部分页控件显示页数（每页 24 条）
9. 点击 **Export CSV** 按钮 → 浏览器下载 `keepstack-assets-*.csv`
10. 用 Excel 打开 CSV（utf-8-sig BOM），多标签 `; ` 分隔

### API 直接调用（curl）
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/assets?q=harbor&tags=approved&tags=campaign&exclude_tags=internal&sort=newest"

curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/assets/export.csv?q=harbor&tags=approved&tags=campaign&exclude_tags=internal&sort=newest" \
  -o out.csv
```

---

## 二、实测结果

### 自动化测试（`pytest tests/`）
| 分组 | 数量 | 说明 |
|---|---|---|
| 原仓库 `test_core.py` | 22 passed + 2 skipped | 无回归 |
| 新增 `test_quiz_requirements.py` | **12 passed** | 覆盖题目全部验收点 |

12 个新增测试覆盖：

| 测试 | 断言 |
|---|---|
| 多标签 AND | 仅同时含 approved+campaign 的素材返回（4 条） |
| 排除 OR | 任一含 internal 的被剔除（3 条） |
| 同标签两边短路 | `tags=["approved"], exclude_tags=["approved"]` → 0 条 |
| q+collection+tags+active | 只返回 active，按 oldest 排序，created_at 相同靠 id 稳定 |
| 分页稳定性 | 5 条跨 3 页无重复无遗漏 |
| 导出 = 列表 | 逐页 UUID 并集 == 导出，顺序一致 |
| CSV 转义 | 标题含 `"quotes"`、逗号、换行、中文 → 往返正确 |
| 零结果导出 | 只有表头行 |
| 未登录导出 | 401 |
| 非法 limit/offset | 422 |
| 特殊字符不 500 | `!!!`, `"*`, 纯空白, emoji, `AND OR` → 200 |
| 修改后查询 | PATCH 改 title → 重新 query + export 都反映新标题 |

### 手动验证
- 特殊字符查询 `q=!!!` 不再 500（修复前实测复现了 `fts5: syntax error near ""`）
- `status=deleted` 无法绕过（接口已移除 status 参数，固定传 `active`）
- 前端请求竞态：快速切换 include/exclude → 最终响应对应最新条件（reqSeq 丢弃过期响应）

---

## 三、提交拆分（11 个 commit，无 squash/amend/rebase）

```
97e4025 fix(search): collection_id JOIN ON 参数顺序 bug + 新增 12 个需求测试
651b2e3 feat(web): 组合筛选栏 + 生效条件 chips + 分页 + 导出按钮
f04af8d feat(api): /api/assets/export.csv 全量导出
356137f fix(api): 列表固定只返回 active + 新增 tags/exclude_tags 参数
40fabc0 fix(search): 有关键词时按所选排序 + id 稳定次序
f4412c0 feat(search): 多标签包含/排除组合筛选
10906d3 fix(search): 纯标点/空白关键词按无关键词处理
```

> 最后一个 commit 额外修了一个开发中发现的 **SQL 参数绑定顺序 bug**（`JOIN ON` 里的 `?` 先于 `WHERE` 出现，但 params 列表顺序反了，导致 collection_id 查询返回 0）。这是在跑 pytest 时发现的，修后 collection 相关测试全部通过。

---

## 四、主要实现取舍

| 决定 | 收益 | 成本 |
|---|---|---|
| 不新增依赖，CSV 用标准库 `csv` + `utf-8-sig` | 无额外安装；Excel 直接打开不乱码 | — |
| 同一查询构造函数 `_build_where_join_order()` 供列表和导出复用 | 保证筛选/排序/导出一致性，改一处改两处 | 多一层抽象 |
| 标签筛选用 `EXISTS` / `NOT EXISTS` 子查询 | AND 语义天然正确；不需要 GROUP BY/HAVING；SQLite 优化器可按索引 | 每个 include tag 多一条子查询，但数量 ≤ 10 条可接受 |
| 排序统一追加 `a.id` 作为稳定次序键 | 相同 created_at 不跨页重复/遗漏 | 多一个 ORDER BY 列 |
| 有关键词时也遵守所选排序（去掉 bm25 强制排序） | 题目明确要求；用户习惯"自己选的排序一直生效" | 失去了 bm25 相关性排序能力（但题目允许） |
| 接口固定 `status="active"` | 杜绝 status=deleted 绕过（安全问题） | 管理员无法通过 API 查已删除素材（可在前端 detail 里看） |
| 前端请求序号守卫 `reqSeq` | 快速切换筛选时只展示最新结果 | 多几行代码 |
| 导出用裸 `fetch` + Blob（不走 api() helper） | 失败 toast 不触发全局 401 登出 | 复制了一下 Authorization header 逻辑 |
| 隐藏语义搜索下拉框 | 题目明确"可隐藏"；避免语义排序与结构化筛选语义冲突 | 语义搜索还在后端，只是前端入口隐藏 |
| facets 计数保持全局统计 | 简单；不影响结果正确性 | 不标"当前匹配数"（按题目允许） |
| 导出在内存聚合 | 本地 SQLite 场景量级可接受 | 大库需改流式分批 |

### 多标签分隔符：`; `
题目说"多标签格式自定"，选择用 `; `（分号加空格）。理由：
- CSV 标准分隔符是逗号，避免额外转义
- `;` 比 `,`/`/` 在标签名称中更少见
- Excel 双击单元格就能看到清晰分隔

---

## 五、限制与未完成项

| 项目 | 说明 |
|---|---|
| **大库导出** | 导出一次性加载全部匹配行到内存，`export_assets_data()` 用全量 SELECT。本地 SQLite OK，百万级素材需改为流式分批（`yield` + `csv.writerow`） |
| **facets 不联动当前筛选** | 左侧 facets 显示全局 Top tags / Media type 计数，不是"当前筛选下的 Top N"。题目允许，但用户可能误解 |
| **无跨请求快照** | 题目明确"不要求跨请求快照"。分页过程中若被删/被加，翻页可能出现重复或遗漏 |
| **标签搜索建议是 facets 全集** | datalist 建议来自 facets，不是"输入前缀匹配"。用起来够用但不精细 |
| **`search.py` 里旧的单 `tag` 参数保留** | 向后兼容。题目允许，前端已改用 `tags[]` 数组 |
| **`collection_id` 排序 bug 已修但 join-on-params 陷阱未抽象** | 以后再加新的 JOIN ON 参数仍可能犯同错。可考虑封装 `add_join_condition()` helper |
| **无 seed 子命令** | 题目要求"可复现的数据或生成方式"，用 `python -c "from keepstack.seed import run_seed; run_seed()"` 即可；未做 `python -m keepstack seed --scenario`（原仓库 seed.py 已够用） |

---

## 六、范围外的已知问题（只记录，不修）

- `remove_tag` 未重建 embedding / FTS（可能影响语义搜索和关键词搜索，但本次不涉及）
- OCR / CLIP 模型依赖本地二进制和 pip 包，安装较重（题目说可以跳过）
- `auth.login` 在 session 内用了 `on_event`（deprecated，FastAPI 推荐 lifespan）
