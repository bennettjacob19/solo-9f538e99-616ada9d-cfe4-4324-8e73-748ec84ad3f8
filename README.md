# 本地服务发现目录（svcdir）

一个运行在 Flask + SQLite 上的小型服务发现目录：运维人员可以在网页上修改
「服务名 → 目标地址」的固定映射、登记带租期的临时实例、创建命名备份、并从一个
备份恢复。另有 `/resolve` 查询 API 供客户端使用。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

- 网页：<http://localhost:8000/>
- API：`GET http://localhost:8000/resolve?name=orders-api`
- 健康检查：`GET /healthz`
- SQLite 数据与备份都位于命名卷 `svcdir-data` 的 `/data/directory.sqlite3`
  （备份是同一数据库里的版本快照，所以备份与恢复天然原子）。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
SVCDIR_DB=./data/directory.sqlite3 python3 -m svcdir   # 开发用内置服务器
# 或： gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 8 svcdir.__main__:app
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SVCDIR_DB` | `data/directory.sqlite3` | SQLite 文件路径 |
| `SVCDIR_RESTORE_LEASE_SECONDS` | `30` | 恢复租约时长（进程异常死亡后的兜底回收上限） |
| `PORT` | `8000` | 仅用于 `python -m svcdir` |

## 页面与 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页：目录（含临时项及到期时间）、修改表单、试查、租约操作、备份列表、恢复按钮 |
| GET | `/resolve?name=<服务名>` | 返回单个服务的地址（固定项或有效临时项） |
| GET | `/resolve?name=<名>&token=<凭据>` | 同上，并附带该凭据对应租约的状态 |
| GET | `/resolve` | 返回整个当前目录（`services` 便于客户端，`entries` 带类型与到期时间） |
| GET | `/api/state` | 当前版本、完整目录、临时项、租约状态、恢复状态、备份列表 |
| GET | `/api/backups` | 备份列表 |
| POST | `/api/services` | `{"service": ..., "address": ..., "note": ...}` 新增/修改固定项 |
| DELETE | `/api/services/<名>` | 删除固定项 |
| POST | `/api/temporary` | `{"service": ..., "address": ..., "lease_seconds": 30-300}` 登记临时项，返回 `token`（省略租期默认 60 秒） |
| POST | `/api/temporary/<名>/renew` | `{"token": ..., "lease_seconds": 可选}` 续租 |
| DELETE | `/api/temporary/<名>` | `{"token": ...}` 或请求头 `X-Lease-Token`，撤销租约 |
| POST | `/api/backups` | `{"name": ...}` 创建命名备份（只含固定项） |
| GET | `/api/backups/<名>/validate` | 校验备份（不修改目录） |
| POST | `/api/backups/<名>/restore` | 从备份恢复 |

浏览器表单走 `POST /services`、`/temporary`、`/temporary/<名>/renew`、
`/temporary/<名>/revoke`、`/backups`、`/backups/<名>/restore`，成功后 303 跳回页面
并显示结果或错误。登记临时服务时，凭据只在跳回页面的提示中出现一次。

查询响应示例：

```json
{"ok": true, "version": 6, "restore_in_progress": false,
 "name": "worker-1", "address": "10.0.0.21:9000", "kind": "temporary",
 "lease_expires_at": "2026-11-18T21:04:49Z", "lease_expires_in": 119.982}
