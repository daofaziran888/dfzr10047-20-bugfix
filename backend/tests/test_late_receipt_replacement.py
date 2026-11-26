"""题位回归：迟到失败回执不得写成“已确认但缺行”，新轮替代须核对原件证据。

完整复现题目场景：
- 接收方实际确认 3 行，另一笔尝试迟报缺 1 行；
- 旧台账一度同时是“已送达”和“缺失 1 行”；
- 值班员补交接收方三行证明后恢复原确认，原行数/目标/确认时间不被重写；
- 重解析工作台据此裁决新轮替代申请：待领办 ↔ 已领待目标校验；
- 只有核对通过的原件才能领成独立新轮，新轮失败可独立登记并重试。
"""
from __future__ import annotations

import pytest

from app.services import SERVICES
from app.store import store


@pytest.fixture(autouse=True)
def fresh_store():
    store.reload_seed()
    yield store
    store.reload_seed()


@pytest.fixture()
def deliver():
    return SERVICES["result_deliver"]


@pytest.fixture()
def reparse():
    return SERVICES["reparse_task"]


class TestLateFailureReceipt:
    def test_confirmed_then_late_failure_keeps_original_confirmation(self, deliver):
        deliver.create_deliver_record("RD-L1", "OC-003", "/data/out/l1/", 3, "csv")
        ok, _ = deliver.confirm_delivered("RD-L1", "2026-09-03T09:00:00")
        assert ok

        # 另一笔尝试迟报缺 1 行
        ok, msg = deliver.retry_missing("RD-L1", 1, "2026-09-03T10:00:00")
        assert ok is False  # 旧目标已确认，补投被拒绝
        assert "迟到" in msg

        row = deliver._find("RD-L1")
        assert row["status"] == "已送达"
        assert row["缺失行数"] == "0"
        assert row["投递行数"] == "3"
        assert row["目标位置"] == "/data/out/l1/"
        assert row["目标确认时间"] == "2026-09-03T09:00:00"
        assert row["投递时间"] == "2026-09-03T09:00:00"  # 未被迟到时间重写

        # 迟到失败可查，且是独立版本尝试
        late = deliver.late_receipts("RD-L1")
        assert len(late) == 1
        attempts = deliver.attempts_of("RD-L1")
        versions = [a["版本"] for a in attempts]
        assert len(set(versions)) == len(versions) >= 2

    def test_legacy_contradiction_repaired_by_three_row_proof(self, deliver):
        # seed RD-005：已送达 3 行但残留缺失 1 行的矛盾台账
        row = deliver._find("RD-005")
        assert row["status"] == "已送达"
        assert row["缺失行数"] == "1"
        assert deliver.contradiction_reason("RD-005")

        original_time = row["目标确认时间"]
        ok, msg = deliver.submit_confirmation_proof(
            "RD-005", confirmed_rows=3, credential="RCV-3ROWS",
            submit_time="2026-09-03T11:00:00")
        assert ok, msg

        row = deliver._find("RD-005")
        assert row["status"] == "已送达"
        assert row["缺失行数"] == "0"
        assert row["已确认行数"] == "3"
        assert row["投递行数"] == "3"
        assert row["目标位置"] == "/data/cooked/channel_a/reissue/20260903/"
        # 关键：保留原始确认时间，而不是补交时间
        assert row["目标确认时间"] == original_time == "2026-09-03T09:20:00"
        assert row["投递时间"] == original_time

    def test_same_credential_failure_receipt_checked_against_original_attempt(self, deliver):
        deliver.record_late_failure("RD-005", 1, "t-late", credential="DUP-RCV-1")
        # 目的地确认相同凭据：按原尝试核对（支持只传凭据全局反查）
        ok, msg, attempt = deliver.verify_attempt(credential="DUP-RCV-1")
        assert ok, msg
        assert attempt is not None
        assert "按原尝试" in attempt["核对状态"] or "核对" in attempt["核对状态"]
        # 旧确认没有被改成未送达
        assert deliver._find("RD-005")["status"] == "已送达"

    def test_confirmed_target_cannot_be_rewritten_by_supplementary_write(self, deliver):
        deliver.create_deliver_record("RD-L2", "OC-003", "/same/target/", 3, "csv")
        deliver.confirm_delivered("RD-L2", "t0")
        # 同目标新建投递被拒
        ok, _msg, rec = deliver.create_deliver_record(
            "RD-L3", "OC-003", "/same/target/", 9, "csv")
        assert ok is False and rec is None
        # 部分送达/回退同样被拒
        ok, _ = deliver.partial_deliver("RD-L2", 1, "t1")
        assert ok is False


