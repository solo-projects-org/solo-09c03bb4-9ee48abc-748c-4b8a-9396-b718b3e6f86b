# 正射影像批量下架服务（Orthophoto Take-down Service）

同一测绘期冗余正射影像的批量下架接口。核心约束：**下架后下游取图不能少覆盖**。
每张被申请下架的影像，都必须由本批下架后仍在架、米/像素不大于它（即同等或更精细）
的影像**完整覆盖**，覆盖可以由多张影像拼合而成；任一条件不满足，整批不生效。

技术栈：FastAPI + SQLite（标准库 `sqlite3`）+ Pydantic v2，Docker Compose 启动，
Swagger UI（`/docs`）可直接试下架和查询在架目录。

## 业务规则

1. 提交一批片号（`POST /withdrawals`），片号去重后逐张校验。
2. 任一片号不在目录中或已下架 → 整批拒绝（HTTP 409），返回该片号。
3. 下架后每张申请片号的覆盖矩形，必须能被**其余在架影像**（不含本批其他片号）
   中分辨率 `米/像素 ≤ 该片号` 的影像并集完整覆盖；允许合用多张。
4. 存在面积缺口 → 整批拒绝，返回**失败片号 + 一处未覆盖矩形**
   （同一平面坐标系下的正面积轴对齐矩形）。
5. 全部通过才在同一事务内把这批片号置为下架；拒绝时目录数据零变更。
6. 只改供数目录（SQLite 的 `is_active` 标记），**不删除、不移动任何影像文件**；
   JSON 源目录文件只读。

## 目录结构

```
app/
  config.py      环境变量配置（DB/JSON 路径、忙等待、网格上限）
  geometry.py    精确矩形覆盖判定（坐标压缩网格，无栅格化误差）
  database.py    SQLite 初始化、查询、BEGIN IMMEDIATE 串行化下架事务
  schemas.py     Pydantic 请求/响应模型（OpenAPI）
  main.py        FastAPI 路由
data/
  catalog.json   演示目录（EPSG:32650，同一测绘期 2024Q3，17 张，16 个场景）
tests/           35 项 pytest（几何单测 / API 规则 / 多线程并发）
Dockerfile, docker-compose.yml, requirements*.txt, pytest.ini
```

## 数据模型

JSON 目录（`data/catalog.json`）字段：

| 字段 | 含义 |
| --- | --- |
| `crs` / `units` | 全部矩形共用的平面坐标系与单位（演示为 EPSG:32650 / 米） |
| `images[].sheet_no` | 片号，目录内唯一 |
| `images[].period` | 测绘期 |
| `images[].bbox` | 有效覆盖矩形 `[min_x, min_y, max_x, max_y]` |
| `images[].resolution_mpp` | 分辨率，米/像素，越小越精细 |
| `images[].file_path` | 影像文件路径，仅作元数据保存，服务永不访问该文件 |

SQLite 表：`images`（片号、测绘期、矩形四角、分辨率、文件路径、`is_active`）、
`withdrawal_batches`（每次提交的提交/拒绝审计流水，含失败片号与缺口矩形）、
`catalog_meta`（坐标系等元数据与初始化标记）。

## 覆盖判定算法（精确，无误差）

轴对齐矩形求并集覆盖采用**坐标压缩**：取目标矩形与所有候选覆盖矩形的 x/y 边
投影成网格，网格内任一单元格要么被某矩形整体包含、要么完全不被包含。逐格用
中心点判定，找到第一个未覆盖格即返回该格矩形作为"一处未覆盖矩形"；零宽/零高
格跳过，共边不会产生伪缺口。另有 `IMAGERY_MAX_GRID_CELLS`（默认 200 万）兜底
防止病态目录造成过大网格。

## 并发正确性

- 每请求独立 SQLite 连接（不跨线程共享）。
- 写事务一律 `BEGIN IMMEDIATE`：进入事务即拿写锁，覆盖复核与状态更新在同一个
  串行化事务内完成，因此"本批生效时仍满足覆盖条件"——其他批次可能先于本批提交，
  但本批提交前看到的一定是最新在架状态。
- 锁冲突走 `busy_timeout` + 指数退避重试（默认 5 次），耗尽返回 503。
- 拒绝批次的审计记录用独立事务写入，不随校验事务回滚而丢失。

## 运行

### Docker Compose（要求环境有 Docker）

```bash
docker compose up --build
# Swagger:  http://localhost:8000/docs
# 健康检查: http://localhost:8000/health
```

`./data` 挂载到容器 `/data`：JSON 只读，SQLite 库 `imagery.db` 在该目录内生成。
改了 JSON 后可在 Swagger 调 `POST /admin/reinitialize?force=true` 重建库
（不带 `force` 时已初始化的库保持不变，幂等）。

### 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv && . .venv/bin/activate   # 或用 uv
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

