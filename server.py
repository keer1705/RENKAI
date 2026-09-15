# server.py — FINAL VERSION
# Place at: S:\agent_b_agents\nemo-agent-toolkit\examples\rummy\rummy\server.py
# Run: python server.py

import http.server
import socketserver
import json
import subprocess
import os
import threading
import re
import time
import logging

logger = logging.getLogger(__name__)

PORT     = 8000
THIS_DIR = os.path.dirname(os.path.abspath(__file__))

PATH_RESEARCH    = os.path.join(THIS_DIR, "deerflow_agent",    "research_output.json")
PATH_PLAN        = os.path.join(THIS_DIR, "planner",           "output.json")
PATH_OUTPUT_PY   = os.path.join(THIS_DIR, "constructor_agent", "output.py")
PATH_OUTPUT_HTML = os.path.join(THIS_DIR, "constructor_agent", "output.html")
PATH_DIRECT_OUT  = os.path.join(THIS_DIR, "deerflow_agent",    "direct_output.txt")
CONFIG_FILE      = os.path.join(THIS_DIR, "rot", "src", "rot", "configs", "config.yml")

run_status = {
    "status": "IDLE", "log": [],
    "exec_output": None, "inspector_passed": None,
    "output_type": "html", "elapsed": None,
}
state_lock      = threading.Lock()
current_process = None


def parse_inspector(text):
    result = {"passed": "OVERALL STATUS: PASSED" in text, "exec_output": None}
    prog = re.search(
        r"PROGRAM OUTPUT:\s*\n([\s\S]*?)(?=\nFINAL CODE:|\nDEBUG SECTION:|$)", text)
    if prog:
        out = prog.group(1).strip()
        result["exec_output"] = out if out and out != "(no output printed)" else ""
    if result["exec_output"] is None:
        msg = re.search(r"MESSAGE:[^\n]*\n([\s\S]*?)(?=\nFINAL CODE:|$)", text)
        if msg:
            result["exec_output"] = msg.group(1).strip()
    return result

def safe_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return None

def safe_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f: return f.read()
    except: return None


def run_nat(user_input):
    global current_process
    start = time.time()

    for p in [PATH_RESEARCH, PATH_PLAN, PATH_OUTPUT_PY, PATH_OUTPUT_HTML, PATH_DIRECT_OUT]:
        try: os.remove(p)
        except: pass

    safe_input  = user_input.replace('"', '\\"')
    config_rel  = os.path.relpath(CONFIG_FILE, THIS_DIR)
    cmd         = f'nat run --config_file "{config_rel}" --input "{safe_input}"'

    print(f"\n[RENKAI] {cmd}\n")

    try:
        current_process = subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=os.environ.copy(), cwd=THIS_DIR,
        )
    except Exception as e:
        with state_lock:
            run_status["log"].append(f"[ERROR starting NAT] {e}")
            run_status["exec_output"] = f"Failed to start pipeline:\n{e}"
            run_status["status"]      = "DONE"
        return

    lines = []
    for raw in current_process.stdout:
        line = raw.rstrip()
        print(line)
        with state_lock:
            run_status["log"].append(line)
        lines.append(line)

        # Detect inspector passing while streaming
        if "OVERALL STATUS: PASSED" in line and run_status["exec_output"] is None:
            parsed = parse_inspector("\n".join(lines))
            with state_lock:
                run_status["exec_output"]      = parsed["exec_output"] or ""
                run_status["inspector_passed"] = True

        if "OVERALL STATUS: FAILED" in line:
            with state_lock:
                if run_status["inspector_passed"] is None:
                    run_status["inspector_passed"] = False

        # Detect HTML output from constructor log line
        if "HTML saved to" in line or "HTML file generated" in line:
            with state_lock:
                run_status["output_type"] = "html"

    current_process.wait()
    exit_code = current_process.returncode
    full_text = "\n".join(lines)
    parsed    = parse_inspector(full_text)

    with state_lock:
        # ── Determine exec_output ──────────────────────────────────────────
        if run_status["exec_output"] is None:
            if parsed["exec_output"]:
                run_status["exec_output"] = parsed["exec_output"]

            elif os.path.exists(PATH_OUTPUT_HTML):
                # HTML project — show a helpful message + the HTML content preview
                html_code = safe_text(PATH_OUTPUT_HTML) or ""
                lines_count = html_code.count("\n") + 1
                run_status["output_type"]  = "html"
                run_status["exec_output"]  = (
                    f"HTML project generated successfully!\n"
                    f"File: {PATH_OUTPUT_HTML}\n"
                    f"Size: {lines_count} lines\n\n"
                    f"Open that file in your browser to see the result.\n\n"
                    f"─────────────────────────────────────\n"
                    f"HTML PREVIEW (first 30 lines):\n"
                    f"─────────────────────────────────────\n"
                    + "\n".join(html_code.splitlines()[:30])
                )

            elif os.path.exists(PATH_OUTPUT_PY):
                code = safe_text(PATH_OUTPUT_PY) or ""
                run_status["exec_output"] = (
                    f"output.py written ({code.count(chr(10))+1} lines).\n"
                    f"No stdout was captured.\n"
                    f"Path: {PATH_OUTPUT_PY}"
                )

            else:
                # Direct answer lane — read from saved file first (no truncation)
                direct_text = safe_text(PATH_DIRECT_OUT)
                if direct_text and direct_text.strip():
                    run_status["exec_output"]      = direct_text.strip()
                    run_status["output_type"]      = "direct"
                    run_status["inspector_passed"] = True
                    logger.info(f"[server] Direct answer loaded ({len(direct_text)} chars)")
                else:
                    # Fallback: extract from log
                    final_answer = _extract_final_answer(full_text)
                    if final_answer:
                        run_status["exec_output"]      = final_answer
                        run_status["output_type"]      = "direct"
                        run_status["inspector_passed"] = True
                    else:
                        run_status["exec_output"] = (
                            "[Pipeline finished — no output captured]\n"
                            "Check the terminal for the agent response."
                        )

        if run_status["inspector_passed"] is None:
            run_status["inspector_passed"] = parsed["passed"] or (exit_code == 0)

        run_status["elapsed"] = round(time.time() - start, 1)
        run_status["status"]  = "DONE"

    print(f"\n[RENKAI] Done in {run_status['elapsed']}s  exit={exit_code}")


