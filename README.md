# 正射影像下架与取图清单服务（Orthophoto Take-down & Image-Selection Service）

包含两块能力：

1. **批量下架**：同一测绘期冗余正射影像的原子下架。核心约束是**下架后下游取图不能少覆盖**——
   每张被申请下架的影像都必须由本批下架后仍在架、米/像素不大于它的影像完整覆盖
   （可多张拼合），否则整批不生效。
2. **取图清单（manifest）**：提交测绘期和一批带编号的矩形瓦片，返回清单号及每片的
   **取图分区**（哪块区域用哪一张影像、分辨率、文件路径），空白区域显式标出。
   凭清单号可回查原清单，也可与**现行目录**比对，只报出必须重切的瓦片。

技术栈：FastAPI + SQLite（标准库 `sqlite3`）+ Pydantic v2，Docker Compose 启动，
Swagger UI（`/docs`）可直接演示取图、回查与复查。服务**只读目录元数据，永不读取影像文件**。

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

## 取图清单规则

登记（`POST /manifests`）时提交测绘期和一批 `{tile_no, bbox}`：

1. 每块瓦片按**逐点**最精细原则取图：只用**当期在架**影像；每一点取覆盖该点、
   `米/像素` 最小的影像；同分辨率按**片号升序**择一。
2. 瓦片被切分为若干**取图分区**（zone），每个分区给出所用片号、分辨率、文件路径、
   分区矩形与面积；无任何在架影像覆盖的部分标为**空白分区**（`sheet_no: null`）。
   整片空白时 `is_blank=true`。
3. 登记与复查各自在**同一事务、同一时刻**读取现行目录，并把当时的目录冻结成快照，
   因此清单可以被原样回查，复查两侧都有可复现的对照基准。
4. 清单号形如 `IS-YYYYMMDD-00001`；复查号 `RV-YYYYMMDD-00001`，均可持久回查，
   服务重启后仍然有效。**下架操作和强制重建都不会遍历或删除清单**。

复查（`POST /manifests/{manifest_no}/review`）与现行目录比对：

1. 只报**正面积区域**上片号、分辨率或文件路径发生变化的瓦片；**有图↔空白互换**
   同样算变化。
2. **分区切法变了但取图没变，不算变化**，不要求重切。
3. 只**重算**与"变动影像的新旧覆盖范围"有正面积交集的瓦片；影像**新增**与**下架**
   都算变动（下架把 `is_active` 翻转为 0）。其他瓦片不动。
4. 坐标系的 `crs` 或 `units` 与登记时不同 → 无法比较，返回 HTTP 409
   `status=not_comparable`，且不进行任何重算；该结论同样会入库存档。
5. 每次复查都存档：变动的片号、候选瓦片、重算数量、逐瓦片的变动区域
   （含新旧片号/分辨率/路径），可凭复查号回查。

## 目录结构

