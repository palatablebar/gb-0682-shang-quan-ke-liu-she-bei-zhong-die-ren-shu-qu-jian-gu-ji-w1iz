# 深度登记 商圈客流设备重叠与人数区间估计

规模：中型。核心规则：无法识别个人时只输出满足区域交集约束的人数上下界；矛盾观测隔离而非伪造精确人数。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 设备采信 | 查看并复算设备采信 | `/api/m1_ingest` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py` |
| M2 区域映射 | 查看并复算区域映射 | `/api/m2_mapping` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 观测分桶 | 查看并复算观测分桶 | `/api/m3_versions` | `app/modules/m3_versions/rules.py` | `tests/test_versions.py` |
| M4 重叠约束 | 查看并复算重叠约束 | `/api/m4_segments` | `app/modules/m4_segments/rules.py` | `tests/test_segments.py` |
| M5 人数估界 | 查看并复算人数估界 | `/api/m5_calculate` | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py` |
| M6 日汇对账 | 查看并复算日汇对账 | `/api/m6_reconcile` | `app/modules/m6_reconcile/compare.py` | `tests/test_reconcile.py` |
| M7 报告归档 | 查看并复算报告归档 | `/api/m7_archive` | `app/modules/m7_archive/export_out.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 固定UTF-8失败 | 按编码链回退 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_legacy_encoding` |
| TRAP-02 | 表头BOM | 首列键失配 | 去除BOM | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom` |
| TRAP-03 | 引号内换行 | 分行误计 | csv逻辑记录 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_preserves_multiline_note` |
| TRAP-05 | 列数错位 | 静默补列 | WIDTH隔离 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_width_isolation` |
| TRAP-06 | 全角千分位 | float失败 | NFKC与Decimal | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_thousands` |
| TRAP-07 | 空值哨兵 | 空值当零 | MISSING隔离 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_missing_numeric` |
| TRAP-08 | 前导零ID | 编码失真 | 字符串保形 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_identifier_zeroes` |
| TRAP-10 | 浮点误差 | 累计失准 | Decimal求和 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_decimal_precision` |
| TRAP-14 | 同版冲突 | 最后覆盖 | 整组隔离 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_real_conflict_quarantine` |
| TRAP-18 | 三种空态 | 除零或伪成功 | 三个错误码 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_empty_three_states` |
| TRAP-26 | 旧版本复核 | 覆盖新结论 | 事务条件更新 | `app/store.py` | `tests/test_archive.py::test_stale_review` |
| TRAP-11 | 时区混用 | 同刻不同桶 | UTC归整 | `app/modules/m2_mapping/coerce.py` | `tests/test_versions.py::test_same_instant` |
| TRAP-19 | 三方约束矛盾 | 输出伪精确数 | 非负原子区域可行检查 | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py::test_three_way_contradiction` |
| TRAP-30 | 矛盾桶点名送修 | 单凭当次矛盾送修正常设备 | 整桶隔离，全部相关设备入同一张待检工单 | `app/work_orders.py` | `tests/test_work_orders.py::test_work_order_lists_every_device_not_single_blame` |
| TRAP-31 | 重试重复派单 | 同批重跑生成新工单 | 冲突指纹确定性身份，INSERT 幂等 | `app/work_orders.py`、`app/store.py` | `tests/test_work_orders.py::test_same_batch_retry_is_idempotent` |
| TRAP-32 | 补录全量重判 | 无关桶结论被搅动 | 按桶指纹只重判受影响桶，旧工单标记已解除并留痕 | `app/work_orders.py` | `tests/test_work_orders.py::test_rejudge_only_affected_buckets_after_supplement` |
| TRAP-33 | 交接覆盖丢记录 | 后到派单覆盖旧版 | 条件版本更新，409 时旧版派单保留、拒绝事件独立事务落库 | `app/store.py` | `tests/test_work_orders.py::test_dispatch_uses_conditional_version_and_keeps_old_on_stale` |

## 对账规则

输入行数=采纳+隔离+空行+重复表头+完全重复；业务总数与计算证据独立求和。隔离行保留来源行、ID、原因码与原值。

## 待检工单裁决

两两交集互相矛盾（m4 约束失败或 m5 无可行三方交集）时整桶隔离，不点名任何单台设备：同桶全部相关设备进入同一张待检工单，记录冲突依据（阶段、原因码、桶内全部证据与约束快照）与受影响区间（同结构设备组的相邻矛盾小时桶合并，间隔正常桶则拆单）。工单身份由设备结构签名与逐桶冲突指纹派生，同批重试指纹不变、不新增工单；补录后仅指纹变化的时间桶被重判，消失的旧工单转「已解除」并保留全部记录与派单历史。派单交接走 `dispatch_version` 条件更新，版本不匹配返回 409 且旧版派单继续有效，拒绝与交接事件只追加不覆盖。封存 CSV 以「待检工单」记录类型保留设备清单、冲突依据与受影响区间。

## 已知不覆盖

不做多租户权限和外部服务联调。行业规则以项目数据字典为准。文件上限2MB，逐行解析后在上限内完成分组计算。