class TestReparseReplacementWorkflow:
    def test_contradictory_ledger_blocks_replacement(self, reparse, deliver):
        reason = deliver.contradiction_reason("RD-005")
        assert reason
        ok, msg, _ = reparse.claim_as_new_round("RT-005", "NO-SUCH")
        assert ok is False

    def test_submit_disconnect_reentry_claim_verdict(self, reparse):
        ok, _msg, app = reparse.submit_replacement_application(
            "RT-005", "RA-E2E",
            original_deliver_no="RD-005", source_receipt_no="RD-005-A1",
            applicant="successor")
        assert ok and app

        # 断线重进：未领取 → 需要重新领办
        state = reparse.application_state("RT-005", "RA-E2E")
        assert state["需要重新领办"] is True
        assert state["重进判定"] == "需要重新领办"

        ok, _ = reparse.claim_replacement("RT-005", "RA-E2E", "worker-1")
        assert ok
        # 再次“断线重进”：状态持久，仍是已领待目标校验
        state = reparse.application_state("RA-E2E")  # 仅申请编号全局反查
        assert state["已领待目标校验"] is True
        assert state["重进判定"] == "已领待目标校验"

    def test_new_round_requires_verified_original_evidence(self, reparse, deliver):
        reparse.submit_replacement_application(
            "RT-005", "RA-GATE", original_deliver_no="RD-005")
        reparse.claim_replacement("RT-005", "RA-GATE")
        # 矛盾未消除前不能领为独立新轮
        ok, msg, _ = reparse.claim_as_new_round("RT-005", "RA-GATE")
        assert ok is False and "不能据此实施目标替换" in msg

        # 补交三行证明 → 资格放开
        ok, msg = reparse.verify_application_evidence(
            "RT-005", "RA-GATE", "RCV-PROOF-3", 3)
        assert ok, msg
        assert reparse.application_state("RT-005", "RA-GATE")["可领为新轮"] is True

        ok, msg, entry = reparse.claim_as_new_round(
            "RT-005", "RA-GATE", "RD-ROUND-2")
        assert ok, msg and entry
        new_row = deliver._find("RD-ROUND-2")
        assert new_row["轮次"] == "新轮"
        assert new_row["关联原投递记录"] == "RD-005"

        # 原 3 行确认与时间仍保留
        old = deliver._find("RD-005")
        assert old["投递行数"] == "3"
        assert old["目标确认时间"] == "2026-09-03T09:20:00"

    def test_independent_new_round_failure_and_retry(self, reparse, deliver):
        reparse.submit_replacement_application(
            "RT-005", "RA-NR", original_deliver_no="RD-005")
        reparse.claim_replacement("RT-005", "RA-NR")
        reparse.verify_application_evidence("RT-005", "RA-NR", "C", 3)
        reparse.claim_as_new_round("RT-005", "RA-NR", "RD-NR-X")

        ok, _ = reparse.register_new_round_failure(
            "RT-005", "RA-NR", missing_rows=3, reason="destination 500")
        assert ok
        assert deliver._find("RD-NR-X")["status"] == "投递失败"

        ok, _ = reparse.retry_new_round("RT-005", "RA-NR", still_missing=0)
        assert ok
        assert deliver._find("RD-NR-X")["status"] == "已送达"
        # 旧件时间不被新轮重试改写
        assert deliver._find("RD-005")["目标确认时间"] == "2026-09-03T09:20:00"

    def test_verified_scope_synced_to_reparse_task(self, reparse, deliver):
        deliver.submit_confirmation_proof("RD-005", 3, "RCV-SYNC")
        ok, _msg, scope = reparse.sync_verified_scope("RT-005")
        assert ok
        task = reparse._find("RT-005")
        assert any(s["投递记录编号"] == "RD-005" and s["verified"]
                   for s in task["已核实确认范围"])

    def test_complete_replace_strategy_blocked_by_contradiction(self, reparse):
        ok, msg, _ = reparse.complete_task("RT-005", 1, "尝试完成替换")
        assert ok is False
        assert "不能按「被新记录替代」完成" in msg


class TestPipelineManagerLateReceipt:
    def test_manager_late_missing_report_keeps_delivered_and_time(self):
        from app.pipeline.result_deliver_manager import ResultDeliverManager
        dm = ResultDeliverManager()
        dm.create_deliver("RD-M1", "OC-1", "/m1/", 3, "csv")
        dm.deliver("RD-M1", "t0")
        ok, _msg, rec = dm.retry_deliver("RD-M1", "t1", missing_rows=1)
        assert ok is True
        assert rec.delivered is True
        assert rec.deliver_time == "t0"
        assert "未送达" not in rec.conclusion or "不改变" in rec.conclusion
        assert len(dm.late_receipts("RD-M1")) == 1
