"""结果投递管理器：契约验证→目标写入→按尝试留存独立投递回执→重试管理。

核心行为：
- 只有通过输出契约校验的批次才触发投递
- 已确认送达的投递记录/目标不得被覆盖（重解析时的关键约束）
- 部分送达支持重试补投缺失行数
- 迟到的失败回执只写入“对应尝试”的独立回执并标记待核对，
  不得把已确认记录改写成“已送达但仍缺行”的矛盾状态，也不得覆盖原确认时间
"""
from __future__ import annotations

from typing import Any


class DeliverRecord:
    """单次投递台账（含每次尝试的独立回执）。"""

    def __init__(self, record_id: str, contract_id: str, target_path: str,
                 row_count: int, output_format: str, retry_count: int = 0):
        self.record_id = record_id
        self.contract_id = contract_id
        self.target_path = target_path
        self.row_count = row_count
        self.output_format = output_format
        self.retry_count = retry_count
        self.conclusion = ""
        self.delivered = False
        self.deliver_time = ""
        # 目标确认凭据与首次确认时间（一旦确认不再被迟到回执重写）
        self.confirmation_credential = ""
        self.confirmed_rows = 0
        self.confirmation_time = ""
        # 每次尝试一条独立回执，版本互不覆盖
        self.attempts: list[dict[str, Any]] = []

    def add_attempt(self, *, attempt_type: str, version: int | None = None,
                    missing_rows: int | None = None,
                    delivered_rows: int | None = None,
                    credential: str = "", receipt_time: str = "",
                    check_status: str = "待核对", note: str = "",
                    late: bool = False) -> dict[str, Any]:
        seq = version if version is not None else len(self.attempts) + 1
        attempt = {
            "attempt_no": f"{self.record_id}-A{seq}",
            "版本": f"v{seq}",
            "version": f"v{seq}",
            "seq": seq,
            "attempt_type": attempt_type,
            "missing_rows": missing_rows,
            "delivered_rows": delivered_rows,
            "credential": credential,
            "目标确认凭据": credential,
            "receipt_time": receipt_time,
            "check_status": check_status,
            "核对状态": check_status,
            "note": note,
            "late": late,
            "是否迟到": late,
        }
        self.attempts.append(attempt)
        return attempt

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "contract_id": self.contract_id,
            "target_path": self.target_path,
            "row_count": self.row_count,
            "output_format": self.output_format,
            "retry_count": self.retry_count,
            "conclusion": self.conclusion,
            "delivered": self.delivered,
            "deliver_time": self.deliver_time,
            "confirmation_credential": self.confirmation_credential,
            "confirmed_rows": self.confirmed_rows,
            "confirmation_time": self.confirmation_time,
            "attempts": [dict(a) for a in self.attempts],
        }


