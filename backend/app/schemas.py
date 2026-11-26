"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    # 允许调用方把参数直接放在请求体顶层（如 {"action": "...", "credential": "..."}），
    # 也支持统一放在 params 里，方便不同测试/脚本的调用习惯。
    model_config = ConfigDict(extra="allow")

    action: str
    remark: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    def merged_params(self) -> dict[str, Any]:
        extra = {
            key: value
            for key, value in self.model_dump().items()
            if key not in {"action", "remark", "params"} and value is not None
        }
        merged: dict[str, Any] = dict(extra)
        merged.update(self.params or {})
        if self.remark and "remark" not in merged:
            merged["remark"] = self.remark
        return merged
