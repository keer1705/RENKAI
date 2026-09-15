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

BASE      = "S:/Renkai"
JSON_PATH = f"{BASE}/planner/output.json"
OUT_PY    = f"{BASE}/constructor_agent/output.py"
OUT_HTML  = f"{BASE}/constructor_agent/output.html"


class ConstructorAgentFunctionConfig(FunctionBaseConfig, name="constructor_agent"):
    llm_name: str      = Field(default="nim_llm")
    verbose:  bool     = Field(default=True)
    max_fix_attempts: int = Field(default=3)
    description: str   = Field(default="Generates working code from plan")


def is_html_project(plan_data: dict) -> bool:
    """
    HTML mode for: games, dashboards, visual tools, web UIs.
    Python mode for: algorithms, data processing, simulations, CLI tools.
    """
    project = plan_data.get("project", "").lower()
    steps   = json.dumps(plan_data.get("algorithm_steps", [])).lower()
    # Explicit HTML from planner
    if plan_data.get("output_type") == "html":
        return True
    html_keywords = [
        "html", "css", "webpage", "website", "web page", "frontend",
        "tailwind", "dashboard", "ui", "interface", "game", "tic tac",
        "chess", "rummy", "browser", "visual", "canvas", "interactive",
        "click", "button", "drag", "animation",
    ]
    return any(k in project or k in steps for k in html_keywords)


def needs_simulation(plan_data: dict) -> bool:
    """Detect if project would normally need internet/API — use simulated data instead."""
    project = plan_data.get("project", "").lower()
    steps   = json.dumps(plan_data.get("algorithm_steps", [])).lower()
    api_keywords = [
        "weather", "stock", "price", "api", "scrape", "fetch", "news",
        "twitter", "github", "slack", "telegram", "bitcoin", "crypto",
        "real-time", "live data", "monitor", "alert",
    ]
    return any(k in project or k in steps for k in api_keywords)


def clean_code(text: str) -> str:
    text = text.strip()
    text = re.sub(r"```html|```python|```javascript|```css|```", "", text)
    return text.strip()


def parse_inspector_debug(output: str):
    err  = re.search(r"ERROR_TYPE:\s*(.+)", output)
    tb   = re.search(r"TRACEBACK:\n(.+?)\n\nORIGINAL CODE:", output, re.S)
    return {
        "error_type": err.group(1).strip() if err else "UNKNOWN",
        "traceback":  tb.group(1).strip()  if tb  else "",
        "is_failed":  "OVERALL STATUS: FAILED" in output,
    }


async def surgical_fix(llm, code: str, error_text: str) -> str:
    prompt = f"""You are a SURGICAL Python code repair agent.
Fix ONLY the specific error shown. Return the COMPLETE corrected Python code.
No markdown, no backticks, no explanation.

STRICT RULES (do not violate or the fix will also fail):
- Standard library ONLY: math, random, collections, itertools, datetime, json, re, os, sys, string, functools, statistics
- NO tkinter, pygame, wx, or ANY GUI
- NO requests, httpx, urllib, or ANY network calls
- NO placeholder API keys or "YOUR_KEY_HERE"
- NO sqlite3 or databases
- ALL results must be printed with print()

ERROR:
{error_text}

CODE TO FIX:
{code}
"""
    resp = await llm.ainvoke(prompt)
    return clean_code(resp.content if hasattr(resp, "content") else str(resp))