路径可用环境变量覆盖：`IMAGERY_DB_PATH`、`IMAGERY_CATALOG_PATH`、
`IMAGERY_BUSY_TIMEOUT_MS`、`IMAGERY_MAX_GRID_CELLS`。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务状态、坐标系、在架/总数 |
| POST | `/withdrawals` | 批量下架（原子），body：`{"sheet_nos": ["ORTHO-001"]}` |
| GET | `/images?active=true` | 在架目录（`active=false` 查全目录含已下架） |
| GET | `/images/{sheet_no}` | 单片查询 |
| GET | `/withdrawals?limit=50` | 提交/拒绝审计流水 |
| POST | `/admin/reinitialize?force=false` | 从 JSON 初始化/重建 SQLite |

成功响应示例：

```json
{"batch_no":"WD-20261123-00001","status":"committed",
 "withdrawn_sheets":["ORTHO-001"],"withdrawn_count":1,
 "committed_at":"2026-11-23T16:18:09+00:00"}
```

拒绝响应示例（HTTP 409，缺口矩形为目标 footprint 内的正面积矩形）：

```json
{"detail":{"detail":"coverage gap after withdrawal","failed_sheet":"ORTHO-004",
 "uncovered_rect":{"min_x":503000.0,"min_y":3000000.0,
                   "max_x":503500.0,"max_y":3001000.0}}}
```

## 测试

```bash
pip install -r requirements-dev.txt
python -I -m pytest -q
```

---

# 收尾自测报告

## 做成了什么

- FastAPI 批量下架服务：片号/测绘期/同一平面坐标系覆盖矩形（四角坐标）/
  米-像素分辨率全部入库 SQLite，库由 JSON 目录初始化（幂等，可 force 重建）。
- 完整实现"本批下架后仍在架、分辨率不大于目标、可多张拼合、完整覆盖"规则；
  整批原子生效/拒绝；拒绝时给出失败片号和一处正面积未覆盖矩形。
- 精确几何判定（坐标压缩网格），覆盖拼合、内部空洞、非对齐边缘、共边、
  分辨率更粗不算覆盖等情形均有对应测试与演示数据。
- `BEGIN IMMEDIATE` 串行化事务 + 忙等待重试，覆盖复核与提交同一事务，
  保证核算期间其他批次可生效、本批生效瞬间条件仍成立。
- 审计流水（含拒绝原因、失败片号、缺口矩形）；在架目录/全目录/单片查询；
  Swagger UI 与 OpenAPI schema；Dockerfile + docker-compose + 健康检查；
  全程只更新 `is_active`，不触碰影像文件，JSON 源目录哈希实测不变。

## 实际验证过什么

- **35 项 pytest 全部通过**（`python -I -m pytest`），含 12 项几何单测、
  18 项 API 规则测试（成功批、拼合覆盖、缺口 409、粗分辨率拒绝、未知片号、
  重复下架、混合批原子性、批内成员不得互相覆盖、去重、空批 422、审计流水、
  重建幂等、JSON 不被改写）、5 项多线程并发测试；并发套连续重复 5 轮无抖动。
- 测试中发现并修复了一个真实缺陷：拒绝审计原本写在校验事务内，随回滚丢失；
  已改为独立事务写入并补回归测试。
- 真实 uvicorn 进程 + curl/urllib 端到端实测 15 个场景：3 个成功批
  （单张拼合覆盖、同足迹精细替代、两张一批）、4 类缺口拒绝（半幅、粗分辨率、
  内部空洞、边缘 100m 缺口，返回矩形坐标逐一核对正确）、已下架/未知片号拒绝、
  混合批整批不生效且其他片号仍在架、在架数 17→15 与提交结果一致、
  审计流水 3 committed / 7 rejected、`catalog.json` md5 前后一致、
  库里 `file_path` 原样保留仅 `is_active` 变化。
- 真实 HTTP 并发竞争：两片号各自单独可撤、联合必撤穿的场景下多轮验证
  恰好一个提交一个拒绝；同片号并发只有一个提交；4 请求并发（含必失败批）
  无丢失更新，拒绝原因与最终在架状态自洽。
- `/docs` 返回 200，OpenAPI 列出全部 6 个路径；`docker-compose.yml` YAML
  解析通过，全部源码 `py_compile` 通过。

## 没完成 / 受限项

- **本环境没有 Docker 守护进程**，`docker compose up --build` 未实际执行；
  Compose/Dockerfile 仅做了语法与配置静态校验，容器内运行需在有 Docker 的
  机器上验证（流程与本地 uvicorn 一致，镜像基于 python:3.11-slim）。
- 覆盖模型限定为**轴对齐矩形**（题目所述"有效覆盖矩形"）；若后续目录出现
  旋转矩形或多边形 footprint，需要扩展几何模块（网格思路可复用，交集计算需替换）。
- 未做鉴权/多租户与分页（审计接口仅 `limit`）；当前定位为内网运维工具，
  如需开放应加认证与写操作权限控制。
- 未引入 SQL 迁移工具（Alembic 等）：表结构随初始化脚本创建，结构变更目前
  靠 `force` 重建；因下架只改标记位、源数据在 JSON，重建不丢业务状态语义，
  但正式环境仍建议补迁移方案。
- 未提交 git commit（按约定需明确指示再提交）；工作区改动均已落盘可直接审阅。
