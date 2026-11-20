# 墓碑修复册（keyremap-bugfix-06）

**封册日期：2026-11-20，当天口径禁改口。**

## 一、正确性口径（二选一，甲乙互斥，不得混用）

| 路径 | 定义 | 本册择定 |
|------|------|----------|
| 甲（禁写） | 墓碑后重映**整笔拒绝**，正式零改，写**拒映审计** | ✅ **择定** |
| 乙（改键审计） | 允许改键，但必须**同事务追加改键世代快照**，失败则正式与快照一并回滚 | 不采用 |

- 择定理由：题面要求「切断墓碑暗写回」。乙路径仍允许改键，无法切断暗写，故取甲。
- **严禁**只改只读角标文案冒充修复（只改角标判失败）。
- 偏离本册口径的任何写入 → 拒写，并记「墓碑偏了」。

## 二、锁定与并发

- 墓碑写入与重映并发须**占旧键锁**；后到者只记「争用」，不再写正式区。
- 两路同时抢写须占锁，**后到只记争用**。

## 三、五处同态（须一致）

1. 墓碑表 `tombstones`（status=只读）
2. 正式实体 `formal_entities`
3. 拒映审计 `refuse_audit`
4. 互证页（前端 Tombstones / Refuse Audit 视图）
5. 映射册 `mappings` + `map_versions`（墓碑台账与正式键一致）

## 四、施工顺序（强制）

1. 复现：墓碑后仍改键 → 落**暗写失败测见红**。
2. 落**抢写争用失败测见红**。
3. 两条红测都红后，才可改墓碑相关写路径。
4. 全绿后：只读架构拒映 / 改键架构审计可查。

禁止跳过失败测；禁止无测直线改角标。

## 五、约束

- 仍用墓碑与重映接口，**不新建废弃台**。
- 账号照旧（admin / 123456）。
- 库文件保持本题路径 `data/app.db`；本地夹具，SQLite。

## 六、落地对照（2026-11-20）

| 修复册条款 | 落地位置 | 验证 |
|------------|----------|------|
| 甲路径：整笔拒绝 + 正式零改 + 拒映审计 | `backend/app.py:remap` | `test_tombstoned_key_remap_is_rejected_and_formal_untouched` |
| 旧键锁 + 后到只记争用 | `_acquire_key_lock` | `test_remap_while_old_key_lock_held_records_contention_only`、真实两路重叠压测 |
| 墓碑写入与重映共锁 | `create_tombstone` | `test_tombstone_write_and_remap_share_old_key_lock` |
| 偏离修复册拒写 + 记「墓碑偏了」 | `REPAIR_BOOK` / `tombstone_deviation` | `test_deviation_from_repair_book_is_refused_and_recorded` |
| 五处同态可查 | `/api/cross-check` + 前端「墓碑互证」页 | `test_cross_check_*`、端到端 |