class ResultDeliverManager:
    """结果投递管理器。"""

    MAX_RETRIES = 3

    def __init__(self) -> None:
        self._records: dict[str, DeliverRecord] = {}
        self._delivered_set: set[str] = set()

    def create_deliver(self, record_id: str, contract_id: str,
                       target_path: str, row_count: int,
                       output_format: str) -> DeliverRecord:
        """创建投递记录。"""
        record = DeliverRecord(record_id, contract_id, target_path,
                               row_count, output_format, 0)
        self._records[record_id] = record
        return record

    def is_delivered(self, target_path: str) -> bool:
        """检查目标位置是否已有已确认送达的投递记录。"""
        return target_path in self._delivered_set

    def deliver(self, record_id: str, deliver_time: str,
                simulate_partial: int = 0,
                credential: str = "") -> tuple[bool, str, DeliverRecord | None]:
        """执行投递。

        simulate_partial > 0 模拟部分送达行为：表示未送达行数。
        """
        record = self._records.get(record_id)
        if record is None:
            return False, f"投递记录 {record_id} 不存在", None

        if self.is_delivered(record.target_path):
            record.conclusion = "目标位置已有送达记录，本次投递跳过"
            record.add_attempt(
                attempt_type="重复投递跳过", receipt_time=deliver_time,
                check_status="跳过-不覆盖已确认记录",
                note=record.conclusion)
            return True, record.conclusion, record

        if simulate_partial > 0:
            effective_rows = record.row_count - simulate_partial
            record.retry_count += 1
            if effective_rows <= 0:
                record.conclusion = f"投递失败:模拟写入0/{record.row_count}行(重试{record.retry_count})"
                record.add_attempt(
                    attempt_type="初始投递", missing_rows=simulate_partial,
                    delivered_rows=0, receipt_time=deliver_time,
                    check_status="投递失败", note=record.conclusion)
                return False, record.conclusion, record
            record.conclusion = f"部分送达:{effective_rows}/{record.row_count}行写入成功(重试{record.retry_count})"
            record.deliver_time = deliver_time
            record.add_attempt(
                attempt_type="初始投递", missing_rows=simulate_partial,
                delivered_rows=effective_rows, receipt_time=deliver_time,
                check_status="部分送达-待补投", note=record.conclusion)
            return True, record.conclusion, record

        record.delivered = True
        record.conclusion = f"成功投递:{record.row_count}行写入 {record.target_path}"
        record.deliver_time = deliver_time
        record.confirmation_time = deliver_time
        record.confirmed_rows = record.row_count
        record.confirmation_credential = credential
        self._delivered_set.add(record.target_path)
        record.add_attempt(
            attempt_type="初始投递", missing_rows=0,
            delivered_rows=record.row_count, credential=credential,
            receipt_time=deliver_time, check_status="已核对" if credential else "目标已确认-凭据待核对",
            note=record.conclusion)
        return True, record.conclusion, record

    def retry_deliver(self, record_id: str, deliver_time: str,
                      missing_rows: int,
                      credential: str = "") -> tuple[bool, str, DeliverRecord | None]:
        """重试投递缺失行。

        修复：若该记录/目标已经确认全量送达，迟到的缺失/失败回执只写入一次
        独立尝试（版本化），不再把 record.delivered 改成矛盾状态、不改结论与
        首次确认时间，也不会向旧目标再次补投写入。
        """
        record = self._records.get(record_id)
        if record is None:
            return False, f"投递记录 {record_id} 不存在", None

        already_confirmed = record.delivered or self.is_delivered(record.target_path)
        if already_confirmed:
            attempt = record.add_attempt(
                attempt_type="迟到失败回执" if missing_rows > 0 else "迟到回执",
                missing_rows=missing_rows,
                delivered_rows=max(0, record.row_count - missing_rows),
                credential=credential, receipt_time=deliver_time, late=True,
                check_status="迟到回执-按原尝试待核对",
                note=(f"另一笔尝试迟报缺失 {missing_rows} 行；原目标已确认 "
                      f"{record.row_count} 行，保留原确认且不补投写入"))
            if missing_rows == 0:
                attempt["check_status"] = "迟到回执-与全量确认一致"
                attempt["核对状态"] = attempt["check_status"]
            return True, (
                f"迟到回执 {attempt['attempt_no']} 已按独立尝试留存核对，"
                f"不改变已确认 {record.row_count} 行的结论"), record

        if record.retry_count >= self.MAX_RETRIES:
            record.conclusion = f"重试次数 {record.retry_count} 已达上限 {self.MAX_RETRIES}"
            record.add_attempt(
                attempt_type="重试被上限拦截", missing_rows=missing_rows,
                receipt_time=deliver_time, check_status="已达重试上限",
                note=record.conclusion)
            return False, record.conclusion, record

        record.retry_count += 1
        if missing_rows == 0:
            record.delivered = True
            self._delivered_set.add(record.target_path)
            record.conclusion = f"重试成功:全部{record.row_count}行已送达"
            record.deliver_time = deliver_time
            record.confirmation_time = deliver_time
            record.confirmed_rows = record.row_count
            record.add_attempt(
                attempt_type="重试补投", missing_rows=0,
                delivered_rows=record.row_count, credential=credential,
                receipt_time=deliver_time, check_status="已核对" if credential else "目标已确认",
                note=record.conclusion)
        else:
            record.conclusion = f"重试后仍有{missing_rows}行未送达(重试{record.retry_count})"
            record.deliver_time = deliver_time
            record.add_attempt(
                attempt_type="重试补投", missing_rows=missing_rows,
                delivered_rows=max(0, record.row_count - missing_rows),
                credential=credential, receipt_time=deliver_time,
                check_status="待核对", note=record.conclusion)

        return True, record.conclusion, record

    def verify_attempt_credential(self, record_id: str, attempt_no: str,
                                  credential: str = "",
                                  confirmed_rows: int | None = None,
                                  verify_time: str = "") -> tuple[bool, str]:
        """按目标确认凭据核对某次尝试；凭据证明全量时固化原确认，不覆盖原时间。"""
        record = self._records.get(record_id)
        if record is None:
            return False, f"投递记录 {record_id} 不存在"
        attempt = next((a for a in record.attempts
                        if a.get("attempt_no") == attempt_no), None)
        if attempt is None:
            return False, f"尝试 {attempt_no} 不存在"
        if credential:
            attempt["credential"] = credential
            attempt["目标确认凭据"] = credential
        if confirmed_rows is not None:
            attempt["目标确认行数"] = confirmed_rows
        if confirmed_rows is not None and confirmed_rows >= record.row_count:
            attempt["check_status"] = "核对一致-全量送达"
            attempt["核对状态"] = attempt["check_status"]
            record.delivered = True
            self._delivered_set.add(record.target_path)
            record.confirmed_rows = record.row_count
            if not record.confirmation_time:
                record.confirmation_time = verify_time or record.deliver_time
            # 关键：保留首次确认时间，不用核对时间重写。
            record.deliver_time = record.confirmation_time
            record.conclusion = f"凭据核对通过:全部{record.row_count}行已送达，原确认恢复"
        else:
            attempt["check_status"] = "已核对"
            attempt["核对状态"] = "已核对"
        return True, f"尝试 {attempt_no} 已核对"

    def submit_delivery_proof(self, record_id: str, confirmed_rows: int,
                              credential: str, proof_time: str = "") -> tuple[bool, str]:
        """补交接收方行数证明后恢复原确认（保留原行数/目标/首次确认时间）。"""
        record = self._records.get(record_id)
        if record is None:
            return False, f"投递记录 {record_id} 不存在"
        record.add_attempt(
            attempt_type="接收方证明补交", confirmed_rows=confirmed_rows,
            credential=credential, receipt_time=proof_time,
            check_status="已核对", note="补交接收方行数证明")
        if confirmed_rows >= record.row_count:
            record.delivered = True
            self._delivered_set.add(record.target_path)
            record.confirmed_rows = record.row_count
            if not record.confirmation_time:
                record.confirmation_time = proof_time
            record.deliver_time = record.confirmation_time
            record.conclusion = f"接收方证明全量{record.row_count}行，原确认恢复"
        return True, record.conclusion

    def late_receipts(self, record_id: str | None = None) -> list[dict[str, Any]]:
        """列出迟到失败回执，供详情页/工作台核查。"""
        out: list[dict[str, Any]] = []
        for rid, record in self._records.items():
            if record_id and rid != record_id:
                continue
            for attempt in record.attempts:
                if attempt.get("late"):
                    item = dict(attempt)
                    item["record_id"] = rid
                    out.append(item)
        return out

    def get_record(self, record_id: str) -> DeliverRecord | None:
        return self._records.get(record_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "records": {k: v.to_dict() for k, v in self._records.items()},
            "delivered_targets": sorted(self._delivered_set),
        }
