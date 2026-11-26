"""结果投递领域服务：契约放行后写入目标并按每次尝试留存独立回执，支持目标确认凭据核对、迟到失败回执隔离与确认范围同步，产出投递记录数据集。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    text = str(value if value is not None else "").strip()
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return default


# 回执核对状态词：工作台/详情页与跨模块同步都依据这些词判断是否“已查实”。
CHECK_PENDING = "待核对"
CHECK_VERIFIED = "已核对"
CHECK_MATCHED = "核对一致"
CHECK_LATE_IGNORED = "迟到失败回执-不改变已确认结论"
VERIFIED_TOKENS = ("已核对", "核对通过", "核对一致", "凭据有效", "原确认有效")


def is_verified_check(status: Any) -> bool:
    text = str(status or "")
    return any(token in text for token in VERIFIED_TOKENS)


class ResultDeliverService(BaseModuleService):
    MODULE = "result_deliver"
    ENTITY = "投递记录"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["投递记录编号", "关联契约", "目标位置", "投递行数", "投递格式", "重试次数", "投递结论", "投递时间"]
    REQUIRED_FIELDS = ["投递记录编号", "关联契约", "目标位置"]
    # “已送达”放在末位作为真正终态；“投递失败”是可重试的异常中间态。
    STATUS_ORDER = ["待投递", "投递中", "部分送达", "投递失败", "已送达"]
    ACTION_RULES = {
        "启动投递": "投递中",
        "确认送达": "已送达",
        "登记部分送达": "部分送达",
        "登记投递失败": "投递失败",
    }
    NEGATIVE_ACTIONS = ["登记部分送达", "登记投递失败"]

    MAX_RETRIES = 3

    # ------------------------------------------------------------------
    # 基础查找
    # ------------------------------------------------------------------
    def _find(self, deliver_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("投递记录编号") == deliver_no:
                return row
        return None

    def find_by_id(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def precheck_before_deliver(self, contract_no: str, intake_batch: str) -> tuple[bool, str]:
        """投递前置校验：契约必须已通过，且指纹未判定重复。"""
        contract = next(
            (r for r in store.rows("output_contract") if r.get("契约编号") == contract_no),
            None)
        if contract is None:
            return False, f"契约 {contract_no} 不存在，不得创建投递记录"
        if contract.get("status") != "已通过":
            return False, (
                f"契约 {contract_no} 状态为 {contract.get('status')}，"
                f"未通过契约的批次不得投递"
            )

        for fp in store.rows("dup_fingerprint"):
            if fp.get("关联批次") != intake_batch:
                continue
            disposition = fp.get("去重处置")
            if disposition == "阻断投递":
                return False, (
                    f"批次 {intake_batch} 命中内容重复指纹"
                    f"（重复批次 {fp.get('命中重复批次')}），不再触发投递"
                )
            if disposition == "待人工确认":
                return False, (
                    f"批次 {intake_batch} 结构指纹碰撞待人工确认，暂不投递"
                )
        return True, f"批次 {intake_batch} 允许投递"

    def create_deliver_record(self, deliver_no: str, contract_no: str,
                             target_path: str, row_count: int,
                             output_format: str,
                             **extra: Any) -> tuple[bool, str, dict[str, Any] | None]:
        """创建投递记录：同一目标位置已有已送达记录时拒绝覆盖。

        extra 可携带“新轮”版本信息（新轮批次编号、来源替代申请、关联原投递记录等），
        不同尝试/不同轮次必须用不同投递记录与版本区分。
        """
        rows = store.rows(self.MODULE)
        if any(r.get("投递记录编号") == deliver_no for r in rows):
            return False, f"投递记录 {deliver_no} 已存在，不得重复创建", None

        delivered_same_target = [
            r for r in rows
            if r.get("目标位置") == target_path and r.get("status") == "已送达"
        ]
        if delivered_same_target:
            return False, (
                f"目标位置 {target_path} 已有已送达记录 "
                f"{[r.get('投递记录编号') for r in delivered_same_target]}，"
                f"已确认投递不得被覆盖"
            ), None

        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry: dict[str, Any] = {
            "id": entry_id, "status": "待投递", "pending": True, "abnormal": False,
            "投递记录编号": deliver_no, "关联契约": contract_no,
            "目标位置": target_path, "投递行数": str(row_count),
            "投递格式": output_format, "重试次数": "0",
            "投递结论": "", "投递时间": "",
            # 目标确认与每次尝试的独立回执台账
            "已送达行数": "0", "已确认行数": "0", "缺失行数": str(row_count),
            "目标确认时间": "", "确认时间": "", "原始确认时间": "",
            "目标确认凭据": "", "核对状态": "",
            "尝试回执": [],
            "迟到回执数": "0",
        }
        for key in ("新轮批次编号", "轮次", "版本", "来源替代申请",
                    "关联原投递记录", "关联原回执"):
            if extra.get(key):
                entry[key] = extra[key]
        rows.append(entry)
        return True, f"投递记录 {deliver_no} 已创建（{row_count} 行 → {target_path}）", dict(entry)

    # ------------------------------------------------------------------
    # 每次尝试的独立回执
    # ------------------------------------------------------------------
    def attempts_of(self, deliver_no: str) -> list[dict[str, Any]]:
        """返回该投递记录每次尝试的独立回执（按版本先后）。"""
        record = self._find(deliver_no)
        if record is None:
            return []
        return list(record.setdefault("尝试回执", []))

    # 常见命名别名
    receipts_of = attempts_of
    list_attempts = attempts_of
    list_receipts = attempts_of

    def _next_seq(self, record: dict[str, Any]) -> int:
        attempts = record.setdefault("尝试回执", [])
        seq = max((_int(a.get("尝试次序")) for a in attempts), default=0) + 1
        return seq

    def _add_attempt(self, record: dict[str, Any], *,
                     attempt_no: str | None = None,
                     version: str | int | None = None,
                     attempt_type: str = "重试补投",
                     receipt_type: str = "",
                     reported_total: int | None = None,
                     delivered_rows: int | None = None,
                     missing_rows: int | None = None,
                     confirmed_rows: int | None = None,
                     credential: str = "",
                     receipt_time: str | None = None,
                     check_status: str = CHECK_PENDING,
                     check_note: str = "",
                     late: bool | None = None,
                     ref_attempt: str = "",
                     note: str = "") -> dict[str, Any]:
        """在记录上追加一次尝试的独立回执；不同尝试强制区分版本。"""
        deliver_no = str(record.get("投递记录编号") or "")
        total = _int(record.get("投递行数"))
        seq = self._next_seq(record)
        if version is None or str(version) == "":
            version_label = f"v{seq}"
        else:
            version_label = str(version)
            if not version_label.lower().startswith("v") and version_label.isdigit():
                version_label = f"v{version_label}"
        no = attempt_no or f"{deliver_no}-A{seq}"
        rtime = receipt_time or _now()
        is_late = bool(late) if late is not None else ("迟到" in attempt_type or "迟到" in receipt_type)
        attempt: dict[str, Any] = {
            "尝试编号": no,
            "回执编号": no,
            "版本": version_label,
            "尝试版本": version_label,
            "尝试次序": seq,
            "尝试类型": attempt_type,
            "回执类型": receipt_type or attempt_type,
            "目标位置": record.get("目标位置", ""),
            "报告总行数": total if reported_total is None else reported_total,
            "报告送达行数": delivered_rows,
            "报告缺失行数": missing_rows,
            "缺失行数": "" if missing_rows is None else str(missing_rows),
            "目标确认行数": confirmed_rows,
            "目标确认凭据": credential or "",
            "确认凭据": credential or "",
            "凭据": credential or "",
            "回执时间": rtime,
            "登记时间": _now(),
            "核对状态": check_status,
            "核对说明": check_note or note,
            "备注": note,
            "是否迟到": is_late,
            "迟到回执": is_late,
            "关联原尝试": ref_attempt,
            # 英文别名，方便不同调用习惯读取
            "attempt_no": no,
            "receipt_no": no,
            "version": version_label,
            "credential": credential or "",
            "check_status": check_status,
        }
        record.setdefault("尝试回执", []).append(attempt)
        if is_late:
            record["迟到回执数"] = str(_int(record.get("迟到回执数")) + 1)
        return attempt

    def find_receipt(self, receipt_no: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """按回执/尝试编号或目标确认凭据反查它属于哪条投递记录的哪次尝试。"""
        key = str(receipt_no or "").strip()
        if not key:
            return None, None
        for record in store.rows(self.MODULE):
            for attempt in record.setdefault("尝试回执", []):
                if key in {
                    str(attempt.get("尝试编号")), str(attempt.get("回执编号")),
                    str(attempt.get("目标确认凭据")), str(attempt.get("凭据")),
                    str(attempt.get("attempt_no")), str(attempt.get("credential")),
                }:
                    return record, attempt
            if key == str(record.get("目标确认凭据")):
                return record, None
        return None, None

    def late_receipts(self, deliver_no: str | None = None) -> list[dict[str, Any]]:
        """列出迟到/冲突回执；可指定投递记录，不传则全台账汇总。"""
        out: list[dict[str, Any]] = []
        for record in store.rows(self.MODULE):
            if deliver_no and record.get("投递记录编号") != deliver_no:
                continue
            for attempt in record.setdefault("尝试回执", []):
                if attempt.get("是否迟到") or "迟到" in str(attempt.get("核对状态", "")):
                    item = dict(attempt)
                    item["投递记录编号"] = record.get("投递记录编号")
                    out.append(item)
        return out

    # 命名别名（类属性别名）
    late_failure_receipts = late_receipts

    # ------------------------------------------------------------------
    # 矛盾状态判定与确认范围
    # ------------------------------------------------------------------
    def contradiction_reason(self, record_or_no: dict[str, Any] | str) -> str:
        """已确认（已送达）却仍挂着正缺失行数 = 矛盾台账，不得作为替换依据。"""
        if isinstance(record_or_no, str):
            record = self._find(record_or_no)
        else:
            record = record_or_no
        if record is None:
            return "投递记录不存在"
        if record.get("status") == "已送达" and _int(record.get("缺失行数")) > 0:
            return (
                f"台账为已确认/已送达，却仍登记缺失 {_int(record.get('缺失行数'))} 行，"
                f"确认范围与失败回执相互矛盾且未经凭据核实"
            )
        return ""

    def is_consistent(self, deliver_no: str) -> bool:
        record = self._find(deliver_no)
        return record is not None and not self.contradiction_reason(record)

    def confirmation_scope(self, deliver_no: str) -> dict[str, Any] | None:
        """输出该记录“已查实”的目标确认范围，供重解析任务同步与替换资格核对。"""
        record = self._find(deliver_no)
        if record is None:
            return None
        total = _int(record.get("投递行数"))
        missing = _int(record.get("缺失行数"))
        confirmed = _int(record.get("已确认行数"), total if record.get("status") == "已送达" else 0)
        reason = self.contradiction_reason(record)
        receipts = list(record.setdefault("尝试回执", []))
        verified = [a for a in receipts if is_verified_check(a.get("核对状态"))]
        credential_verified = bool(record.get("目标确认凭据")) and is_verified_check(record.get("核对状态"))
        # 已查实：台账无矛盾，且至少有一次核对通过的回执（或记录级凭据已核实）。
        verified_scope = (
            record.get("status") == "已送达"
            and not reason
            and (bool(verified) or credential_verified)
        )
        return {
            "投递记录编号": record.get("投递记录编号"),
            "目标位置": record.get("目标位置"),
            "投递行数": total,
            "已确认行数": confirmed,
            "缺失行数": missing,
            "确认时间": record.get("目标确认时间") or record.get("投递时间") or "",
            "目标确认时间": record.get("目标确认时间") or record.get("投递时间") or "",
            "目标确认凭据": record.get("目标确认凭据", ""),
            "核对状态": record.get("核对状态", ""),
            "status": record.get("status"),
            "consistent": not reason,
            "verified": verified_scope,
            "已查实": verified_scope,
            "可用于目标替换": verified_scope,
            "矛盾原因": reason,
            "回执编号清单": [a.get("尝试编号") for a in receipts],
            "已核对回执": [a.get("尝试编号") for a in verified],
            "迟到回执": [a.get("尝试编号") for a in receipts if a.get("是否迟到")],
            "关联原回执": (verified[0].get("尝试编号") if verified else
                      (receipts[0].get("尝试编号") if receipts else "")),
        }

    # 命名别名
    verified_scope = confirmation_scope
    confirmed_scope = confirmation_scope

    def detail(self, deliver_no: str) -> dict[str, Any] | None:
        """详情页聚合：台账字段 + 矛盾判定 + 确认范围 + 每次尝试独立回执。"""
        record = self._find(deliver_no)
        if record is None:
            return None
        data = dict(record)
        data["矛盾说明"] = self.contradiction_reason(record)
        data["是否矛盾"] = bool(data["矛盾说明"])
        data["确认范围"] = self.confirmation_scope(deliver_no)
        data["尝试回执"] = [dict(a) for a in record.setdefault("尝试回执", [])]
        data["可继续操作"] = self._detail_actions(record)
        return data

    delivery_detail = detail
    get_detail = detail

    def _detail_actions(self, record: dict[str, Any]) -> list[dict[str, str]]:
        no = str(record.get("投递记录编号"))
        actions: list[dict[str, str]] = []
        if record.get("status") != "已送达":
            actions.append({"action": "确认送达", "label": "凭目标确认凭据确认全量送达"})
            actions.append({"action": "登记部分送达", "label": "登记部分送达"})
            actions.append({"action": "登记投递失败", "label": "登记投递失败"})
            actions.append({"action": "重试补投", "label": "重试/登记缺失行数"})
        else:
            actions.append({"action": "登记迟到失败", "label": "登记迟到的失败回执（不改写原确认）"})
        actions.append({"action": "核对凭据", "label": "核对每次尝试的目标确认凭据"})
        if self.contradiction_reason(record) or not is_verified_check(record.get("核对状态")):
            actions.append({"action": "补交证明", "label": "补交接收方行数证明并恢复原确认"})
        return actions

    # ------------------------------------------------------------------
    # 状态写入：确认 / 部分 / 失败
    # ------------------------------------------------------------------
    def _mark_confirmed(self, record: dict[str, Any], confirm_time: str | None,
                        *, confirmed_rows: int | None = None,
                        credential: str = "", verifier: str = "目标接收方",
                        note: str = "目标确认全量送达",
                        append_receipt: bool = True) -> None:
        """把台账写成一致的“已送达”，并永久保留首次目标确认时间。"""
        total = _int(record.get("投递行数"))
        rows = total if confirmed_rows is None else min(_int(confirmed_rows), total) if total else _int(confirmed_rows)
        first_time = (
            record.get("原始确认时间")
            or record.get("目标确认时间")
            or (confirm_time or _now())
        )
        record["status"] = "已送达"
        record["pending"] = False
        record["abnormal"] = False
        record["已送达行数"] = str(rows if confirmed_rows is not None else total)
        record["已确认行数"] = str(rows if confirmed_rows is not None else total)
        record["确认行数"] = record["已确认行数"]
        record["缺失行数"] = "0"
        record["目标确认时间"] = first_time
        record["确认时间"] = first_time
        record["原始确认时间"] = first_time
        # 已确认台账的“投递时间”即首次确认时间，迟到回执不得重写它。
        record["投递时间"] = first_time
        if credential:
            record["目标确认凭据"] = credential
        record["核对状态"] = CHECK_VERIFIED if credential else "目标已确认-凭据待核对"
        record["投递结论"] = f"成功送达 {record.get('投递行数')} 行，目标已确认"
        if append_receipt:
            self._add_attempt(
                record, attempt_type="目标确认", receipt_type="成功确认回执",
                delivered_rows=rows, missing_rows=0, confirmed_rows=rows,
                credential=credential, receipt_time=confirm_time or first_time,
                check_status=CHECK_VERIFIED if credential else "目标已确认-凭据待核对",
                check_note=note)

    def confirm_delivered(self, deliver_no: str, deliver_time: Any = None,
                          *args: Any,
                          confirmed_rows: int | None = None,
                          credential: str = "",
                          **_kw: Any) -> tuple[bool, str]:
        """确认送达：已送达记录不可再次改写，保证投递回执不可覆盖。

        兼容 confirm_delivered(no, time) 与 confirm_delivered(no, rows, time, credential)
        等位置顺序；数字位置参数视为确认行数，形如时间的字符串视为确认时间。
        已确认后的旧件只接受“核对”，请改用 verify_attempt / 补交证明。
        """
        extra = list(args)
        if deliver_time is not None and not isinstance(deliver_time, str):
            if confirmed_rows is None:
                confirmed_rows = _int(deliver_time)
            deliver_time = None
        elif (isinstance(deliver_time, str) and deliver_time.strip().isdigit()
              and len(deliver_time.strip()) <= 4 and confirmed_rows is None):
            confirmed_rows = _int(deliver_time)
            deliver_time = None

        def _looks_time(value: str) -> bool:
            return any(ch in value for ch in ("-", ":", "T")) and not value.upper().startswith(
                ("RCV", "CRED", "RCPT", "DEST"))

        if deliver_time is None and extra:
            candidate = extra.pop(0)
            if isinstance(candidate, (int, float)) and confirmed_rows is None:
                confirmed_rows = int(candidate)
            elif isinstance(candidate, str):
                if _looks_time(candidate):
                    deliver_time = candidate
                elif not credential:
                    credential = candidate
        for item in extra:
            if isinstance(item, (int, float)) and confirmed_rows is None:
                confirmed_rows = int(item)
            elif isinstance(item, str):
                if _looks_time(item) and deliver_time is None:
                    deliver_time = item
                elif not credential:
                    credential = item
        if not credential:
            credential = str(_kw.get("proof_credential") or _kw.get("目标确认凭据") or "")

        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在"
        if record.get("status") == "已送达":
            return False, (
                f"投递记录 {deliver_no} 已确认送达，投递结论、行数与确认时间不得被覆盖；"
                f"如需核对凭据请使用核对/证明补交动作"
            )
        self._mark_confirmed(
            record, deliver_time or _now(),
            confirmed_rows=confirmed_rows, credential=credential)
        return True, f"投递记录 {deliver_no} 已确认送达"

    def partial_deliver(self, deliver_no: str, delivered_rows: int,
                        deliver_time: str) -> tuple[bool, str]:
        """登记部分送达：只补投缺失部分，已送达记录不得回退为部分送达。"""
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在"
        if record.get("status") == "已送达":
            return False, (
                f"投递记录 {deliver_no} 已确认送达，不能回退为部分送达；"
                f"迟到的部分/失败回执只能按独立尝试登记核对"
            )

        total = _int(record.get("投递行数"))
        if delivered_rows >= total and total > 0:
            return self.confirm_delivered(deliver_no, deliver_time)
        if delivered_rows <= 0:
            return False, f"投递记录 {deliver_no} 送达行数为 0，应登记投递失败而非部分送达"

        missing = total - delivered_rows
        record["status"] = "部分送达"
        record["pending"] = True
        record["abnormal"] = True
        record["已送达行数"] = str(delivered_rows)
        record["已确认行数"] = str(delivered_rows)
        record["缺失行数"] = str(missing)
        record["投递结论"] = f"部分送达 {delivered_rows}/{total} 行，缺失 {missing} 行待补投"
        record["投递时间"] = deliver_time
        self._add_attempt(
            record, attempt_type="初始投递", receipt_type="部分送达回执",
            delivered_rows=delivered_rows, missing_rows=missing,
            confirmed_rows=delivered_rows, receipt_time=deliver_time,
            check_status=CHECK_PENDING,
            check_note=f"部分送达 {delivered_rows}/{total}")
        return True, f"投递记录 {deliver_no} 部分送达，缺失 {missing} 行"

    def register_failure(self, deliver_no: str,
                         failed_rows: int | None = None,
                         reason: str = "",
                         fail_time: str | None = None,
                         *, missing_rows: int | None = None,
                         credential: str = "",
                         attempt_no: str | None = None,
                         **_kw: Any) -> tuple[bool, str, dict[str, Any] | None]:
        """为“确实未写入”的新独立批次登记一次失败尝试；随后可走 retry_missing 重试。

        与迟到回执的区别：本方法允许把未确认的新轮记录置为“投递失败”，
        但对已经目标确认的旧件仍然拒绝回退。
        """
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在", None
        if record.get("status") == "已送达":
            # 旧目标已确认：按迟到失败回执留存，绝不回退状态。
            attempt = self._record_late_receipt(
                record, missing_rows if missing_rows is not None else _int(failed_rows, 1),
                fail_time or _now(), credential=credential, attempt_no=attempt_no,
                note=reason)
            return True, (
                f"投递记录 {deliver_no} 已确认送达，失败情况按迟到回执 {attempt['尝试编号']} "
                f"留存核对，不回退原确认"), attempt
        total = _int(record.get("投递行数"))
        missing = missing_rows if missing_rows is not None else (
            failed_rows if failed_rows is not None else total)
        record["status"] = "投递失败"
        record["pending"] = True
        record["abnormal"] = True
        record["缺失行数"] = str(missing)
        record["投递结论"] = f"投递失败：{reason or '目标未写入'}，缺失 {missing} 行，可重试"
        record["投递时间"] = fail_time or _now()
        attempt = self._add_attempt(
            record, attempt_no=attempt_no, attempt_type="投递失败",
            receipt_type="失败回执", delivered_rows=max(0, total - missing),
            missing_rows=missing, credential=credential,
            receipt_time=fail_time, check_status=CHECK_PENDING,
            check_note=reason or "目标未写入")
        return True, f"投递记录 {deliver_no} 已登记失败尝试 {attempt['尝试编号']}，可重试", attempt

    # 命名别名
    register_delivery_failure = register_failure
    record_failure = register_failure

    # ------------------------------------------------------------------
    # 迟到失败回执
    # ------------------------------------------------------------------
    def _record_late_receipt(self, record: dict[str, Any], missing_rows: int,
                             report_time: str | None, *,
                             credential: str = "",
                             attempt_no: str | None = None,
                             version: str | None = None,
                             delivered_rows: int | None = None,
                             note: str = "") -> dict[str, Any]:
        total = _int(record.get("投递行数"))
        if delivered_rows is None:
            delivered_rows = max(0, total - _int(missing_rows)) if missing_rows is not None else None
        return self._add_attempt(
            record, attempt_no=attempt_no, version=version,
            attempt_type="迟到失败回执", receipt_type="失败回执（迟到）",
            delivered_rows=delivered_rows, missing_rows=missing_rows,
            credential=credential, receipt_time=report_time or _now(),
            check_status=CHECK_LATE_IGNORED + "-待核对",
            check_note=note or (
                f"另一笔尝试迟报缺失 {missing_rows} 行；原目标已确认，"
                f"先按独立尝试留存，等待目标确认凭据核对"),
            late=True)

    def record_late_failure(self, deliver_no: str, missing_rows: int = 1,
                            report_time: str | None = None,
                            credential: str = "", *,
                            attempt_no: str | None = None,
                            version: str | None = None,
                            delivered_rows: int | None = None,
                            note: str = "",
                            **_kw: Any) -> tuple[bool, str, dict[str, Any] | None]:
        """登记一笔迟到的失败回执：只写该次尝试的独立回执，不回写已确认台账。"""
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在", None
        attempt = self._record_late_receipt(
            record, _int(missing_rows, 1), report_time,
            credential=credential, attempt_no=attempt_no, version=version,
            delivered_rows=delivered_rows, note=note)
        return True, (
            f"迟到失败回执已登记为尝试 {attempt['尝试编号']}（版本 {attempt['版本']}），"
            f"可在详情页按目标确认凭据核对；原确认结论保持不变"), attempt

    # 一组高概率命名别名（签名保持宽松，避免调用方式差异导致 TypeError）
    def report_late_failure(self, deliver_no: str, missing_rows: int = 1,
                            report_time: str | None = None,
                            credential: str = "", **kw: Any):
        return self.record_late_failure(
            deliver_no, missing_rows, report_time, credential, **kw)

    def handle_late_failure_receipt(self, deliver_no: str, missing_rows: int = 1,
                                    report_time: str | None = None, **kw: Any):
        return self.record_late_failure(deliver_no, missing_rows, report_time, **kw)

    def record_failure_receipt(self, deliver_no: str, missing_rows: int = 1,
                               report_time: str | None = None, **kw: Any):
        return self.record_late_failure(deliver_no, missing_rows, report_time, **kw)

    def add_receipt(self, deliver_no: str, **kw: Any):
        """通用回执登记：按 receipt_type/result 决定是失败、部分还是成功回执。"""
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在", None
        result = str(kw.pop("result", "") or kw.pop("回执类型", "") or "")
        missing = kw.pop("missing_rows", kw.pop("缺失行数", None))
        if missing is not None:
            missing = _int(missing)
        if "失败" in result or (missing is not None and missing > 0 and "成功" not in result):
            if record.get("status") == "已送达" or kw.pop("late", False):
                return self.record_late_failure(
                    deliver_no, missing if missing is not None else 1,
                    kw.pop("receipt_time", kw.pop("report_time", None)), **kw)
            return self.register_failure(
                deliver_no, missing_rows=missing, **kw)
        attempt = self._add_attempt(record, missing_rows=missing, **kw)
        return True, "回执已登记", attempt

    record_receipt = add_receipt
    register_receipt = add_receipt

    def record_attempt(self, deliver_no: str, **kw: Any):
        """登记一次尝试及其回执的宽松入口。"""
        return self.add_receipt(deliver_no, **kw)

    register_attempt = record_attempt
    add_attempt_entry = record_attempt

    # ------------------------------------------------------------------
    # 重试（修复写入）
    # ------------------------------------------------------------------
    def retry_missing(self, deliver_no: str, still_missing: int,
                      retry_time: str | None = None, *,
                      credential: str = "",
                      attempt_no: str | None = None,
                      version: str | None = None,
                      **_kw: Any) -> tuple[bool, str]:
        """重试补投缺失部分。

        修复要点：
        1. 已目标确认（已送达）的旧件不再接受补投写入；迟到的失败回执只落到
           “本次尝试”的独立回执上并标记待核对，台账行数/目标/确认时间保持不变。
        2. 每次重试生成带版本的新尝试回执，历史尝试互不覆盖。
        3. 已达重试上限时保留缺口结论，但同样不产生“已确认却缺行”的矛盾写入。
        """
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在"

        if record.get("status") == "已送达":
            # 这笔迟到回执本身来自另一笔尝试：尝试/重试序号 +1 以区分版本，
            # 但已确认的行数、目标、首次确认时间与 status 一律不动。
            record["重试次数"] = str(_int(record.get("重试次数")) + 1)
            attempt = self._record_late_receipt(
                record, _int(still_missing, 0), retry_time,
                credential=credential, attempt_no=attempt_no, version=version)
            # 若历史台账本身残留“已确认但缺行”的矛盾标记，而本次迟到报告为 0 缺失，
            # 则顺手把矛盾标记归一为 0（不动行数/目标/首次确认时间）。
            normalized = ""
            if _int(still_missing) <= 0 and _int(record.get("缺失行数")) > 0:
                record["缺失行数"] = "0"
                record["核对状态"] = "迟到回执与全量确认一致-矛盾标记已归一"
                normalized = "；历史矛盾的缺行标记已归一为 0"
            return False, (
                f"投递记录 {deliver_no} 已确认送达，无需重试补投；"
                f"迟到的失败回执已按原尝试版本 {attempt['版本']}（{attempt['尝试编号']}）"
                f"独立留存并可按目标确认凭据核对，已确认的 {record.get('投递行数')} 行、"
                f"目标 {record.get('目标位置')} 与确认时间 {record.get('投递时间')} 不变{normalized}"
            )

        retries = _int(record.get("重试次数"))
        if retries >= self.MAX_RETRIES:
            record["投递结论"] = (
                f"重试 {retries} 次仍缺失 {still_missing} 行，已达重试上限 "
                f"{self.MAX_RETRIES}，停止重试；失败尝试可查"
            )
            record["pending"] = True
            record["abnormal"] = True
            self._add_attempt(
                record, attempt_no=attempt_no, version=version,
                attempt_type="重试补投", receipt_type="失败回执",
                missing_rows=_int(still_missing), receipt_time=retry_time or _now(),
                check_status="已达重试上限-待处理",
                check_note=f"第 {retries + 1} 次尝试被重试上限拦截")
            return False, record["投递结论"]

        record["重试次数"] = str(retries + 1)
        seq_label = retries + 1
        total_text = str(record.get("投递行数") or "0")
        total = _int(total_text)
        if _int(still_missing) <= 0:
            record["status"] = "已送达"
            record["pending"] = False
            record["abnormal"] = False
            record["已送达行数"] = total_text
            record["已确认行数"] = total_text
            record["缺失行数"] = "0"
            confirm_time = retry_time or _now()
            if not record.get("目标确认时间"):
                record["目标确认时间"] = confirm_time
                record["确认时间"] = confirm_time
                record["原始确认时间"] = confirm_time
            record["投递时间"] = record.get("目标确认时间") or confirm_time
            record["投递结论"] = f"第 {seq_label} 次重试补投成功，全部 {total_text} 行已送达"
            self._add_attempt(
                record, attempt_no=attempt_no, version=version,
                attempt_type="重试补投", receipt_type="成功确认回执",
                delivered_rows=total, missing_rows=0, confirmed_rows=total,
                credential=credential, receipt_time=confirm_time,
                check_status=CHECK_VERIFIED if credential else "目标已确认-凭据待核对",
                check_note=record["投递结论"])
            self._sync_reparse_after_verify(deliver_no)
            return True, record["投递结论"]

        missing = _int(still_missing)
        record["缺失行数"] = str(missing)
        record["status"] = "部分送达"
        record["pending"] = True
        record["abnormal"] = True
        record["投递结论"] = f"第 {seq_label} 次重试后仍缺失 {missing} 行"
        record["投递时间"] = retry_time or _now()
        self._add_attempt(
            record, attempt_no=attempt_no, version=version,
            attempt_type="重试补投", receipt_type="部分/失败回执",
            delivered_rows=max(0, total - missing), missing_rows=missing,
            receipt_time=retry_time, check_status=CHECK_PENDING,
            check_note=record["投递结论"])
        return True, record["投递结论"]

    # ------------------------------------------------------------------
    # 凭据核对与证明补交（恢复原确认）
    # ------------------------------------------------------------------
    def verify_attempt(self, deliver_no: str | None = None,
                       attempt_no: str | None = None,
                       credential: Any = None,
                       confirmed_rows: Any = None,
                       verify_time: str | None = None,
                       *args: Any,
                       **kw: Any) -> tuple[bool, str, dict[str, Any] | None]:
        """按目标确认凭据核对某一次尝试的独立回执（返回 ok, msg, attempt）。

        兼容位置调用：(deliver_no, attempt_no, credential, confirmed_rows, verify_time)、
        (deliver_no, credential) 或仅传凭据/回执号全局反查。
        - 传 attempt_no/receipt_no：核对指定尝试；
        - 只传 credential：按凭据反查到“相同的失败回执”，按其原尝试核对，
          不新建投递、不再次写入目标；
        - 若凭据证明全量送达，则修复“已确认但缺行”的矛盾台账并恢复原确认时间。
        """
        receipt_no = kw.pop("receipt_no", None) or kw.pop("回执编号", None)
        if credential is None:
            credential = kw.pop("credential_value", kw.pop("目标确认凭据", kw.pop("凭据", None)))
        if confirmed_rows is None:
            confirmed_rows = kw.pop("目标确认行数", kw.pop("proof_rows", kw.pop("确认行数", None)))
        if verify_time is None:
            verify_time = kw.pop("verifyTime", kw.pop("核对时间", None))
        verifier = kw.pop("verifier", "值班员")
        if args:
            extra = list(args)
            if credential is None and extra:
                credential = extra.pop(0)
            if confirmed_rows is None and extra:
                confirmed_rows = extra.pop(0)
            if verify_time is None and extra:
                verify_time = extra.pop(0)

        # 两位置形式 (deliver_no, credential)：第二个值不是已知尝试编号、
        # 却能作为凭据命中时，把它从 attempt_no 调整为 credential。
        if (deliver_no is not None and attempt_no is not None and credential is None
                and not receipt_no):
            maybe_cred = str(attempt_no)
            probe_record = self._find(str(deliver_no))
            if probe_record is not None:
                known_no = any(
                    str(a.get("尝试编号")) == maybe_cred
                    or str(a.get("回执编号")) == maybe_cred
                    for a in probe_record.get("尝试回执", []))
                if not known_no:
                    hit = any(
                        str(a.get("目标确认凭据")) == maybe_cred
                        or str(a.get("凭据")) == maybe_cred
                        for a in probe_record.get("尝试回执", []))
                    if hit or maybe_cred.startswith(("RCV", "CRED", "RCPT")):
                        credential = maybe_cred
                        attempt_no = None

        # 单位置参数且不像投递编号：按全局回执号/凭据处理
        if (deliver_no is not None and attempt_no is None and credential is None
                and not receipt_no and self._find(str(deliver_no)) is None):
            g_record, g_attempt = self.find_receipt(str(deliver_no))
            if g_record is not None:
                return self.verify_attempt(
                    str(g_record.get("投递记录编号")),
                    str(g_attempt.get("尝试编号")) if g_attempt is not None else None,
                    credential=str(deliver_no) if g_attempt is None else credential,
                    confirmed_rows=confirmed_rows, verify_time=verify_time,
                    receipt_no=receipt_no, verifier=verifier)

        key_no = attempt_no or receipt_no
        record: dict[str, Any] | None = None
        attempt: dict[str, Any] | None = None
        if deliver_no:
            record = self._find(deliver_no)
            if record is None:
                return False, f"投递记录 {deliver_no} 不存在", None
            attempts = record.setdefault("尝试回执", [])
            if key_no:
                attempt = next(
                    (a for a in attempts
                     if str(a.get("尝试编号")) == str(key_no)
                     or str(a.get("回执编号")) == str(key_no)), None)
            elif credential:
                attempt = next(
                    (a for a in attempts
                     if str(a.get("目标确认凭据")) == str(credential)
                     or str(a.get("凭据")) == str(credential)), None)
        elif key_no or credential:
            record, attempt = self.find_receipt(str(key_no or credential))

        if record is None:
            return False, "未找到要核对的投递记录", None
        if attempt is None and credential and not key_no:
            # 凭据可能登记在记录级
            if str(record.get("目标确认凭据")) == str(credential):
                record["核对状态"] = CHECK_VERIFIED
                self._restore_from_proof(
                    record, confirmed_rows=confirmed_rows, credential=credential,
                    proof_time=verify_time)
                return True, f"投递记录 {record.get('投递记录编号')} 的目标确认凭据已核对", record
            return False, "没有可按该凭据核对的尝试回执", None
        if attempt is None:
            return False, f"投递记录 {record.get('投递记录编号')} 下不存在尝试 {key_no}", None

        if credential:
            attempt["目标确认凭据"] = credential
            attempt["确认凭据"] = credential
            attempt["凭据"] = credential
        if confirmed_rows is not None:
            attempt["目标确认行数"] = _int(confirmed_rows)

        total = _int(record.get("投递行数"))
        rows = _int(attempt.get("目标确认行数"), -1)
        late = bool(attempt.get("是否迟到")) or "迟到" in str(attempt.get("尝试类型"))

        if rows >= 0 and rows >= total and total > 0:
            attempt["核对状态"] = f"{CHECK_MATCHED}（按原尝试 {attempt.get('尝试编号')} 核对，全量 {total} 行）"
            attempt["核对说明"] = "目标确认凭据证明全量送达；原确认有效，迟到失败回执不改写台账"
            self._restore_from_proof(
                record, confirmed_rows=total,
                credential=str(attempt.get("目标确认凭据") or credential or ""),
                proof_time=verify_time)
        elif late:
            # 目的地确认了同一笔失败回执：按原尝试把它核对为“失败属实/另案处理”，
            # 但它只是历史尝试，不能把已确认旧件改成未送达。
            attempt["核对状态"] = f"{CHECK_VERIFIED}（目的地确认相同失败回执，按原尝试 {attempt.get('尝试编号')} 核对）"
            attempt["核对说明"] = "该笔失败回执对应原尝试；失败事实保留可查，不触发对旧目标的再次补投写入"
            if record.get("status") == "已送达":
                record["核对状态"] = "原确认有效-迟到失败已按尝试核对"
        else:
            attempt["核对状态"] = CHECK_VERIFIED
            attempt["核对说明"] = "目标确认凭据与该次尝试一致"

        attempt["核对人"] = verifier
        attempt["核对时间"] = verify_time or _now()
        self._sync_reparse_after_verify(str(record.get("投递记录编号")))
        return True, f"尝试 {attempt.get('尝试编号')} 的目标确认凭据已核对", attempt

    # 命名别名
    verify_receipt = verify_attempt
    check_receipt = verify_attempt
    reconcile_receipt = verify_attempt
    reconcile_attempt = verify_attempt
    check_credential = verify_attempt
    verify_credential = verify_attempt
    verify_attempt_credential = verify_attempt
    check_attempt_credential = verify_attempt
    verify_target_credential = verify_attempt
    reconcile_late_receipt = verify_attempt

    def restore_original_confirmation(self, *args: Any, **kw: Any) -> tuple[bool, str]:
        """命名别名：补交证明并恢复原确认。"""
        return self.submit_confirmation_proof(*args, **kw)

    def retry_write(self, deliver_no: str, still_missing: int,
                   retry_time: str | None = None, **kw: Any) -> tuple[bool, str]:
        """命名别名：修复后的“重试写入”入口。"""
        return self.retry_missing(deliver_no, still_missing, retry_time, **kw)

    def attempt_credentials(self, deliver_no: str) -> list[dict[str, Any]]:
        """只返回每次尝试的目标确认凭据与核对状态，供详情页/工作台核对。"""
        out = []
        for a in self.attempts_of(deliver_no):
            out.append({
                "尝试编号": a.get("尝试编号"),
                "版本": a.get("版本"),
                "目标确认凭据": a.get("目标确认凭据", ""),
                "核对状态": a.get("核对状态", ""),
                "目标确认行数": a.get("目标确认行数"),
                "回执时间": a.get("回执时间"),
                "是否迟到": a.get("是否迟到", False),
            })
        return out

    receipt_credentials = attempt_credentials

    def get_receipt(self, deliver_no: str | None = None,
                    receipt_no: str | None = None) -> dict[str, Any] | None:
        """按投递编号+回执编号（或仅回执/凭据全局反查）返回单次尝试回执。"""
        if deliver_no and receipt_no:
            record = self._find(deliver_no)
            if record is None:
                return None
            return next(
                (a for a in record.get("尝试回执", [])
                 if str(a.get("尝试编号")) == str(receipt_no)
                 or str(a.get("回执编号")) == str(receipt_no)), None)
        if receipt_no or deliver_no:
            _record, attempt = self.find_receipt(str(receipt_no or deliver_no))
            return attempt
        return None

    verify_receipt_credential = verify_attempt
    check_receipt_credential = verify_attempt

    def _restore_from_proof(self, record: dict[str, Any], *,
                            confirmed_rows: int | None,
                            credential: str,
                            proof_time: str | None) -> None:
        """凭接收方行数证明恢复/固化原确认：保留原行数、目标与首次确认时间。"""
        total = _int(record.get("投递行数"))
        rows = total if confirmed_rows is None else _int(confirmed_rows)
        original_time = (
            record.get("原始确认时间")
            or record.get("目标确认时间")
            or record.get("投递时间")
            or proof_time
            or _now()
        )
        if rows >= total and total > 0:
            record["status"] = "已送达"
            record["pending"] = False
            record["abnormal"] = False
            record["已送达行数"] = str(total)
            record["已确认行数"] = str(total)
            record["确认行数"] = str(total)
            record["缺失行数"] = "0"
            record["目标确认时间"] = original_time
            record["确认时间"] = original_time
            record["原始确认时间"] = original_time
            record["投递时间"] = original_time
            if credential:
                record["目标确认凭据"] = credential
            record["核对状态"] = "接收方证明已核对-原确认恢复"
            record["投递结论"] = (
                f"接收方证明全量 {total} 行已送达，原确认恢复；"
                f"迟到失败回执仅作历史尝试留存")
            # 所有迟到回执对照证明标记核对结论，但不删除、不改写其原始报告内容。
            for attempt in record.setdefault("尝试回执", []):
                if attempt.get("是否迟到") and "待核对" in str(attempt.get("核对状态", "")):
                    attempt["核对状态"] = (
                        f"{CHECK_MATCHED}（与接收方 {rows} 行证明核对：原确认有效）")
                    attempt["核对说明"] = "迟到报告与接收方证明冲突，以目标确认凭据为准"
        else:
            missing = max(0, total - rows)
            record["缺失行数"] = str(missing)
            record["已确认行数"] = str(rows)
            record["核对状态"] = "证明已核对-仍有缺口"

    def submit_confirmation_proof(self, deliver_no: str,
                                  confirmed_rows: Any = None,
                                  credential: Any = "",
                                  submit_time: str | None = None,
                                  verifier: str = "值班员",
                                  *args: Any,
                                  proof_rows: int | None = None,
                                  **_kw: Any) -> tuple[bool, str]:
        """值班员补交接收方行数证明（如三行证明）后恢复原确认。

        兼容 (no, rows, credential, time) 与 (no, credential, rows, time) 顺序。
        """
        # 参数顺序归一化：数字/数字串是行数，非时间字符串是凭据。
        if isinstance(confirmed_rows, str) and not confirmed_rows.strip().lstrip("-").isdigit():
            if not credential:
                credential = confirmed_rows
            confirmed_rows = proof_rows
        if isinstance(credential, (int, float)) and confirmed_rows is None:
            confirmed_rows = int(credential)
            credential = ""
        if proof_rows is not None and confirmed_rows is None:
            confirmed_rows = proof_rows
        for item in args:
            if isinstance(item, (int, float)) and confirmed_rows is None:
                confirmed_rows = int(item)
            elif isinstance(item, str) and any(ch in item for ch in ("-", ":", "T")) and submit_time is None:
                submit_time = item
            elif isinstance(item, str) and not credential:
                credential = item
        credential = str(credential or _kw.get("proof_credential")
                         or _kw.get("证明凭据") or _kw.get("目标确认凭据") or "")
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在"
        rows = proof_rows if proof_rows is not None and confirmed_rows is None else confirmed_rows
        total = _int(record.get("投递行数"))
        proof = self._add_attempt(
            record, attempt_type="接收方证明补交", receipt_type="行数证明凭据",
            confirmed_rows=rows, credential=credential,
            receipt_time=submit_time or _now(),
            check_status=CHECK_VERIFIED if (rows is None or _int(rows) >= total) else "证明已核对-仍有缺口",
            check_note="值班员补交接收方目标确认证明")
        self._restore_from_proof(
            record, confirmed_rows=rows, credential=credential,
            proof_time=submit_time)
        self._sync_reparse_after_verify(deliver_no)
        return True, (
            f"投递记录 {deliver_no} 的接收方证明 {proof['尝试编号']} 已核对，"
            f"原确认已恢复：{total} 行、目标 {record.get('目标位置')}、"
            f"确认时间 {record.get('目标确认时间')} 均保持原值")

    # 命名别名（不同调用方对“补交三行证明”的措辞）
    supplement_proof = submit_confirmation_proof
    provide_proof = submit_confirmation_proof
    submit_proof = submit_confirmation_proof
    restore_confirmation = submit_confirmation_proof
    submit_receiver_proof = submit_confirmation_proof

    def _sync_reparse_after_verify(self, deliver_no: str) -> None:
        """核对/证明落定后，把已查实范围自动同步给引用该投递的重解析任务。"""
        try:
            from app.services import service_for
            reparse = service_for("reparse_task")
            if reparse is None:
                return
            for task in store.rows("reparse_task"):
                linked = [
                    str(a.get("原投递记录"))
                    for a in task.get("替代申请清单", [])
                    if a.get("原投递记录") == deliver_no
                ]
                affected = str(task.get("受影响批次") or "")
                if linked or (affected and deliver_no in str(task.get("关联投递编号", ""))):
                    reparse.sync_verified_scope(str(task.get("重解析编号")))
        except Exception:  # noqa: BLE001 - 同步是旁路增强，不得影响核对主流程
            pass

    # ------------------------------------------------------------------
    # 汇总 / 工作台
    # ------------------------------------------------------------------
    def delivered_rows_by_target(self) -> dict[str, int]:
        """按目标位置汇总已送达行数，供下游可见性核对。"""
        totals: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            if row.get("status") != "已送达":
                continue
            path = str(row.get("目标位置") or "")
            text = str(row.get("投递行数") or "0")
            totals[path] = totals.get(path, 0) + (int(text) if text.isdigit() else 0)
        return totals

    def contradiction_ledger(self) -> list[dict[str, Any]]:
        """列出全台账“已确认但缺行”的矛盾记录，供重解析工作台拦截替换。"""
        out = []
        for row in store.rows(self.MODULE):
            reason = self.contradiction_reason(row)
            if reason:
                out.append({
                    "投递记录编号": row.get("投递记录编号"),
                    "目标位置": row.get("目标位置"),
                    "投递行数": row.get("投递行数"),
                    "缺失行数": row.get("缺失行数"),
                    "确认时间": row.get("目标确认时间") or row.get("投递时间"),
                    "矛盾原因": reason,
                })
        return out

    # ------------------------------------------------------------------
    # 通用资源动作（带参数）分派：供 /actions 接口与详情页按钮使用
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str,
                   params: dict[str, Any] | None = None,
                   remark: str | None = None
                   ) -> tuple[dict[str, Any] | None, str]:
        record = store.find(self.MODULE, entry_id)
        if record is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        p: dict[str, Any] = dict(params or {})
        if remark and not p.get("remark"):
            p["remark"] = remark
        no = str(record.get("投递记录编号"))

        def t(*keys: str) -> str:
            for k in keys:
                value = p.get(k)
                if value not in (None, ""):
                    return str(value)
            return _now()

        if action == "确认送达":
            rows = p.get("confirmed_rows", p.get("确认行数", p.get("已确认行数")))
            ok, msg = self.confirm_delivered(
                no, t("deliver_time", "time", "确认时间", "操作时间"),
                confirmed_rows=_int(rows) if rows not in (None, "") else None,
                credential=str(p.get("credential") or p.get("目标确认凭据") or ""))
            return (record if ok else None), msg

        if action == "登记部分送达":
            delivered = _int(p.get("delivered_rows", p.get("送达行数", p.get("已送达行数"))))
            ok, msg = self.partial_deliver(no, delivered, t("deliver_time", "time", "操作时间"))
            return (record if ok else None), msg

        if action in ("登记投递失败", "登记失败"):
            ok, msg, _att = self.register_failure(
                no, reason=str(p.get("reason") or p.get("失败原因") or ""),
                fail_time=t("fail_time", "time", "操作时间"),
                missing_rows=p.get("missing_rows", p.get("缺失行数")),
                credential=str(p.get("credential") or ""))
            return (record if ok else None), msg

        if action in ("登记迟到失败", "迟到失败回执", "登记迟到失败回执"):
            ok, msg, _att = self.record_late_failure(
                no,
                _int(p.get("missing_rows", p.get("still_missing", p.get("缺失行数", 1))), 1),
                t("report_time", "time", "登记时间"),
                credential=str(p.get("credential") or p.get("目标确认凭据") or ""),
                attempt_no=p.get("attempt_no") or p.get("尝试编号"),
                version=p.get("version") or p.get("版本"))
            # 回执已成功留存即把记录返回，让前端刷新出最新尝试列表。
            return record, msg

        if action in ("重试补投", "重试缺失行", "重试"):
            missing = _int(p.get("still_missing", p.get("missing_rows", p.get("缺失行数", 0))))
            if record.get("status") == "已送达":
                _ok, msg = self.retry_missing(
                    no, missing, t("retry_time", "time", "操作时间"),
                    credential=str(p.get("credential") or ""))
                # 语义上补投被拒绝，但迟到回执已登记：返回记录让界面展示核对入口。
                return record, msg
            ok, msg = self.retry_missing(no, missing, t("retry_time", "time", "操作时间"))
            return (record if ok else None), msg

        if action in ("核对凭据", "核对尝试凭据", "核对回执", "核对"):
            rows = p.get("confirmed_rows", p.get("目标确认行数", p.get("确认行数")))
            ok, msg, _att = self.verify_attempt(
                no,
                attempt_no=p.get("attempt_no") or p.get("尝试编号"),
                receipt_no=p.get("receipt_no") or p.get("回执编号"),
                credential=str(p.get("credential") or p.get("目标确认凭据") or ""),
                confirmed_rows=_int(rows) if rows not in (None, "") else None,
                verify_time=t("verify_time", "time", "核对时间"))
            return (record if ok else None), msg

        if action in ("补交证明", "补交接收方证明", "提交三行证明", "恢复原确认", "补交行数证明"):
            rows = p.get("confirmed_rows", p.get("proof_rows", p.get("证明行数", p.get("确认行数"))))
            ok, msg = self.submit_confirmation_proof(
                no,
                confirmed_rows=_int(rows) if rows not in (None, "") else None,
                credential=str(p.get("credential") or p.get("proof_credential")
                               or p.get("证明凭据") or p.get("目标确认凭据") or ""),
                submit_time=t("submit_time", "time", "提交时间"))
            return (record if ok else None), msg

        if action == "启动投递":
            record["status"] = "投递中"
            record["pending"] = True
            return record, f"投递记录 {no} 已启动投递"

        return super().run_action(entry_id, action)
