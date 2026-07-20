"""Structured, auditable AI screening for comment leads."""
from __future__ import annotations

import json
from typing import Any

from . import ai_report

SCREENING_KEYS = {"lead_id", "keep", "intent_level", "intent_tags", "summary", "reason", "confidence", "needs_human_review"}
OPPORTUNITY_KEYS = {"lead_id", "priority", "follow_up_channel", "recommended_action", "opening_message", "rationale", "risks", "confidence", "needs_human_review"}
KEEP_LEVELS = {"high", "medium", "low", "exclude"}
PRIORITIES = {"P1", "P2", "P3"}
CHANNELS = {"抖音私信", "人工查看主页", "暂不跟进"}


def _complete_json(messages: list[dict[str, str]]) -> str:
    return ai_report._chat_completion(ai_report.load_config(), messages, temperature=0.1, max_tokens=3000, response_format={"type": "json_object"})


def _parse(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("AI 未返回合法 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise ValueError("AI 返回缺少 results 数组")
    return value


def _valid_common(row: dict[str, Any], expected: set[str]) -> bool:
    return set(row) == expected and isinstance(row.get("lead_id"), str) and isinstance(row.get("confidence"), (int, float)) and 0 <= float(row["confidence"]) <= 1 and isinstance(row.get("needs_human_review"), bool)


def _screening(row: dict[str, Any], known: set[str]) -> dict[str, Any] | None:
    if isinstance(row, dict) and isinstance(row.get("confidence"), (int, float)) and row["confidence"] < .65:
        row["needs_human_review"] = True
    if not _valid_common(row, SCREENING_KEYS) or row["lead_id"] not in known or not isinstance(row.get("keep"), bool): return None
    if row.get("intent_level") not in KEEP_LEVELS or not isinstance(row.get("intent_tags"), list) or len(row["intent_tags"]) > 3: return None
    if (not row["keep"] and row["intent_level"] != "exclude") or (row["keep"] and row["intent_level"] == "exclude"): return None
    if not all(isinstance(tag, str) and len(tag) <= 8 for tag in row["intent_tags"]): return None
    if not all(isinstance(row.get(k), str) for k in ("summary", "reason")): return None
    if len(row["summary"]) > 40 or len(row["reason"]) > 80 or (row["confidence"] < .65 and not row["needs_human_review"]): return None
    return row


def _opportunity(row: dict[str, Any], known: set[str]) -> dict[str, Any] | None:
    if isinstance(row, dict) and isinstance(row.get("confidence"), (int, float)) and row["confidence"] < .65:
        row["needs_human_review"] = True
    if isinstance(row, dict) and isinstance(row.get("risks"), str):
        row["risks"] = [row["risks"]] if row["risks"].strip() else []
    if not _valid_common(row, OPPORTUNITY_KEYS) or row["lead_id"] not in known: return None
    if row.get("priority") not in PRIORITIES or row.get("follow_up_channel") not in CHANNELS: return None
    if not all(isinstance(row.get(k), str) for k in ("recommended_action", "opening_message", "rationale")): return None
    if not isinstance(row.get("risks"), list) or len(row["risks"]) > 3 or not all(isinstance(x, str) for x in row["risks"]): return None
    if len(row["opening_message"]) > 60 or (row["confidence"] < .65 and not row["needs_human_review"]): return None
    return row


def _input(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"lead_id": str(x.get("lead_id") or ""), "content": str(x.get("content") or ""), "create_time": x.get("create_time"), "comment_ip_location": str(x.get("comment_ip_location") or ""), "like_count": x.get("like_count") or 0, "reply_count": x.get("reply_count") or 0, "video": x.get("video_context") if isinstance(x.get("video_context"), dict) else {}} for x in leads]


def analyze_leads(leads: list[dict[str, Any]], *, business_context: dict[str, Any]) -> dict[str, Any]:
    """Run screening first, then opportunity only for retained rows."""
    source = [x for x in leads if isinstance(x, dict) and x.get("lead_id")]
    known = {str(x["lead_id"]) for x in source}
    errors: list[str] = []
    try:
        first = _parse(_complete_json([{"role": "system", "content": "你是获客虾筛选智能体。仅输出 JSON，不要 Markdown。必须返回 {schema_version:'1.0',results:[...]}; 每个结果只能有 lead_id,keep,intent_level,intent_tags,summary,reason,confidence,needs_human_review。intent_level 只能 high/medium/low/exclude，keep=false 时必须 exclude。教育、生活、通勤配套可作为潜在置业线索，但不得断言一定购房。"}, {"role": "user", "content": json.dumps({"business_context": business_context, "leads": _input(source)}, ensure_ascii=False)}]))
    except Exception as exc:  # noqa: BLE001
        return {"processed": 0, "errors": [str(exc)]}
    kept: list[dict[str, Any]] = []
    by_id = {str(x["lead_id"]): x for x in source}
    for row in first["results"]:
        valid = _screening(row, known) if isinstance(row, dict) else None
        if not valid: errors.append("筛选结果不符合 Schema"); continue
        target = by_id[valid["lead_id"]]; target.setdefault("ai", {})["screening"] = valid
        target["ai_label"] = "·".join(valid["intent_tags"]) or valid["intent_level"]
        if valid["keep"]: kept.append(target)
    if not kept: return {"processed": 0, "errors": errors}
    try:
        second = _parse(_complete_json([{"role": "system", "content": "你是获客虾机会智能体。仅输出 JSON，不要 Markdown。必须返回 {schema_version:'1.0',results:[...]}; 每个结果只能有 lead_id,priority,follow_up_channel,recommended_action,opening_message,rationale,risks,confidence,needs_human_review。priority 只能 P1/P2/P3；follow_up_channel 只能 抖音私信/人工查看主页/暂不跟进。不可承诺买房保证入学。"}, {"role": "user", "content": json.dumps({"business_context": business_context, "leads": _input(kept), "screening": [x["ai"]["screening"] for x in kept]}, ensure_ascii=False)}]))
    except Exception as exc:  # noqa: BLE001
        return {"processed": len(kept), "errors": errors + [str(exc)]}
    processed = 0
    kept_ids = {str(x["lead_id"]) for x in kept}
    for row in second["results"]:
        valid = _opportunity(row, kept_ids) if isinstance(row, dict) else None
        if not valid: errors.append("机会结果不符合 Schema"); continue
        by_id[valid["lead_id"]].setdefault("ai", {})["opportunity"] = valid; processed += 1
    return {"processed": processed, "errors": errors}


def analyze_stored_leads(lead_ids: list[str], *, business_context: dict[str, Any]) -> dict[str, Any]:
    from . import comment_leads
    requested = {str(value or "").strip() for value in lead_ids[:50] if str(value or "").strip()}
    if not requested:
        raise ValueError("请先选择 1 至 50 条评论")
    store = comment_leads.load_store()
    leads = [row for row in store.get("leads", []) if str(row.get("lead_id") or "") in requested]
    result = analyze_leads(leads, business_context=business_context)
    if result["processed"]:
        comment_leads.save_store(store)
    return {**result, "requested": len(requested)}
