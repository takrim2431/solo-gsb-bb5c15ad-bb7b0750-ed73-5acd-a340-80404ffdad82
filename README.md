# 供水管网故障隔离与复供服务

基于 **Python 3.12 + FastAPI + PostgreSQL** 的供水管网故障隔离与复供服务。

支持录入带编号的**水源(source)**、**用水点(sink)**、**连接点(junction)**及两端节点明确的**管段**（管段可设阀门）；
针对一条故障管段，预演并提交阀门关闭方案，自动判断**能否隔离**并计算**失供用水点**；
隔离提交后生成**待修复(pending_repair)**隔离事件，修复后操作员按**事件编号 + 当前版本**预演并确认**复供**，
事件最终变为**已复供(restored)**。

---

## 一、快速启动（Docker Compose 一键启动）

前置条件：安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build
```

该命令会：

1. 启动 PostgreSQL 16（端口 `5432`），并通过 `healthcheck` 等待数据库就绪；
2. 构建并启动 API 服务（端口 `8000`）；
3. API 启动时自动执行 `CREATE TABLE IF NOT EXISTS ...` 完成数据库初始化（无需手工建表）。

停止：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时清空数据库数据卷
```

## 二、服务地址与端口

| 服务 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |
| PostgreSQL | `localhost:5432`（库/用户/密码默认均为 `water`） |

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://water:water@localhost:5432/water` | PostgreSQL 连接串；Compose 中指向 `db` 服务 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库就绪的重试次数 |
| `DB_CONNECT_RETRY_INTERVAL` | `1.0` | 重试间隔秒数 |
| `HISTORY_LIMIT` | `100` | 历史记录接口返回上限 |

### 本地（非 Docker）运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 确保 PostgreSQL 已建库（create database water;）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 四、核心业务规则

**导入校验**（任一不满足即整体拒绝，返回全部问题明细，不写入数据）：

- 管段两端节点必须存在 —— 拒绝**悬空端点**；
- 节点编号、管段编号分别不可重复 —— 拒绝**重号**；
- 管段两端不可为同一节点 —— 拒绝**自环**；
- 没有阀门的管段不能标记为已关闭。

**隔离判定**（方案图 = 当前通水图移除故障管段 + 关闭拟关阀门，**已关闭阀门继续生效**）：

- 当前通水图中，**已关闭阀门**与所有**待修复事件的故障管段**都不可通行（隔离预演与提交均计入待修复故障）；
- 仅当故障管段**两端**在方案图中都**无法从任一水源到达**，方案才 `isolatable=true`，方可提交；
- 失供用水点 = **方案前**由任一水源可达、**方案后**不可达的用水点，按编号（字符串字典序）排序；
  方案前已经失供的点（如其他待修复事件造成的失供）不会重复计入。

**提交约束**（任一不满足即拒绝且不改任何状态）：

- `version` 必须等于当前管网版本，否则 `409 VERSION_CONFLICT`（版本过期）；
- 故障管段、拟关管段必须存在；拟关管段必须**带阀门**；
- 拟关列表不得重复、不得包含故障管段本身；已关闭的阀门不得重复关闭；
- 方案必须可隔离（`isolatable=true`），否则 `422 NOT_ISOLATABLE`；
- 提交在**单个数据库事务**内完成：行级锁校验版本 → 更新阀位 → 版本号 +1 → 写**待修复**隔离事件，原子生效。
- 整网导入为替换式：重写全部节点/管段、清空历史（**旧事件全部失效**）、版本重置为 `1`。

**复供（恢复供水）规则**：操作员按隔离事件编号与当前版本先预演、再确认。

- 复供预演（只读，**不改任何状态**）返回：
  该事件提交时所关阀门 `closed_pipe_ids`、复供后**实际重开**的阀门 `reopen_pipe_ids`、
  仍须保持关闭的阀门 `stay_closed_pipe_ids`（被其他待修复事件占用）、
  **预计恢复供水**的用水点 `restored_sink_ids`、以及 `safe` 与阻断原因 `issues`；
- 复供计算时排除：**其他待修复事件的故障管段**与**仍关闭的阀门**（本事件故障管段视为已修复）；
- 仅当重开后，**其余待修复事件的每一条故障管段两端仍都无法从水源到达**（无重新进水风险）才 `safe=true`，才允许确认；
- 确认在**同一事务**内：锁行复核版本与事件状态、重新做安全性计算 →
  重开本事件阀门（其他事件占用的阀门保持关闭）→ 事件置为 `restored` 记录 `restored_at` → 版本号 +1；