```

## 临时租约语义

- **登记与凭据。** 网页（或 `POST /api/temporary`）为当前未被占用的名称登记地址和
  30–300 秒租期，返回一次性可见的租约凭据。数据库只保存凭据的 SHA-256 摘要。
- **到期即退出。** 租期以绝对截止时间保存；`/resolve` 的单名查询和全目录查询都在
  查询时过滤 `deadline > now`，因此租期一到临时项立刻从结果中消失，无需等待任何
  清理任务。过期行的物理删除只是事后整理，可发生在任意时刻而不影响可见性。
- **重启后仍按原截止时间判定。** 截止时间存在 SQLite 里，进程重启（或容器重启）后
  仍以原始绝对时间为准，不会重新计时。
- **续租与撤销。** 只有持有该实例凭据的调用方能操作。续租把截止时间推后；撤销立即
  移除临时项。凭据可重复使用，直到租约到期或被撤销。
- **同名复用。** 租约到期（或撤销）后同名可重新登记，新实例得到新凭据；旧凭据在
  数据库中已不存在（或在名被复用时被替换），无法操作新实例，也不会删掉新实例。
- **固定项冲突。** 有效租约占用中的名称不能被登记为固定项（409 `name_in_use`），
  反之固定项存在时也不能登记同名临时项。已过期的租约不阻断固定项写入。
- **备份只含固定项。** 临时项是易变的租约状态，不进入版本链，因此命名备份天然只
  保存固定目录。
- **恢复与租约。** 恢复成功的那一次提交会原子地删除全部临时租约（包括仍有效的），
  旧凭据随之失效；恢复在校验阶段失败时目录与租约都保持原状。恢复租约（恢复进行中
  的标记）有效期间，登记/续租/撤销一律返回 409 `restore_in_progress`，与固定项修改
  一致。
- **一致性。** 租约表与目录版本在同一个 SQLite 数据库中；登记/续租/撤销是
  `BEGIN IMMEDIATE` 写事务，查询在单个读事务快照里同时读取固定目录与租约表。因此
  注册、续租、固定项修改与恢复交错时，每一次查询只看到一致的目录和租约状态。

## 一致性与恢复语义

实现要点（对应需求逐条）：

- **备份是同一提交点的完整目录。** 目录从不原地修改：每次修改都写入一个
  *完整的新版本*（先复制当前版本的全部行，再改一行），最后一条
  `UPDATE meta SET value=<新版本>` 才是发布。备份行只固定「版本号」，因此
  创建备份期间持续写入不会让备份变成半个目录。
- **恢复前校验。** 恢复分为三步：①在一个写事务里登记「恢复中」租约；②只读
  事务里校验备份（条目数、每行服务名/地址合法性、重复服务名、SHA-256 摘要与
  备份记录比对）；③一个原子写事务创建完整的新版本并切换 `current_version`。
  校验失败 → 直接返回 422，目录一个字节都不动。
- **恢复期间查询仍返回当前完整目录。** `current_version` 只在最后一步切换，
  查询永远读它；整个查询在读事务快照里完成，因此不会读到混排的数据。
- **新修改明确拒绝，已受理的修改先结算。** 登记恢复意图需要写锁，所有已受理
  的修改必然先提交完成；之后到达的修改看到有效租约，返回 `409
  restore_in_progress`（网页也会明确提示）。
- **恢复后新查询只见备份地址；先前受理的查询不混用两版。** 切换后新查询用新
  版本；一个在切换前开始的查询持有自己的读快照（`store.read_view()`），
  即使内部重复读取也仍是同一版本。
- **切换中断后重启只呈现原版或完整备份版。** 新版本与版本号切换在同一事务内
  提交；崩在提交前 → 仍是原版，崩在提交后 → 是完整备份版，旧版本的行永不删除。
  恢复者用租约 + 操作系统文件锁（`<db>.restore.lock`）标记存活：进程死亡时
  内核释放锁，重启后立即回收被遗弃的「恢复中」标记，不必等租约到期。
- **并发恢复。** 文件锁 + 数据库内的 `restore_state` 双重互斥，第二个恢复请求
  得到 409。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

关键交错场景覆盖在 `tests/test_svcdir.py`：

临时租约：

- `test_temporary_registration_resolves_until_expiry_without_cleanup`
- `test_restart_respects_original_deadline`
- `test_lease_bounds_are_enforced`
- `test_renew_and_revoke_require_matching_live_token`
- `test_expired_token_cannot_touch_new_registration_of_same_name`
- `test_registering_live_name_is_rejected` / `test_fixed_entry_cannot_take_name_with_live_lease`
- `test_backup_contains_only_fixed_entries`
- `test_successful_restore_revokes_all_temporary_leases` / `test_failed_restore_keeps_leases_and_directory`
- `test_lease_writes_rejected_during_restore`
- `test_concurrent_registrations_of_one_name_have_one_winner`
- `test_concurrent_lease_writes_and_queries_stay_consistent`
- `test_expired_lease_rows_are_only_housekeeping`
- HTTP：`test_api_temporary_lifecycle` / `test_api_temporary_validation_and_conflicts` /
  `test_api_state_reports_expiry_and_token_view` / `test_html_page_shows_leases_and_supports_operations` /
  `test_temporary_endpoints_reject_writes_during_restore`

目录版本与恢复：

- `test_writes_rejected_while_restore_in_progress_and_settled_before`
- `test_query_accepted_before_switch_keeps_one_version`
- `test_concurrent_writers_during_restore_never_mix`
- `test_cross_process_readers_and_writer_during_restore`（独立进程 + WAL 快照）
- `test_corrupt_backup_is_rejected_and_directory_unchanged`
- `test_backup_with_missing_rows_is_rejected`
- `test_interrupted_switch_presents_original_or_complete_backup`
- `test_killed_restorer_leaves_original_and_restart_can_retry`（SIGKILL 恢复进程）
- `test_expired_restore_lease_is_reclaimed_after_restart`
- `test_dead_restorer_is_reclaimed_immediately_on_restart`
- `test_restore_can_run_after_prior_abandoned_marker`
- `test_live_restore_lease_survives_sibling_process_initialization`
- `test_backup_row_pins_one_version_during_continuous_writes`
- `test_http_rejects_writes_and_serves_reads_during_restore`
- `test_api_flow` / `test_html_page_and_forms` / `test_html_escapes_hostile_names`
- `test_api_validation_errors_are_json` / `test_edge_case_inputs_are_rejected_cleanly`

## 布局

```
svcdir/errors.py     错误类型 → HTTP 状态码
svcdir/store.py      目录版本、临时租约、命名备份、校验、恢复（核心逻辑）
svcdir/web.py        Flask 路由：网页 + API
svcdir/templates/    页面模板
tests/test_svcdir.py 自测（含并发交错）
Dockerfile / docker-compose.yml
```

数据库 schema 版本为 2（新增 `temp_leases` 表）。v1 数据库在首次启动时原地迁移，
备份格式不变。

## 已知限制与未完成项

- **未在 Docker 中实际构建/运行。** 本环境没有 Docker（`docker` 命令不存在），
  因此 Dockerfile 与 compose 文件只做了静态检查。应用本身已用
  `python -m svcdir` 与 gunicorn 在真实 HTTP 上跑通。
- 租约凭据只在登记时显示一次；服务本身不保存明文，遗失后无法找回，只能等租约
  到期（或由运维从数据库删除对应行）。网页把凭据放在 URL 查询参数里回显页面状态，
  因此凭据会留在浏览器历史/访问日志中——只应在内网使用。
- 单容器部署，恢复互斥依赖同一文件系统上的 `flock`；若把数据目录放到不支持
  `flock` 的网络文件系统，会退化为「租约到期才算失效」。
- 备份保存在同一 SQLite 数据库中，可防止误改目录，但不能防磁盘/文件损坏；
  需要异地容灾应另行导出备份文件。
- 未做用户认证与 HTTPS——按需求这是本地运维工具，请只在内网使用。
- 恢复的「已受理查询」保证依赖单个查询请求只在一个读事务里读完（当前实现如此）；
  没有为客户端提供跨多个独立 HTTP 请求的版本固定参数。