```
app/
  config.py      环境变量配置（DB/JSON 路径、忙等待、网格上限、清单/瓦片上限）
  geometry.py    精确矩形覆盖判定与矩形相交（坐标压缩网格，无栅格化误差）
  selection.py   逐点最精细取图分区、变动影像检测、分区覆盖比对（纯几何）
  database.py    SQLite 初始化、下架事务、目录快照、清单登记/回查/复查
  schemas.py     Pydantic 请求/响应模型（OpenAPI）
  main.py        FastAPI 路由
data/
  catalog.json   演示目录（EPSG:32650，同一测绘期 2024Q3，17 张，16 个场景）
  catalog_v2.json 演示换版目录：同名坐标/单位，含文件路径变更、分辨率变更、
                 一张下架、一张新增，用于演示复查
tests/           74 项 pytest（几何 / 选区 / API 规则 / 清单与复查 / 多线程并发）
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
`catalog_meta`（坐标系等元数据与初始化标记）、
`catalog_snapshots` / `catalog_snapshot_images`（不可变目录快照）、
`image_selection_manifests` / `image_selection_tiles` / `image_selection_zones`
（清单、瓦片、取图分区）、`manifest_reviews`（复查结论与逐瓦片变动区域）。

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
`IMAGERY_BUSY_TIMEOUT_MS`、`IMAGERY_MAX_GRID_CELLS`、`IMAGERY_MAX_MANIFEST_TILES`。

### Swagger 演示取图与复查

1. `POST /manifests`，body 示例（2024Q3 的 5 块瓦片，含一块整片空白）：

   ```json
   {"period":"2024Q3","tiles":[
     {"tile_no":"TILE-A","bbox":[500000,3000000,502000,3002000]},
     {"tile_no":"TILE-B","bbox":[503000,3000000,504500,3001000]},
     {"tile_no":"TILE-C","bbox":[510000,3000000,511000,3001000]},
     {"tile_no":"TILE-D","bbox":[530000,3000000,531000,3001000]},
     {"tile_no":"TILE-BLANK","bbox":[590000,3000000,591000,3001000]}]}
   ```

   响应含 `manifest_no` 与每片的分区（`sheet_no`/`resolution_mpp`/`file_path`/`bbox`），
   空白分区 `sheet_no=null`。

2. `GET /manifests/{manifest_no}`：凭清单号回查原清单（原样）。
3. 换版演示：把 `data/catalog_v2.json` 装成目录后
   `POST /admin/reinitialize?force=true`，再
   `POST /manifests/{manifest_no}/review`。v2 对 ORTHO-009 改文件路径、
   对 ORTHO-002 提高分辨率、下架 ORTHO-012、新增 ORTHO-018；复查只返回
   受影响的 TILE-A / TILE-D，并给出逐区域的旧值→新值。
4. 把 `crs` 改成别的值再 `force` 重建并复查 → HTTP 409 `not_comparable`
   （坐标/单位变了不能比）。改回后仍可回查所有历史复查（`GET /reviews/{no}`）。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务状态、坐标系、在架/总数 |
| POST | `/withdrawals` | 批量下架（原子），body：`{"sheet_nos": ["ORTHO-001"]}` |
| GET | `/images?active=true` | 在架目录（`active=false` 查全目录含已下架） |
| GET | `/images/{sheet_no}` | 单片查询 |
| GET | `/withdrawals?limit=50` | 提交/拒绝审计流水 |
| POST | `/admin/reinitialize?force=false` | 从 JSON 初始化/重建 SQLite |
| POST | `/manifests` | 登记取图清单，body：`{"period": "...", "tiles": [{"tile_no","bbox"}]}` |
| GET | `/manifests?limit=50` | 最近清单概要 |
| GET | `/manifests/{manifest_no}` | 凭清单号回查原清单 |
| POST | `/manifests/{manifest_no}/review` | 与现行目录比对，返回需重切的瓦片 |
| GET | `/manifests/{manifest_no}/reviews` | 某清单的历史复查 |
| GET | `/reviews/{review_no}` | 凭复查号回查复查结论 |

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

清单/复查响应示例（节选）：

```json
{"manifest_no":"IS-20261124-00001","period":"2024Q3","crs":"EPSG:32650",
 "units":"metre","tile_count":2,
 "tiles":[{"tile_no":"TILE-A","bbox":[500000,3000000,502000,3002000],
   "is_blank":false,"blank_area_sqm":0.0,
   "zones":[{"sheet_no":"ORTHO-002","resolution_mpp":0.25,
             "file_path":"/data/tiles/ORTHO-002.tif",
             "bbox":[500000,3000000,501000,3002000],"is_blank":false},
            {"sheet_no":"ORTHO-003","resolution_mpp":0.25, "...": "..."}]}]}
