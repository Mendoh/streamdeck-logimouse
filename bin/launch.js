// Stream Deck starts this with Node; the real plugin is plugin.py (stdlib-only Python).
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

const script = path.join(__dirname, "..", "plugin.py");
const candidates = [
  process.env.LOGIMOUSE_PYTHON,
  path.join(process.env.LOCALAPPDATA || "", "Programs", "Python", "Python313", "pythonw.exe"),
  "pythonw.exe",
  "pyw.exe",
].filter(Boolean);

const exe = candidates.find((c) => !c.includes(path.sep) || fs.existsSync(c));
const child = spawn(exe, ["-I", script, ...process.argv.slice(2)], { stdio: "ignore", windowsHide: true });
child.on("exit", (code) => process.exit(code ?? 1));
child.on("error", (e) => {
  fs.appendFileSync(path.join(__dirname, "..", "plugin.log"), `launch error: ${e}\n`);
  process.exit(1);
});
for (const sig of ["SIGINT", "SIGTERM", "exit"]) process.on(sig, () => child.kill());
