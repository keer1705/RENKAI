# deerflow_agent_function.py
# Fix: returns FULL answer, not truncated. Handles both lanes properly.

import logging
import json
import os
import httpx

from pydantic import Field
from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

logger = logging.getLogger(__name__)

BASE      = "S:/Renkai"
RESEARCH_OUTPUT = f"{BASE}/deerflow_agent/research_output.json"
ROUTING_FILE    = f"{BASE}/router/routing_decision.json"
DIRECT_OUTPUT   = f"{BASE}/deerflow_agent/direct_output.txt"  


class DeerflowAgentFunctionConfig(FunctionBaseConfig, name="deerflow_agent"):
    llm_name: str = Field(default="nim_llm")
    description: str = Field(default="Lane 1: answers directly. Lane 2: researches for planner.")


@register_function(config_type=DeerflowAgentFunctionConfig)
async def deerflow_agent_function(config: DeerflowAgentFunctionConfig, builder: Builder):

    async def _response_fn(input_message: str) -> str:
        lane, original_input = _read_routing(input_message)
        logger.info(f"[DeerFlow] Lane={lane} | Query={original_input[:80]}")

        result = await _call_deerflow_or_fallback(original_input, lane, builder, config)

        if lane == "deerflow_direct":
            os.makedirs(os.path.dirname(DIRECT_OUTPUT), exist_ok=True)
            with open(DIRECT_OUTPUT, "w", encoding="utf-8") as f:
                f.write(result)
            logger.info(f"[DeerFlow] Direct answer saved ({len(result)} chars)")
            return result

        os.makedirs(os.path.dirname(RESEARCH_OUTPUT), exist_ok=True)
        with open(RESEARCH_OUTPUT, "w", encoding="utf-8") as f:
            json.dump({
                "query":    original_input,
                "research": result,
                "lane":     "build_pipeline",
                "status":   "ready"
            }, f, indent=2, ensure_ascii=False)

        logger.info("[DeerFlow] Research saved → Planner ready ")
        return "RESEARCH_READY: DeerFlow complete. Planner can proceed."

    yield FunctionInfo.create(
        single_fn=_response_fn,
        description=config.description
    )


def _read_routing(fallback_input: str):
    try:
        with open(ROUTING_FILE, "r", encoding="utf-8") as f:
            r = json.load(f)
        return r.get("lane", "deerflow_direct"), r.get("original_input", fallback_input)
    except Exception:
        return "deerflow_direct", fallback_input


async def _call_deerflow_or_fallback(query: str, lane: str, builder, config) -> str:
    task = query if lane == "deerflow_direct" else (
        f"Research this software build request thoroughly: {query}. "
        "Return: best libraries, architecture patterns, implementation steps, key considerations."
    )

    # Try DeerFlow HTTP first
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            thread = await client.post("http://localhost:2025/threads", json={})
            tid    = thread.json()["thread_id"]
            run    = await client.post(
                f"http://localhost:2025/threads/{tid}/runs/wait",
                json={
                    "assistant_id": "bee7d354-5df5-5f26-a978-10ea053f620d",
                    "input": {"messages": [{"role": "user", "content": task}]},
                    "config": {"configurable": {"thread_id": tid}, "recursion_limit": 100}
                }
            )
            if run.status_code == 200:
                data = run.json()
                if "__error__" not in data:
                    for msg in reversed(data.get("messages", [])):
                        if msg.get("role") == "assistant" or msg.get("type") == "ai":
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = " ".join(c.get("text","") for c in content if isinstance(c,dict))
                            if content:
                                logger.info("[DeerFlow] HTTP response ")
                                return content
    except Exception as e:
        logger.warning(f"[DeerFlow] HTTP failed: {e}")

    # Fallback to NIM LLM
    logger.info("[DeerFlow] Falling back to NIM LLM")
    return await _nim_fallback(query, lane, builder, config)


async def _nim_fallback(query: str, lane: str, builder, config) -> str:
    llm = await builder.get_llm(
        llm_name=config.llm_name,
        wrapper_type=LLMFrameworkEnum.LANGCHAIN
    )

    if lane == "deerflow_direct":
        prompt = f"""Answer this request fully and thoroughly. Use markdown formatting.
Use ## for sections, ### for subsections, **bold** for key terms, bullet lists where appropriate.
Give a COMPLETE answer — do not truncate or summarize.

USER REQUEST: {query}

Provide the full detailed answer now:"""
    else:
        prompt = f"""Research this software build request and return structured findings.

BUILD REQUEST: {query}

Cover:
- Best Python libraries/frameworks to use
- Recommended architecture
- Key implementation steps  
- Important considerations and gotchas"""

    resp = await llm.ainvoke(prompt)
    return resp.content if hasattr(resp, "content") else str(resp)