```

```json
{"review_no":"RV-20261124-00002","manifest_no":"IS-20261124-00001",
 "status":"changed","comparable":true,
 "changed_sheets":["ORTHO-002","ORTHO-009","ORTHO-012","ORTHO-018"],
 "candidate_tiles":["TILE-A","TILE-D"],
 "recomputed_tile_count":2,"changed_tile_count":2,
 "changed_tiles":[{"tile_no":"TILE-D","changed_area_sqm":1000000.0,
   "changed_regions":[{"bbox":[530000,3000000,531000,3001000],
     "old":{"sheet_no":"ORTHO-012","resolution_mpp":0.25,...},
     "new":{"sheet_no":"ORTHO-010","resolution_mpp":0.5,...}}]}]}
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
- 取图清单服务：提交测绘期 + 带编号矩形瓦片，逐点按"当期在架 + 最小米/像素 +
  同分辨率片号升序"切出取图分区，空白显式标出；清单号持久可回查。
- 复查：与现行目录比对，只报正面积区域上片号/分辨率/文件路径变化的瓦片
  （有图↔空白互换也算），分区切法变而取图未变不算；只重算与变动影像
  （新增/下架/改覆盖/改分辨率/改路径/改在架状态）新旧覆盖有正面积交集的瓦片。
- 登记、复查各自在单个 `BEGIN IMMEDIATE` 事务内取"同一时刻"的现行目录并
  冻结快照；`crs`/`units` 不一致时明确返回不可比（409）且不重算。
- 下架与强制重建都不遍历清单；清单、复查、快照全部入库，重启后可回查。

## 实际验证过什么

- **35 项 pytest 全部通过**（`python -I -m pytest`），含 12 项几何单测、
  18 项 API 规则测试（成功批、拼合覆盖、缺口 409、粗分辨率拒绝、未知片号、
  重复下架、混合批原子性、批内成员不得互相覆盖、去重、空批 422、审计流水、
  重建幂等、JSON 不被改写）、5 项多线程并发测试；并发套连续重复 5 轮无抖动。
- 测试中发现并修复了一个真实缺陷：拒绝审计原本写在校验事务内，随回滚丢失；
  已改为独立事务写入并补回归测试。
- **74 项 pytest 全部通过**（本次新增 39 项）：29 项清单 API 测试（登记/回查/
  重启持久化/复查/不可比/403·409/持久化/演示数据一致性）、10 项选区纯几何测试
  （含随机矩形逐点暴力对照、分区面积精确等于瓦片面积）；原有 35 项下架/几何/
  并发测试保持全绿。
- 清单端到端（真实 uvicorn + HTTP，2024Q3 演示目录）：5 块瓦片登记，
  TILE-A 选中 ORTHO-002+003、TILE-B 选中 ORTHO-004+005 且右端 500m 空、
  TILE-BLANK 整片空白；立即复查 `unchanged`（重算 0 片）。
- 换版到 `catalog_v2.json` 后复查：`changed_sheets` 精确列出
  ORTHO-002/009/012/018，候选瓦片 `TILE-A`/`TILE-D`，重算 2 片、变动 2 片；
  TILE-A 因 ORTHO-002 分辨率变化（0.25→0.2 m/px，片号不变但分辨率变，需重切）、
  TILE-D 由下架的 ORTHO-012 改取 ORTHO-010；
  未受影响的 TILE-B/C/BLANK 未被重算。
- 坐标系改成 EPSG:3857 后复查返回 409 `not_comparable`；重启 uvicorn 进程后
  清单、历史复查（`unchanged`/`changed`/`not_comparable` 三条）仍可逐条回查。
- 强制重建（force）前后清单号与内容一致，证明重建不清理清单。
- `/docs` 200，OpenAPI 列出全部 10 个路径；`docker-compose.yml` YAML 解析通过，
  源码与测试 `py_compile`/`pyflakes` 通过。
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
- 取图分区假定轴对齐矩形影像与瓦片；旋转/多边形 footprint 未支持（与下架模块共用限制）。
- 复查按清单登记的测绘期比对；若清单需要跨期重选，需要重新登记清单（当前不提供
  跨期对比，因为不同期的坐标系/单位语义可能不同）。
- 瓦片数量上限默认 1000（`IMAGERY_MAX_MANIFEST_TILES`），单次请求超大清单应分批。
