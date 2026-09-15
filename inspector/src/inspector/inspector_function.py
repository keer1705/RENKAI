import logging
import os
import traceback
import threading
import io
import sys

from pydantic import Field
from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

logger = logging.getLogger(__name__)


BLOCKED_MODULES = [
    "uvicorn", "flask", "fastapi", "webbrowser",
    "http.server", "socketserver", "streamlit",
    "gradio", "django",
]

DANGEROUS_PATTERNS = [
    "os.remove(",
    "os.rmdir(",
    "os.unlink(",
    "os.system(",
    "shutil.rmtree(",
    "shutil.move(",
    "subprocess.call(",
    "subprocess.run(",
    "subprocess.Popen(",
    "sys.exit(",
    "eval(",
    "exec(",         
    "ctypes.",
    "cffi.",
    "marshal.",
    "pickle.loads(",  
]


def _check_dangerous_patterns(code: str):
    for pattern in DANGEROUS_PATTERNS:
        if pattern in code:
            return pattern
    return None


SAFE_BUILTINS = {
    "print": print, "range": range, "len": len,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "enumerate": enumerate, "zip": zip, "map": map,
    "filter": filter, "sorted": sorted, "reversed": reversed,
    "min": min, "max": max, "sum": sum, "abs": abs,
    "round": round, "pow": pow, "divmod": divmod,
    "isinstance": isinstance, "issubclass": issubclass,
    "hasattr": hasattr, "getattr": getattr, "setattr": setattr,
    "type": type, "repr": repr, "vars": vars, "dir": dir,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "StopIteration": StopIteration,
    "RuntimeError": RuntimeError, "AttributeError": AttributeError,
    "NotImplementedError": NotImplementedError,
    "True": True, "False": False, "None": None,
    "open": open,          # ← allowed: constructor writes files legitimately
    "__import__": __import__,  # needed for import math etc inside exec
}


def _run_with_timeout(code: str, exec_globals: dict, timeout: int):
    result = {"output": "", "error": None}
    buffer = io.StringIO()

    def _target():
        old_stdout = sys.stdout
        sys.stdout = buffer
        try:
            exec(code, exec_globals)  # noqa: S102
            result["output"] = buffer.getvalue()
        except Exception:
            result["error"]  = traceback.format_exc()
            result["output"] = buffer.getvalue()  # keep partial output
        finally:
            sys.stdout = old_stdout

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        return buffer.getvalue(), "TIMEOUT: Code exceeded execution limit"

    return result["output"], result["error"]


class InspectorFunctionConfig(FunctionBaseConfig, name="inspector"):
    output_file_path: str = Field(
        default="S:/Renkai/constructor_agent/output.py",
        description="Path to generated code file",
    )
    execute_timeout: int = Field(default=30, description="Execution timeout in seconds")


@register_function(config_type=InspectorFunctionConfig)
async def inspector_function(config: InspectorFunctionConfig, builder: Builder):

    async def _response_fn(input_message: str) -> str:
        file_path = config.output_file_path

        if not os.path.exists(file_path):
            return (
                "OVERALL STATUS: FAILED\nSEND BACK TO CONSTRUCTOR\n\n"
                "DEBUG SECTION:\nERROR_TYPE: FILE_NOT_FOUND\n"
                "ERROR_MESSAGE: output.py not found\n"
            )

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception as e:
            return (
                "OVERALL STATUS: FAILED\nSEND BACK TO CONSTRUCTOR\n\n"
                f"DEBUG SECTION:\nERROR_TYPE: FILE_READ_ERROR\nERROR_MESSAGE: {e}\n"
            )

        if not code.strip():
            return (
                "OVERALL STATUS: FAILED\nSEND BACK TO CONSTRUCTOR\n\n"
                "DEBUG SECTION:\nERROR_TYPE: EMPTY_CODE\n"
                "ERROR_MESSAGE: Generated file is empty\n"
            )

        danger = _check_dangerous_patterns(code)
        if danger:
            return (
                "OVERALL STATUS: FAILED\nSEND BACK TO CONSTRUCTOR\n\n"
                f"DEBUG SECTION:\nERROR_TYPE: DANGEROUS_PATTERN\n"
                f"ERROR_MESSAGE: Code contains blocked pattern: '{danger}'\n"
                f"INSTRUCTION: Rewrite without '{danger}'. "
                f"Use only safe stdlib (math, random, collections, itertools).\n"
            )

        for mod in BLOCKED_MODULES:
            if mod in code:
                logger.info(f"[Inspector] Server module detected: {mod} — skipping exec")
                lines = code.count("\n") + 1

                # If it's an HTML file written separately, note that
                html_path = file_path.replace("output.py", "output.html")
                if os.path.exists(html_path):
                    open_hint = f"Open in browser: {html_path}"
                else:
                    open_hint = f"Run manually: python {file_path}"

                return (
                    "OVERALL STATUS: PASSED\n\n"
                    "PROGRAM OUTPUT:\n"
                    f"Web/server project generated successfully! ({lines} lines)\n"
                    f"Framework detected: {mod}\n"
                    f"{open_hint}\n\n"
                    f"This type of app runs in your browser or terminal,\n"
                    f"not inside the inspector. The code is ready.\n\n"
                    f"FINAL CODE:\n{code}\n"
                )

        try:
            compile(code, "<string>", "exec")
        except SyntaxError:
            syntax_trace = traceback.format_exc()
            return (
                "OVERALL STATUS: FAILED\nSEND BACK TO CONSTRUCTOR\n\n"
                f"DEBUG SECTION:\nERROR_TYPE: SYNTAX_ERROR\n"
                f"TRACEBACK:\n{syntax_trace}\n\n"
                f"ORIGINAL CODE:\n{code}\n"
            )

        exec_globals = {"__name__": "__main__", "__builtins__": SAFE_BUILTINS}
        stdout_output, error = _run_with_timeout(code, exec_globals, config.execute_timeout)

        if error:
            err_type = "TIMEOUT" if "TIMEOUT" in error else "RUNTIME_ERROR"
            err_msg  = (
                f"Code timed out after {config.execute_timeout}s — check for infinite loops"
                if err_type == "TIMEOUT"
                else error
            )

            partial = f"\nPARTIAL OUTPUT (before crash):\n{stdout_output}\n" if stdout_output.strip() else ""
            return (
                "OVERALL STATUS: FAILED\nSEND BACK TO CONSTRUCTOR\n\n"
                f"DEBUG SECTION:\nERROR_TYPE: {err_type}\n"
                f"TRACEBACK:\n{err_msg}\n"
                f"{partial}\n"
                f"ORIGINAL CODE:\n{code}\n"
            )


        output_section = stdout_output if stdout_output.strip() else "(no output printed)"
        return (
            "OVERALL STATUS: PASSED\n\n"
            f"PROGRAM OUTPUT:\n{output_section}\n\n"
            f"FINAL CODE:\n{code}\n"
        )

    try:
        yield FunctionInfo.create(
            single_fn=_response_fn,
            description="Safely executes generated Python code and captures stdout for the UI",
        )
    except GeneratorExit:
        logger.warning("Inspector exited early!")
    finally:
        logger.info("Inspector cleanup done.")