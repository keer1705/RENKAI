import logging
import json
import re
import os

from pydantic import Field
from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig
from nat.builder.framework_enum import LLMFrameworkEnum

logger = logging.getLogger(__name__)

BASE         = "S:/Renkai"
ROUTING_FILE = f"{BASE}/router/routing_decision.json"


class RouterFunctionConfig(FunctionBaseConfig, name="router"):
    llm_name: str = Field(default="nim_llm")
    description: str = Field(default="Routes user input to correct lane")


@register_function(config_type=RouterFunctionConfig)
async def router_function(config: RouterFunctionConfig, builder: Builder):

    async def _response_fn(input_message: str) -> str:

        llm = await builder.get_llm(
            llm_name=config.llm_name,
            wrapper_type=LLMFrameworkEnum.LANGCHAIN
        )

        prompt = f"""You are a routing agent. Classify the user request into exactly ONE lane.

LANE 1 — "deerflow_direct"  

Use this for ANYTHING that needs a text answer:
- Questions: "what is X", "explain X", "how does X work"
- Roadmaps: "give me a roadmap for X", "how do I learn X"
- Plans: "create a study plan", "what should I do to become X"
- Research: "compare X vs Y", "tell me about X"
- Guides: "how do I X", "what are the best ways to X"
- Advice: "should I X", "what do you think about X"
- Lists: "give me top 10 X", "what are examples of X"
- Definitions, explanations, summaries

KEY RULE: If the answer is TEXT/INFORMATION → deerflow_direct

LANE 2 — "build_pipeline"  

Use this ONLY when user wants actual working software:
- "build me X", "create X app", "make X program"
- "write a script that does X"
- "generate a X tool/agent/bot"
- "code a X", "develop X"

KEY RULE: Must contain explicit build intent words like:
build, create, make, generate, code, develop, write a script,
create an app, build an agent, make a program

NEVER use build_pipeline for:
- roadmaps, learning plans, guides (even if they say "create a plan")
- questions about how to do something
- research or explanation requests
- anything where the answer is words not code

ALWAYS use build_pipeline for:
- "simulate X" — simulations are code
- "implement X" — implementation is code
- "write me a X" — writing code
- "give me a script" — script is code



USER REQUEST: {input_message}

Respond ONLY with valid JSON:
{{
  "lane": "deerflow_direct" or "build_pipeline",
  "reason": "one sentence",
  "original_input": "{input_message.replace('"', "'")}"
}}
"""
        resp    = await llm.ainvoke(prompt)
        content = resp.content if hasattr(resp, "content") else str(resp)
        content = re.sub(r"```json|```", "", content).strip()

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
            except Exception:
                result = _fallback_route(input_message)
        else:
            result = _fallback_route(input_message)

        build_words = ["build", "create", "make", "generate", "code", "develop",
                       "write a script", "write a program", "build an agent",
                       "create an app", "make a tool", "simulate", "implement",
                       "design a program", "write me a", "give me a script",
                       "make me a", "create me a", "build me a"]
        has_build_intent = any(w in input_message.lower() for w in build_words)

        if result.get("lane") == "build_pipeline" and not has_build_intent:
            result["lane"]   = "deerflow_direct"
            result["reason"] = "No explicit build intent detected — routing to direct answer"
            logger.info(f"[Router] Safety override → deerflow_direct")

        logger.info(f"[Router] Lane: {result['lane']} | {result.get('reason','')}")

        os.makedirs(os.path.dirname(ROUTING_FILE), exist_ok=True)
        with open(ROUTING_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        return json.dumps(result)

    yield FunctionInfo.create(
        single_fn=_response_fn,
        description=config.description
    )


def _fallback_route(input_message: str) -> dict:
    build_words = ["build", "create", "make", "generate", "code", "develop",
                   "write a script", "write a program", "simulate", "implement",
                   "write me a", "give me a script", "make me a", "build me a"]
    lane = "build_pipeline" if any(w in input_message.lower() for w in build_words) \
           else "deerflow_direct"
    return {
        "lane":           lane,
        "reason":         "Fallback routing based on keyword detection",
        "original_input": input_message,
    }