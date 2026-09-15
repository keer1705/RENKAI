import logging
import os
import json
import re

from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel, Field

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.component_ref import LLMRef
from nat.data_models.function import FunctionBaseConfig

logger = logging.getLogger(__name__)

BASE      = "S:/Renkai"
ROUTING_FILE = f"{BASE}/router/routing_decision.json"
RESEARCH_FILE= f"{BASE}/deerflow_agent/research_output.json"
PLAN_FILE    = f"{BASE}/planner/output.json"


class PlannerAgentConfig(FunctionBaseConfig, name="planner_agent"):
    llm_name: LLMRef = Field(description="LLM to use for planning")


class AgentRequirements(BaseModel):
    agent_type:       str
    primary_purpose:  str
    key_capabilities: list
    target_domain:    str


def extract_json(text: str) -> dict:
    text  = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found")
    raw = match.group(0)
    raw = re.sub(r',\s*}', '}', raw)
    raw = re.sub(r',\s*]', ']', raw)
    return json.loads(raw)


async def llm_json_call(llm, prompt: str, model: type):
    for _ in range(3):
        resp    = await llm.ainvoke(prompt)
        content = resp.content if hasattr(resp, "content") else str(resp)
        if content.strip():
            data = extract_json(content)
            return model(**data)
    raise ValueError("LLM returned empty response")


def get_routing_lane() -> str:
    try:
        with open(ROUTING_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("lane", "build_pipeline")
    except Exception:
        return "build_pipeline"


def load_research_context() -> str:
    try:
        with open(RESEARCH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("status") == "ready":
            return data.get("research", "")
    except Exception:
        pass
    return ""


@register_function(config_type=PlannerAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def planner_agent(tool_config: PlannerAgentConfig, builder: Builder):

    llm = await builder.get_llm(
        llm_name=tool_config.llm_name,
        wrapper_type=LLMFrameworkEnum.LANGCHAIN
    )

    async def _arun(user_input: str) -> str:
        if get_routing_lane() != "build_pipeline":
            logger.info("[Planner] Skipping — not build_pipeline lane")
            return json.dumps({"skipped": True})

        try:
            logger.info("[Planner] Starting...")
            research_context = load_research_context()

            is_html = any(k in user_input.lower() for k in [
                "html", "web", "website", "webpage", "ui", "interface",
                "game", "tic tac", "chess", "rummy", "browser", "frontend", "visual"
            ])

            if is_html:
                output_rule = "- Output: a single self-contained HTML file with inline CSS and JS"
                code_rules  = """
CRITICAL CODE RULES:
- Output must be a SINGLE HTML FILE with everything inline
- Must be interactive and fully functional
- Dark modern UI, no external dependencies except CDN
- NO placeholder text or fake data"""
            else:
                output_rule = "- Output: a single Python file that prints all results to stdout"
                code_rules  = """
CRITICAL CODE RULES:
- Use ONLY Python standard library (math, random, collections, datetime, json, re, string, itertools, functools)
- NO tkinter, pygame, or ANY GUI framework
- NO requests, urllib, httpx, or ANY network calls  
- NO sqlite3 or databases
- NO placeholder API keys
- ALL output must use print() — user sees ONLY what is printed
- The script must run completely in under 30 seconds
- Must produce visible printed output the user can read"""

            plan_prompt = f"""Design a build plan for this project.

USER REQUEST: {user_input}

RESEARCH CONTEXT:
{research_context or "No research context available."}

PROJECT TYPE: {"HTML/Web" if is_html else "Python CLI"}
{output_rule}

{code_rules}

Return ONLY valid JSON with this exact structure:
{{
  "project": "short project name",
  "output_type": "{"html" if is_html else "python"}",
  "algorithm_steps": [
    {{
      "step_number": 1,
      "step_title": "...",
      "step_description": "...",
      "instructions": [
        "specific implementation instruction 1",
        "specific implementation instruction 2"
      ],
      "inputs": [],
      "outputs": [],
      "dependencies": {{
        "requires_previous_steps": [],
        "provides_for_next_steps": []
      }}
    }}
  ]
}}

Rules:
- 4 steps maximum
- Each instruction must be specific and implementable
- No vague instructions like "add error handling"
- For Python: last step must include "Call main() and print all results"
- Valid JSON only, no markdown
"""
            resp    = await llm.ainvoke(plan_prompt)
            content = resp.content if hasattr(resp, "content") else str(resp)
            plan    = extract_json(content)

            if "project" not in plan:
                plan["project"] = user_input[:40]
            if not plan.get("algorithm_steps"):
                raise ValueError("Plan missing algorithm_steps")

            os.makedirs(os.path.dirname(PLAN_FILE), exist_ok=True)
            with open(PLAN_FILE, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2)

            logger.info(f"[Planner] Plan saved — {len(plan['algorithm_steps'])} steps, type={plan.get('output_type','?')}")
            return json.dumps(plan, indent=2)

        except Exception as e:
            logger.error(f"[Planner] Failed: {e}", exc_info=True)
            return json.dumps({"project": "Error", "algorithm_steps": []})

    yield FunctionInfo.from_fn(
        _arun,
        description="Planner — creates a build blueprint with strict CLI/HTML output rules"
    )