@register_function(config_type=ConstructorAgentFunctionConfig)
async def constructor_agent_function(config: ConstructorAgentFunctionConfig, builder: Builder):

    async def _response_fn(input_message: str) -> str:
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                plan_data = json.load(f)
        except Exception:
            plan_data = None

        if not plan_data or not plan_data.get("algorithm_steps"):
            return "ERROR: No plan found. Run planner first."

        project         = plan_data.get("project", "Unnamed")
        algorithm_steps = plan_data["algorithm_steps"]
        html_mode       = is_html_project(plan_data)
        sim_mode        = needs_simulation(plan_data) and not html_mode
        OUT_FILE        = OUT_HTML if html_mode else OUT_PY

        logger.info(f"[Constructor] Mode={'HTML' if html_mode else 'Python'} Sim={sim_mode} | {project}")

        llm = await builder.get_llm(
            llm_name=config.llm_name,
            wrapper_type=LLMFrameworkEnum.LANGCHAIN
        )
        os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)

        if html_mode:
            instructions = []
            for step in algorithm_steps:
                for inst in step.get("instructions", []):
                    instructions.append(f"- {inst}")

            prompt = f"""You are an expert full-stack developer. Generate a complete single-file HTML application.

PROJECT: {project}

REQUIREMENTS:
{chr(10).join(instructions)}

STRICT RULES:
- Return ONLY raw HTML — absolutely no markdown, no backticks, no explanation text
- All CSS inside <style>, all JavaScript inside <script>
- Dark theme with modern clean UI (dark backgrounds, light text)
- The app must be FULLY FUNCTIONAL with real working JavaScript logic
- ALL features listed in requirements must actually work
- For agents/AI tools: implement the agent logic in JavaScript — state machine, decision tree, or rule engine
- For games: full game loop, win condition, score tracking
- For dashboards: real charts using Chart.js from CDN, real data generation in JS
- For data tools: actual processing logic in JS, not just display
- Use CDN libraries when needed: Chart.js, D3.js, Lodash (cdnjs.cloudflare.com)
- NO placeholder buttons that do nothing
- NO "coming soon" or "TODO" sections
- The HTML file must work perfectly when opened in any browser

Generate the complete HTML now:"""

            resp = None
            for attempt in range(2):
                try:
                    resp = await llm.ainvoke(prompt)
                    break
                except Exception as e:
                    if attempt == 0 and ("timeout" in str(e).lower() or "cancel" in str(e).lower()):
                        logger.warning("[Constructor] HTML timeout on attempt 1 — retrying with shorter prompt")
                        # Shorter fallback prompt
                        short_instructions = chr(10).join(instructions[:8])  # first 8 only
                        prompt = f"""Generate a complete single-file HTML app for: {project}

Key features:
{short_instructions}

Rules: Dark theme, inline CSS+JS, fully functional, no external deps except CDN.
Return ONLY raw HTML starting with <!DOCTYPE html>"""
                    else:
                        raise

            if resp is None:
                return f"ERROR: HTML generation timed out for {project}"

            code = clean_code(resp.content if hasattr(resp, "content") else str(resp))

            if not code.strip().startswith("<!"):
                code = "<!DOCTYPE html>\n" + code

            with open(OUT_FILE, "w", encoding="utf-8") as f:
                f.write(code)

            logger.info(f"[Constructor] HTML saved ({code.count(chr(10))} lines)")
            return f"HTML file generated! Open: {OUT_FILE}"


        sim_note = ""
        if sim_mode:
            sim_note = """
SIMULATION MODE — This project normally needs internet/APIs but runs in a sandbox:
- Generate realistic HARDCODED/SIMULATED data instead of real API calls
- Use random/datetime to make data feel dynamic
- Add a comment: # In production: replace with real API call to [service]
- The simulation must still demonstrate the full agent logic and print useful output
"""

        complete_code = ""
        inspector_fn  = await builder.get_function("inspector")

        for step in algorithm_steps:
            step_num   = step.get("step_number")
            step_title = step.get("step_title")
            instr_text = "\n".join(f"- {i}" for i in step.get("instructions", []))

            prompt = f"""Generate Python code for this step.

PROJECT: {project}
STEP {step_num}: {step_title}
{sim_note}
PREVIOUS CODE:
{complete_code or "# start of file"}

INSTRUCTIONS FOR THIS STEP:
{instr_text}

ABSOLUTE RULES:
- Standard library ONLY: math, random, collections, itertools, datetime, json, re, os, sys, string, functools, statistics
- NO tkinter, pygame, wx, or ANY GUI library whatsoever
- NO requests, httpx, urllib, aiohttp, or ANY network calls
- NO placeholder keys like "YOUR_API_KEY"
- NO sqlite3 or any database
- Every function must be complete and immediately runnable
- Final step MUST call main() and print all results clearly
- Use print() for ALL output — user sees ONLY what is printed
- Print section headers, results, summaries — make output readable
- Return ONLY Python code, no markdown, no backticks, no explanation
"""
            resp           = await llm.ainvoke(prompt)
            generated      = clean_code(resp.content if hasattr(resp, "content") else str(resp))
            complete_code += f"\n\n# Step {step_num}: {step_title} \n{generated}"

        # Self-healing part
        code = complete_code
        for attempt in range(config.max_fix_attempts):
            with open(OUT_PY, "w", encoding="utf-8") as f:
                f.write(code)

            result = await inspector_fn.ainvoke("check")
            parsed = parse_inspector_debug(result)

            if config.verbose:
                logger.info(f"[Constructor] Attempt {attempt+1} → {parsed['error_type']}")

            if not parsed["is_failed"]:
                logger.info(f"[Constructor] Passed attempt {attempt+1}")
                return code

            code = await surgical_fix(llm, code, parsed["traceback"])

        # Write final version even if still failing
        with open(OUT_PY, "w", encoding="utf-8") as f:
            f.write(code)
        return code

    yield FunctionInfo.create(
        single_fn=_response_fn,
        description=config.description
    )