- 明确拒绝且**不产生部分变更**：
  版本过期 → `409 VERSION_CONFLICT`；事件不存在（或整网替换后旧事件）→ `404 EVENT_NOT_FOUND`；
  事件已复供 → `409 EVENT_ALREADY_RESTORED`；重新进水风险 → `422 RESTORE_UNSAFE`（issues 给出故障管段与仍可达端点）。

## 五、HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/network/import` | 整网导入（版本重置为 1，旧事件失效） |
| GET | `/api/network` | 读取当前管网与版本 |
| POST | `/api/isolation/preview` | 隔离方案预演（只读，计入待修复故障） |
| POST | `/api/isolation/submit` | 提交隔离（原子关阀 + 版本 +1，生成待修复事件） |
| POST | `/api/isolation/restore/preview` | 复供预演（只读：所关阀门、预计恢复用水点、阻断原因） |
| POST | `/api/isolation/restore/confirm` | 确认复供（原子重开阀门 + 事件置已复供 + 版本 +1） |
| GET | `/api/isolation/events` | 隔离操作历史（含 `pending_repair`/`restored` 状态） |

> 原有导入、隔离预演/提交、历史接口的请求/响应字段保持兼容（仅新增字段）；
> 隔离事件新增 `status`、`restored_at`、`restore_version`，隔离提交响应新增 `event_status`。

业务错误统一返回：

```json
{ "error": { "code": "NOT_ISOLATABLE", "message": "...", "issues": [ ... ] } }
```

## 六、调用示例

### 1. 导入管网

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d @examples/sample_network.json
```

示例管网（`examples/sample_network.json`）：

```
S(水源) --P1[阀]-- A(连接点) --P2[阀]-- B(连接点) --P4[阀]-- D(连接点) --P6(无阀,故障)-- U3(用水点)
                    |                  |
                  P3[阀]             P5[阀]
                    |                  |
                   U1(用水点)          U2(用水点)
```

响应：

```json
{ "version": 1, "node_count": 7, "pipe_count": 6 }
```

导入非法数据（如悬空端点）会返回 `422 IMPORT_INVALID`：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d '{"nodes":[{"id":"S","type":"source"}],"pipes":[{"id":"P1","from":"S","to":"X","has_valve":false}]}'
```

### 2. 读取版本

```bash
curl -s http://localhost:8000/api/network
```

### 3. 预演：只关 P5（不能隔离）

```bash
curl -s -X POST http://localhost:8000/api/isolation/preview \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P5"],"version":1}'
```

`D` 点仍可经 P4 进水，故障管段 P6 的 D 端仍可达，故 `isolatable=false`。

### 4. 预演：关 P4（可以隔离，仅 U3 失供）

```bash
curl -s -X POST http://localhost:8000/api/isolation/preview \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

响应：

```json
{
  "version": 1,
  "version_match": true,
  "fault_pipe_id": "P6",
  "close_pipe_ids": ["P4"],
  "isolatable": true,
  "affected_sink_ids": ["U3"],
  "issues": []
}
```

`version_match=false` 表示你读取的版本已过期（预演仍基于当前管网计算，但不应直接提交）。

### 5. 提交隔离

```bash
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

成功响应（P4 阀位原子置为关闭，版本变为 2）：

```json
{
  "version": 2,
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "event_id": 1
}
```

常见拒绝：

- 再次用 `"version":1` 提交 → `409 VERSION_CONFLICT`（版本过期，状态不变）；
- 取到版本 2 后再次关闭已关的 `P4` → `422 PLAN_INVALID` / `CLOSE_PIPE_DUPLICATE`；
- 关闭无阀管段 `P6` → `422 PLAN_INVALID` / `CLOSE_PIPE_NO_VALVE`；
- 提交不可隔离方案 → `422 NOT_ISOLATABLE`。

### 6. 隔离历史

```bash
curl -s http://localhost:8000/api/isolation/events
```

历史事件在原有字段基础上新增：`status`（`pending_repair`/`restored`）、
`restored_at`（复供时间）、`restore_version`（复供后的版本号）。

### 7. 复供预演与确认（本轮新增）

下面使用包含两条故障支路的演示管网 `examples/restore_network.json`：

```
S -P1[阀]- A -P2[阀]- B -P4[阀]- D -P6[阀]- U3
                         |            \
                       P5[阀]         P8[阀]- E -P9[阀]- U4
                         |
                        U2
```

