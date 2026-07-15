"""Independent ReActAgent reviewer for generated maintenance plans."""

from __future__ import annotations

import json
import os
from typing import Any

from agentscope.agent import ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg

from runtime import get_formatter, get_model_role_config, get_role_model
from services.json_utils import extract_json, get_response_text


EVALUATION_SYSTEM_PROMPT = """你是独立的检修方案质量评审员。你不参与方案生成，也不得读取或采用被评估产品 Skill 的自定义评分契约。

只根据三类事实评审：用户原始需求/结构化参数、候选检修方案正文、同产品同动作的高质量参考方案摘要。

重点判断五项：
1. 实施步骤能否让运维人员直接执行，是否写到入口、对象、参数、动作、预期结果和留痕。
2. 是否存在“根据实际情况”“按需处理”“确保正常”等无法执行的空话。
3. 是否凭空引入用户未提供且参考资料也无法证明的业务名称、实例、人员、账号、时间或参数。
4. 风险、验证和回滚是否针对本次操作并形成闭环。
5. 是否错误复制参考文档中的业务名称、实例、人员或其他个性化数据。

参考方案只能用于比较结构、步骤粒度、风险覆盖和格式，不得把其业务数据当成用户事实。
输出严格 JSON，不要输出分析过程或 Markdown。结构必须是：
{
  "dimension_scores": {
    "executability": 0-100,
    "empty_language": 0-100,
    "requirement_fidelity": 0-100,
    "risk_rollback_relevance": 0-100,
    "reference_leakage": 0-100
  },
  "findings": [
    {
      "severity": "high|medium|low",
      "category": "model_review",
      "message": "具体问题及候选文档证据",
      "suggested_skill_change": "可泛化到 Skill 的修改建议"
    }
  ],
  "summary": "评审结论"
}
不得因为没有发现问题而虚构问题。"""


async def evaluate_with_agent(
    *,
    candidate_text: str,
    state: dict[str, Any],
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run an isolated evaluator Agent and return its parsed JSON verdict."""
    model_name = os.getenv("QUALITY_EVALUATOR_MODEL_NAME") or get_model_role_config()["plan"]
    agent = ReActAgent(
        name="MaintenancePlanQualityReviewer",
        sys_prompt=EVALUATION_SYSTEM_PROMPT,
        model=get_role_model(
            model_name,
            max_tokens_env="QUALITY_EVALUATOR_MAX_TOKENS",
        ),
        formatter=get_formatter(),
        memory=InMemoryMemory(),
        max_iters=2,
    )
    agent.set_console_output_enabled(False)
    payload = {
        "user_requirements": state,
        "candidate_document": candidate_text[:32000],
        "reference_documents": references,
    }
    response = await agent(
        Msg(
            "user",
            "请评审以下候选检修方案，并严格输出约定 JSON：\n"
            + json.dumps(payload, ensure_ascii=False),
            "user",
        )
    )
    result = extract_json(get_response_text(response))
    return normalize_model_evaluation(result)


def normalize_model_evaluation(result: dict[str, Any]) -> dict[str, Any]:
    dimensions = result.get("dimension_scores") if isinstance(result.get("dimension_scores"), dict) else {}
    normalized_dimensions = {
        key: _score(dimensions.get(key, 0))
        for key in (
            "executability",
            "empty_language",
            "requirement_fidelity",
            "risk_rollback_relevance",
            "reference_leakage",
        )
    }
    findings = []
    for item in result.get("findings") or []:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        findings.append(
            {
                "severity": str(item.get("severity") or "medium").lower(),
                "category": "model_review",
                "message": message,
                "suggested_skill_change": str(item.get("suggested_skill_change") or "").strip(),
            }
        )
    return {
        "score": round(sum(normalized_dimensions.values()) / len(normalized_dimensions)),
        "dimension_scores": normalized_dimensions,
        "findings": findings,
        "summary": str(result.get("summary") or "").strip(),
    }


def _score(value: Any) -> int:
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return 0
