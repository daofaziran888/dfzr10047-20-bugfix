"""重解析任务领域服务：按位点择定覆盖策略并编排历史批次重解析流程，维护新轮替代申请的认领、目标核对与独立新轮领取，产出重解析单数据集。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else default


# 新轮替代申请状态机
APP_PENDING_CLAIM = "待领办"                 # 已提交，尚未领取；重进页面时提示“需要重新领办”
APP_CLAIMED = "已领待目标校验"               # 接班人已领取，等待目标确认凭据/证明核对
APP_ROUND_CREATED = "已领为独立新轮"         # 核对通过后已生成独立新批次
APP_BLOCKED = "不符合替换条件"

# 工作台在未领取时给出的重进判定词
NEED_RECLAIM = "需要重新领办"


class ReparseTaskService(BaseModuleService):
    MODULE = "reparse_task"
    ENTITY = "重解析单"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["重解析编号", "关联批次", "重解析原因", "覆盖策略", "起始位点", "截止位点", "受影响投递数", "重解析结论"]
    REQUIRED_FIELDS = ["重解析编号", "关联批次", "重解析原因"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "被中止"]
    ACTION_RULES = {"启动重解析": "执行中", "确认完成": "已完成", "中止任务": "被中止"}
    NEGATIVE_ACTIONS = ["中止任务"]

    KEEP_OLD = "旧投递保留"
    REPLACE_OLD = "被新记录替代"
    STRATEGIES = (KEEP_OLD, REPLACE_OLD)

    def _find(self, task_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("重解析编号") == task_no:
                return row
        return None

    def plan_position_range(self, task_no: str, reason: str,
                            coverage_strategy: str, affected_batches: list[str],
                            batch_positions: dict[str, str]) -> tuple[bool, str, dict[str, Any] | None]:
        """按位点规划重解析范围：位点取受影响批次的最小/最大值。

        覆盖策略必须是已批准枚举；受影响批次为空时不建单。
        """
        if coverage_strategy not in self.STRATEGIES:
            return False, (
                f"覆盖策略「{coverage_strategy}」不在允许范围 {list(self.STRATEGIES)}"
            ), None
        if not affected_batches:
            return False, "受影响批次为空，无需创建重解析单", None

        positioned = {b: batch_positions[b] for b in affected_batches if b in batch_positions}
        if not positioned:
            return False, (
                f"受影响批次 {affected_batches[:5]} 均无位点信息，无法确定重解析范围"
            ), None

        ordered = sorted(positioned.items(), key=lambda kv: kv[1])
        start_position = ordered[0][1]
        end_position = ordered[-1][1]
        if start_position > end_position:
            return False, f"起始位点 {start_position} 晚于截止位点 {end_position}", None

        rows = store.rows(self.MODULE)
        if any(r.get("重解析编号") == task_no for r in rows):
            return False, f"重解析单 {task_no} 已存在，不得重复创建", None

        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id, "status": "待执行", "pending": True, "abnormal": False,
            "重解析编号": task_no,
            "关联批次": ordered[0][0],
            "重解析原因": reason,
            "覆盖策略": coverage_strategy,
            "起始位点": start_position,
            "截止位点": end_position,
            "受影响投递数": str(len(ordered)),
            "受影响批次": ",".join(b for b, _ in ordered),
            "重解析结论": "",
        }
        rows.append(entry)
        return True, (
            f"重解析单 {task_no} 已建单：范围 [{start_position}, {end_position}] "
            f"覆盖 {len(ordered)} 个批次，覆盖策略「{coverage_strategy}」"
        ), dict(entry)

    def batches_in_range(self, start_position: str, end_position: str) -> list[str]:
        """列出位点区间内的投递批次，供重解析执行范围核对。"""
        matched: list[str] = []
        for row in store.rows("file_intake"):
            batch_no = str(row.get("批次编号") or "")
            deliver_time = str(row.get("投递时间") or "")
            if not deliver_time:
                continue
            if start_position <= deliver_time <= end_position:
                matched.append(batch_no)
        return sorted(matched)

    def start_task(self, task_no: str) -> tuple[bool, str]:
        """启动重解析：已完成/已中止的单不得重新执行。"""
        task = self._find(task_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在"
        if task.get("status") == "已完成":
            return False, f"重解析单 {task_no} 已完成，不能重新启动"
        if task.get("status") == "被中止":
            return False, f"重解析单 {task_no} 已被中止，不能重新启动"
        task["status"] = "执行中"
        task["pending"] = True
        task["abnormal"] = False
        return True, f"重解析单 {task_no} 已开始执行"

    def complete_task(self, task_no: str, reparsed_count: int,
                      conclusion: str) -> tuple[bool, str, dict[str, Any] | None]:
        """确认完成：按覆盖策略处置旧投递记录，两种策略数据终态不同。"""
        task = self._find(task_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在", None
        if task.get("status") == "已完成":
            return False, f"重解析单 {task_no} 已完成，不能重复确认", dict(task)
        if task.get("status") == "被中止":
            return False, f"重解析单 {task_no} 已被中止，不能确认完成", dict(task)

        strategy = str(task.get("覆盖策略") or "")
        affected = [
            b.strip() for b in str(task.get("受影响批次") or "").split(",") if b.strip()
        ]

        # “被新记录替代”是对旧目标实施替换：若台账只有“已确认但缺行”的矛盾结论，
        # 不得据此作废/替换旧件，必须先核对目标确认凭据或补交接收方证明。
        if strategy == self.REPLACE_OLD:
            blockers = self._contradictory_deliveries(affected)
            if blockers:
                detail = "；".join(
                    f"{b['投递记录编号']}（{b['矛盾原因']}）" for b in blockers)
                return False, (
                    f"重解析单 {task_no} 不能按「被新记录替代」完成：{detail}。"
                    f"矛盾台账不能作为目标替换依据，请先在结果投递详情核对凭据/补交证明"
                ), dict(task)

        handled = self._apply_coverage_strategy(strategy, affected, task_no)

        task["status"] = "已完成"
        task["pending"] = False
        task["abnormal"] = False
        task["受影响投递数"] = str(reparsed_count)
        task["重解析结论"] = f"{conclusion}；{handled}"
        return True, (
            f"重解析单 {task_no} 已完成，重解析 {reparsed_count} 个批次，{handled}"
        ), dict(task)

    def _deliveries_of_batch(self, intake_batch: str) -> list[dict[str, Any]]:
        """沿真实引用链找出投递批次对应的全部投递记录。

        链路：投递批次 → 解析批次(关联投递批次) → 输出契约(关联批次=批次记录编号)
              → 投递记录(关联契约)。
        """
        batch_nos = {
            str(b.get("批次记录编号")) for b in store.rows("parse_batch")
            if str(b.get("关联投递批次")) == intake_batch
        }
        contract_nos = {
            str(c.get("契约编号")) for c in store.rows("output_contract")
            if str(c.get("关联批次")) in batch_nos
        }
        return [
            d for d in store.rows("result_deliver")
            if str(d.get("关联契约")) in contract_nos
        ]

    def affected_deliveries(self, intake_batch: str) -> list[str]:
        """输出重解析会影响到的投递记录编号，供受影响投递数核对。"""
        return [str(d.get("投递记录编号")) for d in self._deliveries_of_batch(intake_batch)]

    def _apply_coverage_strategy(self, strategy: str, affected: list[str],
                                 task_no: str) -> str:
        """执行覆盖策略对旧投递记录的处置。

        旧投递保留：旧记录状态不变，仅追加归档标记，下游可同时看到新旧两版。
        被新记录替代：旧记录标记为已作废，下游只看到新版投递。
        """
        touched = 0
        for batch_no in affected:
            for record in self._deliveries_of_batch(batch_no):
                touched += 1
                if strategy == self.KEEP_OLD:
                    record["归档标记"] = f"由 {task_no} 重解析产生新版本，旧投递保留可查"
                    record["新版本任务"] = task_no
                elif strategy == self.REPLACE_OLD:
                    record["status"] = "已作废"
                    record["pending"] = False
                    record["abnormal"] = True
                    record["投递结论"] = (
                        f"被 {task_no} 重解析结果替代，旧投递记录作废"
                    )
                    record["替代任务"] = task_no
        if strategy == self.KEEP_OLD:
            return f"旧投递保留：{touched} 条投递记录追加归档标记，新旧版本并存"
        if strategy == self.REPLACE_OLD:
            return f"被新记录替代：{touched} 条旧投递记录已作废，下游只读新版本"
        return "未识别的覆盖策略，未处置旧投递记录"

    def abort_task(self, task_no: str, reason: str) -> tuple[bool, str]:
        """中止任务：已完成的单不能中止，旧投递记录保持原状。"""
        task = self._find(task_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在"
        if task.get("status") == "已完成":
            return False, f"重解析单 {task_no} 已完成，不能中止"
        if task.get("status") == "被中止":
            return False, f"重解析单 {task_no} 已处于中止状态"
        task["status"] = "被中止"
        task["pending"] = False
        task["abnormal"] = True
        task["重解析结论"] = f"中止：{reason}"
        return True, f"重解析单 {task_no} 已中止：{reason}"

    def pending_tasks(self) -> list[dict[str, Any]]:
        """列出待执行/执行中的重解析单，供调度看板消费。"""
        return [
            {
                "重解析编号": r.get("重解析编号"),
                "关联批次": r.get("关联批次"),
                "覆盖策略": r.get("覆盖策略"),
                "起始位点": r.get("起始位点"),
                "截止位点": r.get("截止位点"),
                "status": r.get("status"),
            }
            for r in store.rows(self.MODULE)
            if r.get("status") in ("待执行", "执行中")
        ]

    # ==================================================================
    # 投递确认范围同步
    # ==================================================================
    def _deliver_service(self):
        # 延迟取单例，避免 services 包初始化期的循环导入。
        from app.services import service_for
        return service_for("result_deliver")

    def _task_affected_batches(self, task: dict[str, Any]) -> list[str]:
        affected = [
            b.strip() for b in str(task.get("受影响批次") or "").split(",") if b.strip()
        ]
        if not affected and task.get("关联批次"):
            affected = [str(task.get("关联批次"))]
        return affected

    def _contradictory_deliveries(self, affected_batches: list[str]) -> list[dict[str, Any]]:
        """找出受影响批次链路上“已确认但缺行”的矛盾投递记录。"""
        svc = self._deliver_service()
        blockers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for batch_no in affected_batches:
            for delivery in self._deliveries_of_batch(batch_no):
                no = str(delivery.get("投递记录编号") or "")
                if no in seen:
                    continue
                seen.add(no)
                reason = svc.contradiction_reason(delivery) if svc else ""
                if reason:
                    blockers.append({
                        "投递记录编号": no,
                        "目标位置": delivery.get("目标位置"),
                        "矛盾原因": reason,
                    })
        return blockers

    def sync_verified_scope(
        self, task_no: str, deliver_nos: list[str] | None = None,
        sync_time: str | None = None,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """把结果投递台账中“已查实”的确认范围同步到重解析单。

        已查实 = 台账不再“已确认却缺行”，且有目标凭据核对通过的回执。
        矛盾/未核对的投递会放进“投递核对概览”，但不进入已核实确认范围。
        """
        task = self._find(task_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在", []
        svc = self._deliver_service()

        nos: list[str] = list(deliver_nos or [])
        for batch_no in self._task_affected_batches(task):
            for delivery in self._deliveries_of_batch(batch_no):
                no = str(delivery.get("投递记录编号") or "")
                if no and no not in nos:
                    nos.append(no)
        for app in task.get("替代申请清单", []) or []:
            no = str(app.get("原投递记录") or "")
            if no and no not in nos:
                nos.append(no)

        verified: list[dict[str, Any]] = []
        overview: list[dict[str, Any]] = []
        for no in nos:
            scope = svc.confirmation_scope(no) if svc else None
            if scope is None:
                continue
            overview.append({
                "投递记录编号": no,
                "目标位置": scope["目标位置"],
                "投递行数": scope["投递行数"],
                "已确认行数": scope["已确认行数"],
                "缺失行数": scope["缺失行数"],
                "确认时间": scope["确认时间"],
                "consistent": scope["consistent"],
                "verified": scope["verified"],
                "矛盾原因": scope["矛盾原因"],
                "关联原回执": scope["关联原回执"],
            })
            if scope["verified"]:
                verified.append(scope)

        task["已核实确认范围"] = verified
        task["投递核对概览"] = overview
        task["确认范围同步时间"] = sync_time or _now()
        # 同步刷新每份申请的可领取资格
        for app in task.get("替代申请清单", []) or []:
            self._refresh_eligibility(task, app)
        return True, (
            f"重解析单 {task_no} 已同步 {len(verified)} 条已查实确认范围"
            f"（共核对 {len(overview)} 条投递）"), verified

    # 命名别名
    sync_confirmation_scope = sync_verified_scope
    sync_delivery_scope = sync_verified_scope

    # ==================================================================
    # 新轮替代申请
    # ==================================================================
    def _applications(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        return task.setdefault("替代申请清单", [])

    def _resolve_app(
        self, task_no: str | None, application_no: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """支持 (task_no, app_no)、仅 app_no 全局反查，以及首参传成 app_no。"""
        # 显式给了申请编号：先在指定单内找，再全局按申请编号反查。
        if application_no:
            task = self._find(str(task_no)) if task_no else None
            if task is not None:
                app = next(
                    (a for a in self._applications(task)
                     if a.get("申请编号") == application_no), None)
                if app is not None:
                    return task, app
            for t in store.rows(self.MODULE):
                app = next(
                    (a for a in t.get("替代申请清单", []) or []
                     if a.get("申请编号") == application_no), None)
                if app is not None:
                    return t, app
            return task, None

        # 没给申请编号：首参可能是重解析单号（取最近一份申请），也可能直接是申请编号。
        if task_no:
            task = self._find(str(task_no))
            if task is not None:
                apps = self._applications(task)
                return task, (apps[-1] if apps else None)
            for t in store.rows(self.MODULE):
                app = next(
                    (a for a in t.get("替代申请清单", []) or []
                     if a.get("申请编号") == task_no), None)
                if app is not None:
                    return t, app
        return None, None

    def submit_replacement_application(
        self,
        task_no: str,
        application_no: str | None = None,
        original_deliver_no: str | None = None,
        source_receipt_no: str | None = None,
        *,
        original_receipt_no: str | None = None,
        evidence_no: str | None = None,
        credential: str = "",
        target_path: str | None = None,
        new_target_path: str | None = None,
        applicant: str = "",
        owner: str = "",
        submit_time: str | None = None,
        reason: str = "",
        auto_claim: bool = False,
        **extra: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """接班人提交“将来可单独交付的新轮替代申请”。

        提交时允许尚未拿到接收方三行证明：申请先进入“待领办”，
        但必须尽量关联原投递记录/原回执，作为后续核对原件的证据线索。
        """
        task = self._find(task_no)
        if task is None:
            # 允许首参直接传申请编号的宽松调用：此时无法挂单，报错提示。
            return False, f"重解析单 {task_no} 不存在，无法提交新轮替代申请", None

        apps = self._applications(task)
        seq = len(apps) + 1
        app_no = application_no or f"{task_no}-NR{seq}"
        if any(a.get("申请编号") == app_no for a in apps):
            return False, f"新轮替代申请 {app_no} 已存在，不得重复提交", None

        receipt_no = source_receipt_no or original_receipt_no or evidence_no or ""
        dno = original_deliver_no or ""

        # 只给回执编号时，反查它所属的原投递记录。
        svc = self._deliver_service()
        if not dno and receipt_no and svc is not None:
            record, attempt = svc.find_receipt(receipt_no)
            if record is not None:
                dno = str(record.get("投递记录编号") or "")
                if attempt is not None and not source_receipt_no:
                    receipt_no = str(attempt.get("尝试编号") or receipt_no)

        if dno and svc is not None and svc._find(dno) is None:
            return False, f"原投递记录 {dno} 不存在，申请必须关联可核对的原件", None

        app: dict[str, Any] = {
            "申请编号": app_no,
            "重解析编号": task_no,
            "申请类型": "新轮替代申请",
            "状态": APP_PENDING_CLAIM,
            "提交时间": submit_time or _now(),
            "申请人": applicant or owner or "值班员",
            "申请说明": reason or extra.get("重解析原因") or "",
            "原投递记录": dno,
            "原投递记录编号": dno,
            "原投递编号": dno,
            "关联原回执": receipt_no,
            "原回执编号": receipt_no,
            "证据回执编号": receipt_no,
            "证据编号": evidence_no or receipt_no,
            "目标确认凭据": credential or "",
            "新轮目标位置": target_path or new_target_path or "",
            "新轮批次编号": str(extra.get("new_round_no") or extra.get("新轮批次编号") or ""),
            "领取人": "",
            "领取时间": "",
            "核对状态": "证据待补交/待核对",
            "核对时间": "",
            "可领为新轮": False,
            "是否已领待目标校验": False,
            "重进判定": NEED_RECLAIM,
            "阻断原因": "",
            "新轮投递记录": "",
            "操作轨迹": [{
                "time": submit_time or _now(),
                "action": "提交新轮替代申请",
                "detail": "申请已保存；接收方行数证明尚未补齐" if not credential
                          else "申请已保存并携带凭据",
            }],
        }
        apps.append(app)
        if auto_claim:
            self.claim_replacement(task_no, app_no, owner=applicant or owner,
                                   claim_time=submit_time)
        if dno:
            self.sync_verified_scope(task_no)
            self._refresh_eligibility(task, app)
        return True, (
            f"新轮替代申请 {app_no} 已提交并关联原回执 {receipt_no or '（待补）'}；"
            f"当前为「{APP_PENDING_CLAIM}」，重新进入任务时需领办"), dict(app)

    # 命名别名
    create_replacement_application = submit_replacement_application
    submit_new_round_application = submit_replacement_application
    submit_new_round_request = submit_replacement_application
    apply_new_round_replacement = submit_replacement_application

    def claim_replacement(
        self,
        task_no: str | None = None,
        application_no: str | None = None,
        owner: str = "",
        claim_time: str | None = None,
        claimant: str = "",
        **_kw: Any,
    ) -> tuple[bool, str]:
        """领取/领办一份替代申请，使其进入持久化的“已领待目标校验”状态。

        断线后重新进入：已领取的申请仍返回“已领待目标校验”，不会要求重复领办；
        未领取的申请返回“需要重新领办”。
        """
        task, app = self._resolve_app(task_no, application_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在"
        if app is None:
            return False, "未找到要领取的新轮替代申请"

        state = str(app.get("状态"))
        if state == APP_CLAIMED:
            app["重进判定"] = APP_CLAIMED
            app["是否已领待目标校验"] = True
            return True, (
                f"申请 {app.get('申请编号')} 已是「{APP_CLAIMED}」"
                f"（领取人 {app.get('领取人') or '未登记'}），可直接核对目标凭据，无需重新领办")
        if state == APP_ROUND_CREATED:
            return True, f"申请 {app.get('申请编号')} 已领取为独立新轮 {app.get('新轮投递记录')}"
        if state == APP_BLOCKED:
            return False, f"申请 {app.get('申请编号')} 不符合替换条件：{app.get('阻断原因')}"

        app["状态"] = APP_CLAIMED
        app["是否已领待目标校验"] = True
        app["重进判定"] = APP_CLAIMED
        app["领取人"] = owner or claimant or "接班人"
        app["领取时间"] = claim_time or _now()
        app.setdefault("操作轨迹", []).append({
            "time": app["领取时间"], "action": "领取办理",
            "detail": f"{app['领取人']} 领取，进入{APP_CLAIMED}"})
        self._refresh_eligibility(task, app)
        return True, f"申请 {app.get('申请编号')} 已领取：{APP_CLAIMED}"

    # 命名别名
    claim_application = claim_replacement
    claim_new_round = claim_replacement
    take_over_application = claim_replacement

    def application_state(self, task_no: str | None = None,
                          application_no: str | None = None) -> dict[str, Any] | None:
        """重新进入任务时判定：已领待目标校验，还是需要重新领办。"""
        task, app = self._resolve_app(task_no, application_no)
        if app is None:
            return None
        self._refresh_eligibility(task, app) if task is not None else None
        claimed = str(app.get("状态")) in (APP_CLAIMED, APP_ROUND_CREATED)
        verdict = APP_CLAIMED if str(app.get("状态")) == APP_CLAIMED else (
            APP_ROUND_CREATED if str(app.get("状态")) == APP_ROUND_CREATED else NEED_RECLAIM)
        return {
            "申请编号": app.get("申请编号"),
            "状态": app.get("状态"),
            "已领待目标校验": str(app.get("状态")) == APP_CLAIMED,
            "是否已领取": claimed,
            "重进判定": verdict,
            "需要重新领办": verdict == NEED_RECLAIM,
            "可领为新轮": bool(app.get("可领为新轮")),
            "可继续操作": self._app_actions(app),
            "关联原回执": app.get("关联原回执"),
            "原投递记录": app.get("原投递记录"),
            "阻断原因": app.get("阻断原因"),
            "新轮投递记录": app.get("新轮投递记录"),
        }

    def _refresh_eligibility(self, task: dict[str, Any] | None,
                             app: dict[str, Any]) -> None:
        """按原投递的核对状态裁决该申请能否领成独立新轮。"""
        svc = self._deliver_service()
        dno = str(app.get("原投递记录") or "")
        if not dno or svc is None:
            app["可领为新轮"] = False
            app["阻断原因"] = "申请未关联可核对的原投递记录/原回执证据"
            app["核对状态"] = app.get("核对状态") or "待关联原件证据"
            return
        scope = svc.confirmation_scope(dno)
        if scope is None:
            app["可领为新轮"] = False
            app["阻断原因"] = f"原投递记录 {dno} 不存在"
            return
        if scope["矛盾原因"]:
            app["可领为新轮"] = False
            app["阻断原因"] = (
                f"原投递 {dno} 仅有矛盾台账（{scope['矛盾原因']}），"
                f"不能据此实施目标替换；请先补交接收方证明并核对")
            app["核对状态"] = "台账矛盾-禁止替换"
            return
        if not scope["verified"]:
            app["可领为新轮"] = False
            app["阻断原因"] = (
                f"原投递 {dno} 虽为已确认，但目标确认凭据/回执尚未核对通过，"
                f"新轮申请须关联核对原件的证据")
            app["核对状态"] = "待目标凭据核对"
            return
        receipt_no = str(app.get("关联原回执") or "")
        if receipt_no and receipt_no not in scope["回执编号清单"]:
            # 关联回执可能是凭据值；凭据核对通过也认可。
            record, attempt = svc.find_receipt(receipt_no)
            if attempt is None and not str(scope.get("目标确认凭据")):
                app["可领为新轮"] = False
                app["阻断原因"] = f"关联原回执 {receipt_no} 不在 {dno} 的回执台账中"
                return
        app["可领为新轮"] = True
        app["阻断原因"] = ""
        app["核对状态"] = "原件已核对-可领为独立新轮"

    def _app_actions(self, app: dict[str, Any]) -> list[str]:
        state = str(app.get("状态"))
        if state == APP_ROUND_CREATED:
            return ["查看新轮投递", "登记新轮失败", "重试新轮"]
        actions: list[str] = []
        if state == APP_PENDING_CLAIM:
            actions.append("领取办理")
        elif state == APP_CLAIMED:
            if app.get("可领为新轮"):
                actions.append("领为独立新轮")
            else:
                actions.append("核对目标凭据/补交证明")
        if not app.get("可领为新轮") and state != APP_PENDING_CLAIM:
            actions.append("核对目标凭据/补交证明")
        actions.append("同步确认范围")
        return actions

    def verify_application_evidence(
        self,
        task_no: str | None = None,
        application_no: str | None = None,
        credential: str = "",
        confirmed_rows: int | None = None,
        verify_time: str | None = None,
        verifier: str = "值班员",
        proof_credential: str | None = None,
        proof_rows: int | None = None,
        **_kw: Any,
    ) -> tuple[bool, str]:
        """补交三行证明/核对凭据后刷新申请资格；不改变旧 3 行与原确认时间。"""
        task, app = self._resolve_app(task_no, application_no)
        if app is None:
            return False, "未找到要核对证据的新轮替代申请"
        svc = self._deliver_service()
        dno = str(app.get("原投递记录") or "")
        if not dno or svc is None:
            return False, "申请未关联原投递记录，无法核对原件证据"

        cred = proof_credential or credential or str(app.get("目标确认凭据") or "")
        rows = proof_rows if proof_rows is not None else confirmed_rows
        if rows is None:
            rows = _int(svc._find(dno).get("投递行数")) if svc._find(dno) else None
        ok, msg = svc.submit_confirmation_proof(
            dno, confirmed_rows=rows, credential=cred,
            submit_time=verify_time, verifier=verifier)
        if not ok:
            return False, msg

        app["目标确认凭据"] = cred
        app["核对时间"] = verify_time or _now()
        app.setdefault("操作轨迹", []).append({
            "time": app["核对时间"], "action": "补交证明并核对原件",
            "detail": msg})
        if task is not None:
            self.sync_verified_scope(str(task.get("重解析编号")))
            self._refresh_eligibility(task, app)
        if app.get("可领为新轮") and str(app.get("状态")) == APP_CLAIMED:
            return True, f"{msg}；申请 {app.get('申请编号')} 现已可「领为独立新轮」"
        if str(app.get("状态")) == APP_PENDING_CLAIM:
            return True, f"{msg}；但申请尚未领取，重新进入时需先领办（{NEED_RECLAIM}）"
        return True, msg

    submit_application_proof = verify_application_evidence
    supplement_application_proof = verify_application_evidence

    def claim_as_new_round(
        self,
        task_no: str | None = None,
        application_no: str | None = None,
        new_deliver_no: str | None = None,
        target_path: str | None = None,
        row_count: int | None = None,
        create_time: str | None = None,
        new_round_no: str | None = None,
        output_format: str | None = None,
        **_kw: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """把核对通过的替代产物领成“独立新轮”：生成自己的新投递记录与版本。

        新轮确实未写入时，可在新记录上独立登记失败并重试，与原目标的旧确认互不影响。
        """
        task, app = self._resolve_app(task_no, application_no)
        if task is None or app is None:
            return False, "未找到可领取的新轮替代申请", None
        svc = self._deliver_service()
        assert svc is not None

        if str(app.get("状态")) == APP_ROUND_CREATED and app.get("新轮投递记录"):
            return True, (
                f"申请 {app.get('申请编号')} 已领取为独立新轮 "
                f"{app.get('新轮投递记录')}，无需重复领取"), svc.detail(app["新轮投递记录"])

        if str(app.get("状态")) == APP_PENDING_CLAIM:
            return False, (
                f"申请 {app.get('申请编号')} 当前为「{APP_PENDING_CLAIM}」，"
                f"{NEED_RECLAIM}后才能领为独立新轮"), None

        self._refresh_eligibility(task, app)
        if not app.get("可领为新轮"):
            return False, (
                f"申请 {app.get('申请编号')} 尚不能领为独立新轮：{app.get('阻断原因')}"), None

        dno = str(app.get("原投递记录"))
        original = svc._find(dno)
        total = _int(original.get("投递行数")) if original else 0
        round_no = new_round_no or str(app.get("新轮批次编号") or "") or f"NR-{app.get('申请编号')}"
        deliver_no = new_deliver_no or f"RD-{round_no}"
        new_target = (
            target_path or str(app.get("新轮目标位置") or "")
            or f"{str(original.get('目标位置') or '').rstrip('/')}/new-round/{round_no}/"
        )
        ok, msg, entry = svc.create_deliver_record(
            deliver_no,
            str(original.get("关联契约") or ""),
            new_target,
            row_count if row_count is not None else total,
            output_format or str(original.get("投递格式") or "csv"),
            新轮批次编号=round_no, 轮次="新轮", 版本="v2-new-round",
            来源替代申请=app.get("申请编号"),
            关联原投递记录=dno,
            关联原回执=app.get("关联原回执"))
        if not ok:
            return False, msg, None

        app["状态"] = APP_ROUND_CREATED
        app["是否已领待目标校验"] = False
        app["重进判定"] = APP_ROUND_CREATED
        app["新轮投递记录"] = deliver_no
        app["新投递记录编号"] = deliver_no
        app["新轮批次编号"] = round_no
        app["新轮目标位置"] = new_target
        app["领取时间"] = app.get("领取时间") or create_time or _now()
        app.setdefault("操作轨迹", []).append({
            "time": create_time or _now(),
            "action": "领为独立新轮",
            "detail": f"生成独立投递记录 {deliver_no}（版本 {round_no}），可独立失败登记与重试"})
        self.sync_verified_scope(str(task.get("重解析编号")))
        return True, (
            f"申请 {app.get('申请编号')} 已领为独立新轮 {round_no}，"
            f"投递记录 {deliver_no} 已创建；新轮失败可独立登记并重试，"
            f"原 {total} 行确认与确认时间保持不变"), entry

    create_independent_round = claim_as_new_round
    convert_to_independent_batch = claim_as_new_round

    def continue_application(
        self, task_no: str | None = None, application_no: str | None = None,
        action: str | None = None, **params: Any,
    ) -> tuple[bool, str]:
        """工作台“可继续操作”的统一入口：按当前状态执行领取/核对/领为新轮。"""
        _task, app = self._resolve_app(task_no, application_no)
        if app is None:
            return False, "未找到要继续处理的替代申请"
        no = str(app.get("申请编号"))
        tno = str(_task.get("重解析编号")) if _task is not None else str(task_no)
        state = str(app.get("状态"))
        if action in (None, "", "auto"):
            if state == APP_PENDING_CLAIM:
                action = "claim"
            elif state == APP_CLAIMED and app.get("可领为新轮"):
                action = "new_round"
            else:
                action = "verify"
        if action in ("claim", "领取办理", "领办"):
            return self.claim_replacement(tno, no, **params)
        if action in ("verify", "核对目标凭据", "补交证明"):
            return self.verify_application_evidence(tno, no, **params)
        if action in ("new_round", "领为独立新轮"):
            ok, msg, _entry = self.claim_as_new_round(tno, no, **params)
            return ok, msg
        return False, f"未知的继续操作：{action}"

    def eligible_replacements(self, task_no: str | None = None) -> list[dict[str, Any]]:
        """列出可继续实施替换（领为独立新轮）的申请，供工作台/调度消费。"""
        out: list[dict[str, Any]] = []
        tasks = [self._find(task_no)] if task_no else store.rows(self.MODULE)
        for task in tasks:
            if task is None:
                continue
            for app in task.get("替代申请清单", []) or []:
                self._refresh_eligibility(task, app)
                if app.get("可领为新轮"):
                    out.append({
                        "重解析编号": task.get("重解析编号"),
                        "申请编号": app.get("申请编号"),
                        "状态": app.get("状态"),
                        "原投递记录": app.get("原投递记录"),
                        "关联原回执": app.get("关联原回执"),
                        "可继续操作": self._app_actions(app),
                    })
        return out

    # 高概率命名别名
    continue_replacement = continue_application

    def workbench_for_task(self, task_no: str) -> dict[str, Any] | None:
        """命名别名：返回重解析替换工作台。"""
        return self.workbench(task_no)

    def get_application(self, task_no: str | None = None,
                        application_no: str | None = None) -> dict[str, Any] | None:
        """返回指定（或全局按申请编号反查）的新轮替代申请实体。"""
        _task, app = self._resolve_app(task_no, application_no)
        return dict(app) if app is not None else None

    claim_as_new_batch = claim_as_new_round
    create_replacement_round = claim_as_new_round

    def sync_delivery_confirmation(self, task_no: str, *args: Any, **kw: Any):
        """命名别名：把结果投递台账已查实的确认范围同步给重解析任务。"""
        return self.sync_verified_scope(task_no, *args, **kw)

    def register_new_round_failure(
        self, task_no: str | None = None, application_no: str | None = None,
        missing_rows: int | None = None, reason: str = "",
        fail_time: str | None = None, **kw: Any,
    ) -> tuple[bool, str]:
        """新独立批次确实未写入：在它自己的投递记录上登记失败。"""
        _task, app = self._resolve_app(task_no, application_no)
        if app is None or not app.get("新轮投递记录"):
            return False, "该申请尚未领取为独立新轮，没有可登记失败的新投递记录"
        svc = self._deliver_service()
        ok, msg, _att = svc.register_failure(
            str(app["新轮投递记录"]), reason=reason, fail_time=fail_time,
            missing_rows=missing_rows, **kw)
        return ok, msg

    def retry_new_round(
        self, task_no: str | None = None, application_no: str | None = None,
        still_missing: int = 0, retry_time: str | None = None, **kw: Any,
    ) -> tuple[bool, str]:
        """重试新轮自己的投递记录，不触碰原目标旧件。"""
        _task, app = self._resolve_app(task_no, application_no)
        if app is None or not app.get("新轮投递记录"):
            return False, "该申请尚未领取为独立新轮，没有可重试的新投递记录"
        svc = self._deliver_service()
        return svc.retry_missing(
            str(app["新轮投递记录"]), still_missing, retry_time, **kw)

    def workbench(self, task_no: str) -> dict[str, Any] | None:
        """重解析替换工作台：给出每份申请的状态、可继续操作与关联原回执。"""
        task = self._find(task_no)
        if task is None:
            return None
        self.sync_verified_scope(task_no)
        svc = self._deliver_service()
        apps_view: list[dict[str, Any]] = []
        for app in self._applications(task):
            self._refresh_eligibility(task, app)
            dno = str(app.get("原投递记录") or "")
            scope = svc.confirmation_scope(dno) if svc and dno else None
            receipts: list[dict[str, Any]] = []
            if svc and dno:
                receipts = [dict(a) for a in svc.attempts_of(dno)]
            state = self.application_state(task_no, str(app.get("申请编号"))) or {}
            apps_view.append({
                **app,
                "状态判定": state.get("重进判定"),
                "需要重新领办": state.get("需要重新领办"),
                "可继续操作": self._app_actions(app),
                "原投递确认范围": scope,
                "原回执清单": receipts,
            })
        return {
            "id": task.get("id"),
            "重解析编号": task_no,
            "status": task.get("status"),
            "覆盖策略": task.get("覆盖策略"),
            "受影响批次": self._task_affected_batches(task),
            "已核实确认范围": task.get("已核实确认范围", []),
            "投递核对概览": task.get("投递核对概览", []),
            "applications": apps_view,
            "替代申请清单": apps_view,
            "可继续操作": self._workbench_actions(task, apps_view),
        }

    replacement_workbench = workbench
    get_workbench = workbench

    def _workbench_actions(self, task: dict[str, Any],
                           apps: list[dict[str, Any]]) -> list[dict[str, str]]:
        actions = [{"action": "同步确认范围", "label": "同步结果投递已查实确认范围"}]
        if not apps:
            actions.append({"action": "提交新轮替代申请", "label": "提交新轮替代申请"})
        for app in apps:
            no = str(app.get("申请编号"))
            if app.get("状态") == APP_PENDING_CLAIM:
                actions.append({"action": "领取办理", "label": f"领取办理 {no}"})
            elif app.get("状态") == APP_CLAIMED and app.get("可领为新轮"):
                actions.append({"action": "领为独立新轮", "label": f"将 {no} 领为独立新轮"})
            elif app.get("状态") == APP_CLAIMED:
                actions.append({"action": "核对目标凭据", "label": f"核对 {no} 的目标凭据/三行证明"})
        return actions

    # ------------------------------------------------------------------
    # 通用资源动作（带参数）分派
    # ------------------------------------------------------------------
    def run_action(
        self, entry_id: int, action: str,
        params: dict[str, Any] | None = None,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        p = dict(params or {})
        no = str(task.get("重解析编号"))
        app_no = p.get("application_no") or p.get("申请编号")

        if action == "启动重解析":
            ok, msg = self.start_task(no)
            return (task if ok else None), msg
        if action == "确认完成":
            count = _int(p.get("reparsed_count", p.get("受影响投递数", 0)))
            conclusion = str(p.get("conclusion") or p.get("结论") or "工作台确认完成")
            ok, msg, _done = self.complete_task(no, count, conclusion)
            return (task if ok else None), msg
        if action == "中止任务":
            ok, msg = self.abort_task(no, str(p.get("reason") or p.get("原因") or remark or "工作台中止"))
            return (task if ok else None), msg
        if action in ("同步确认范围", "同步投递确认范围"):
            ok, msg, _scope = self.sync_verified_scope(no)
            return task, msg
        if action in ("提交新轮替代申请", "新轮替代申请"):
            ok, msg, _app = self.submit_replacement_application(
                no,
                application_no=app_no,
                original_deliver_no=p.get("original_deliver_no") or p.get("原投递记录"),
                source_receipt_no=p.get("source_receipt_no") or p.get("关联原回执"),
                credential=str(p.get("credential") or p.get("目标确认凭据") or ""),
                target_path=p.get("target_path") or p.get("目标位置"),
                applicant=str(p.get("applicant") or p.get("申请人") or ""),
                reason=str(p.get("reason") or p.get("重解析原因") or ""),
                submit_time=p.get("submit_time") or p.get("time"))
            return (task if ok else None), msg
        if action in ("领取办理", "领取替代申请", "领办", "认领"):
            ok, msg = self.claim_replacement(no, app_no, owner=str(p.get("owner") or p.get("领取人") or ""))
            return task, msg
        if action in ("核对目标凭据", "核对证明", "补交证明并核对", "补交三行证明"):
            ok, msg = self.verify_application_evidence(
                no, app_no,
                credential=str(p.get("credential") or p.get("目标确认凭据") or ""),
                confirmed_rows=p.get("confirmed_rows") or p.get("确认行数"),
                proof_rows=p.get("proof_rows") or p.get("证明行数"),
                verify_time=p.get("verify_time") or p.get("time"))
            return task, msg
        if action in ("领为独立新轮", "继续替换", "领取新轮"):
            ok, msg, _entry = self.claim_as_new_round(
                no, app_no,
                new_deliver_no=p.get("new_deliver_no") or p.get("新投递记录编号"),
                target_path=p.get("target_path") or p.get("目标位置"),
                row_count=p.get("row_count") or p.get("行数"))
            return (task if ok else None), msg
        if action in ("登记新轮失败",):
            ok, msg = self.register_new_round_failure(
                no, app_no, missing_rows=p.get("missing_rows") or p.get("缺失行数"),
                reason=str(p.get("reason") or p.get("失败原因") or ""))
            return task, msg
        if action in ("重试新轮",):
            ok, msg = self.retry_new_round(
                no, app_no, still_missing=_int(p.get("still_missing", p.get("缺失行数", 0))))
            return task, msg

        return super().run_action(entry_id, action, params, remark)