导入并制造两个**待修复**事件（e1 关 P4 隔离 P6；e2 在 P4 已关后无需再关阀即可隔离 P9）：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d @examples/restore_network.json

curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
# -> event_id=1, version=2, event_status="pending_repair"

curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P9","close_pipe_ids":[],"version":2}'
# -> event_id=2, version=3
```

**先复供事件 1 不安全**（重开 P4 会让仍待修复的故障管段 P9 的 E 端重新进水）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/restore/preview \
  -H 'Content-Type: application/json' \
  -d '{"event_id":1,"version":3}'
```

响应（预演只读，阀门/版本/事件状态均不变）：

```json
{
  "version": 3,
  "version_match": true,
  "event_id": 1,
  "event_status": "pending_repair",
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "reopen_pipe_ids": ["P4"],
  "stay_closed_pipe_ids": [],
  "safe": false,
  "restored_sink_ids": ["U3"],
  "issues": [
    {
      "code": "RESTORE_UNSAFE",
      "message": "复供事件 1 后，待修复故障管段 P9 的端点 E 将重新从水源到达，存在重新进水风险",
      "pipe_id": "P9",
      "node_id": "E"
    }
  ]
}
```

此时确认会被拒绝且无部分变更：

```bash
# 版本过期 -> 409 VERSION_CONFLICT
curl -s -X POST http://localhost:8000/api/isolation/restore/confirm \
  -H 'Content-Type: application/json' \
  -d '{"event_id":1,"version":99}'

# 重新进水风险 -> 422 RESTORE_UNSAFE（P4 仍关、版本仍为 3、事件仍待修复）
curl -s -X POST http://localhost:8000/api/isolation/restore/confirm \
  -H 'Content-Type: application/json' \
  -d '{"event_id":1,"version":3}'
```

**按安全顺序复供**：先确认内层事件 2（无阀门可重开），再复供事件 1（重开 P4，U3、U4 同时恢复）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/restore/confirm \
  -H 'Content-Type: application/json' \
  -d '{"event_id":2,"version":3}'
# -> version=4, reopened_pipe_ids=[], restored_sink_ids=[]

curl -s -X POST http://localhost:8000/api/isolation/restore/confirm \
  -H 'Content-Type: application/json' \
  -d '{"event_id":1,"version":4}'
```

成功响应：

```json
{
  "version": 5,
  "event_id": 1,
  "event_status": "restored",
  "fault_pipe_id": "P6",
  "reopened_pipe_ids": ["P4"],
  "restored_sink_ids": ["U3", "U4"]
}
```

其他拒绝：

- 重复确认已复供事件 → `409 EVENT_ALREADY_RESTORED`；
- 确认不存在或整网替换前的旧事件编号 → `404 EVENT_NOT_FOUND`。

### 一键完整演示脚本（标准库实现，无需安装依赖）

```bash
# 隔离全流程（导入 -> 预演 -> 提交 -> 拒绝场景 -> 历史）
python3 examples/demo.py
# 复供全流程（两故障事件 -> 不安全预演/拒绝 -> 按序复供 -> 状态/历史）
python3 examples/demo_restore.py
# 或指定地址：
python3 examples/demo_restore.py http://localhost:8000
```

## 七、测试

核心图算法与校验逻辑（`tests/test_planning.py`，含隔离/待修复故障/复供用例）不依赖数据库与第三方库，可直接用标准库运行：

```bash
python3 -m unittest tests.test_planning -v
```

路由层集成测试（`tests/test_api.py`）用内存替身替换数据库，覆盖完整 HTTP 契约（含复供预演/确认、404/409/422 拒绝与无部分变更），需先安装依赖：

```bash
pip install -r requirements.txt httpx
python3 -m unittest discover -s tests -v
```

## 八、项目结构

```
app/
  config.py       # 环境变量配置
  schemas.py      # Pydantic 请求/响应模型
  planning.py     # 纯领域逻辑：导入校验、无向图可达性(BFS)、隔离与复供评估
  db.py           # PostgreSQL：DDL、快照、整网导入、事务化隔离提交/复供确认、历史
  serializers.py  # 响应序列化
  main.py         # FastAPI 路由与错误处理
examples/
  sample_network.json     # 隔离演示管网
  restore_network.json    # 复供演示管网（两条故障支路）
  demo.py                 # 隔离全流程示例
  demo_restore.py         # 复供全流程示例
tests/
  test_planning.py
  test_api.py
Dockerfile
docker-compose.yml
requirements.txt
```