def _extract_final_answer(log_text: str) -> str:
    """Pull the 'Final Answer:' from NAT agent output for direct-lane responses."""
    match = re.search(r"Final Answer:\s*(.+?)(?=\n-{10}|\Z)", log_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Also try Workflow Result block
    match2 = re.search(r"Workflow Result:\n\[?'?([\s\S]*?)'?\]?\n-{10}", log_text)
    if match2:
        return match2.group(1).strip()
    return ""


class Handler(http.server.SimpleHTTPRequestHandler):

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200); self.end_headers()

    def log_message(self, fmt, *args):
        if "/status" not in self.path:
            print(f"[http] {fmt % args}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            for name in ["renkai_demo.html", "renkai_ui.html", "login.html"]:
                if os.path.exists(os.path.join(THIS_DIR, name)):
                    self.path = "/" + name
                    break
            return super().do_GET()

        if self.path.startswith("/status"):
            with state_lock:
                data = {
                    "status":           run_status["status"],
                    "log":              run_status["log"][-80:],
                    "research":         safe_json(PATH_RESEARCH),
                    "plan":             safe_json(PATH_PLAN),
                    "code":             safe_text(PATH_OUTPUT_PY),
                    "exec_output":      run_status["exec_output"],
                    "inspector_passed": run_status["inspector_passed"],
                    "output_type":      run_status["output_type"],
                    "elapsed":          run_status["elapsed"],
                    "html_content":     safe_text(PATH_OUTPUT_HTML),
                }
            self._json(data); return

        # /reset — lets UI restart without restarting the server
        if self.path == "/reset":
            with state_lock:
                run_status.update({
                    "status": "IDLE", "log": [],
                    "exec_output": None, "inspector_passed": None,
                    "output_type": None, "elapsed": None,
                })
            self._json({"ok": True}); return

        return super().do_GET()

    def do_POST(self):
        global current_process

        if self.path == "/run":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                data = json.loads(body)
            except:
                self._json({"ok": False, "reason": "Bad JSON"}, 400); return

            user_input = data.get("input", "").strip()
            if not user_input:
                self._json({"ok": False, "reason": "Empty input"}, 400); return

            with state_lock:
                # AUTO-RESET if previous run is done — no more 400 errors!
                if run_status["status"] in ("DONE", "ERROR", "IDLE"):
                    run_status.update({
                        "status": "RUNNING", "log": [],
                        "exec_output": None, "inspector_passed": None,
                        "output_type": None, "elapsed": None,
                    })
                elif run_status["status"] == "RUNNING":
                    self._json({"ok": False, "reason": "Pipeline already running — wait for it to finish"}); return

            threading.Thread(target=run_nat, args=(user_input,), daemon=True).start()
            self._json({"ok": True}); return

        self.send_response(404); self.end_headers()

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def startup_checks():
    print("\n" + "=" * 56)
    print("  RENKAI Server — Final Version")
    print("=" * 56)
    for label, path in [
        ("Config",   CONFIG_FILE),
        ("Rummy dir", THIS_DIR),
    ]:
        ok = os.path.exists(path)
        print(f"  {'✓' if ok else '✗'}  {label}: {path}")
    for name in ["renkai_demo.html", "renkai_ui.html"]:
        p = os.path.join(THIS_DIR, name)
        if os.path.exists(p):
            print(f"  ✓  UI: {name}"); break
    else:
        print(f"  ✗  UI file missing in {THIS_DIR}")
    r = subprocess.run("nat --version", shell=True, capture_output=True, text=True)
    print(f"  {'✓' if r.returncode==0 else '✗'}  nat CLI")
    print("=" * 56)
    print(f"  Open: http://localhost:{PORT}")
    print("=" * 56 + "\n")


if __name__ == "__main__":
    os.chdir(THIS_DIR)
    startup_checks()
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[RENKAI] Stopped.")