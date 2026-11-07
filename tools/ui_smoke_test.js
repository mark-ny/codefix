// Drives the real index.html in jsdom against a running `codefix web` server (needs Node + `npm i jsdom`).
// Usage: start the server in a scratch project (see docs/web-ui.md), then: node tools/ui_smoke_test.js http://127.0.0.1:8765/
const { JSDOM } = require("jsdom");
const BASE = process.argv[2];
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failures = 0;
const check = (name, cond, extra) => { console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : "  -> " + extra)); if (!cond) failures++; };
(async () => {
  const dom = await JSDOM.fromURL(BASE, { runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o); w.confirm = () => true; w.HTMLElement.prototype.scrollIntoView = () => {}; } });
  const w = dom.window, d = w.document, $ = id => d.getElementById(id);
  const errors = []; w.addEventListener("error", e => errors.push(e.message));
  await sleep(1200);
  check("status bar filled from /api/state", /23 rules/.test($("status").textContent), $("status").textContent);
  // ---- paste tab
  $("btn-sample").click(); await sleep(1500);
  const nf = d.querySelectorAll("#findings .finding").length;
  check("sample analyzed and findings rendered", nf >= 5, nf);
  check("fixable confirm-level finding has an enabled checkbox", d.querySelectorAll("#findings input:not(:disabled)").length >= 2, "");
  check("report-only findings have disabled boxes", d.querySelectorAll("#findings input:disabled").length >= 1, "");
  $("btn-fix").click(); await sleep(1800);
  const code1 = $("code").value;
  check("safe fixes applied automatically", code1.includes("item is None") && code1.includes("return None"), code1);
  check("review/risky NOT applied without ticking", code1.includes("import os") && code1.includes("bucket=[]") && code1.includes("except:"), code1);
  check("result card shows diff", /Applied \d+ fix/.test($("result").textContent) && $("result").querySelector(".diff"), $("result").textContent);
  check("undo enabled", !$("btn-undo").disabled, "");
  // tick the risky + review ones and fix
  d.querySelectorAll("#findings input:not(:disabled)").forEach(c => c.checked = true);
  $("btn-fix").click(); await sleep(1800);
  const code2 = $("code").value;
  check("ticked fixes applied (mutable default, bare except, unused import)", code2.includes("bucket=None") && code2.includes("except Exception:") && !code2.includes("import os\n"), code2);
  $("btn-undo").click(); await sleep(1200);
  check("undo restores previous text", $("code").value === code1, "");
  // syntax error
  $("code").value = "def f(:\n"; $("btn-fix").click(); await sleep(900);
  check("syntax error is reported, code untouched", /syntax error/i.test($("result").textContent) && $("code").value === "def f(:\n", $("result").textContent);
  // XSS: finding message containing html must be text
  $("code").value = "x = 1\nif y == None:\n    pass\n"; $("btn-analyze").click(); await sleep(900);
  check("no stray elements injected", d.querySelectorAll("#findings script, #findings img").length === 0, "");
  // ---- files tab
  d.querySelector('nav button[data-tab=files]').click();
  $("path").value = "src"; $("btn-review").click(); await sleep(1500);
  const boxes = d.querySelectorAll("#rules input[data-rule]");
  check("rules table lists fixable rules", boxes.length >= 2, boxes.length);
  $("btn-browse").click(); await sleep(900);
  check("browse lists app.py", [...d.querySelectorAll("#filelist div")].some(x => x.textContent.includes("app.py")), $("filelist").textContent);
  $("btn-preview").click(); await sleep(1800);
  check("preview shows a diff and writes nothing", $("fres").querySelector(".diff") && /dry-run/.test($("fres").textContent), $("fres").textContent);
  const fs = require("fs"); const before = fs.readFileSync("/tmp/webproj/src/app.py", "utf8");
  check("file untouched after preview", before.includes("i == None"), before);
  boxes.forEach(c => { if (c.dataset.rule === "UNUSED_IMPORT") c.checked = true; });
  $("btn-apply").click(); await sleep(2200);
  const after = fs.readFileSync("/tmp/webproj/src/app.py", "utf8");
  check("apply wrote safe + ticked fixes", after.includes("i is None") && !after.includes("import os"), after);
  check("backup exists", fs.existsSync("/tmp/webproj/.codefix/backups"), "");
  const undoBtn = [...d.querySelectorAll("#fres button")].find(b => /Undo/.test(b.textContent));
  check("undo button offered", !!undoBtn, $("fres").textContent);
  undoBtn.click(); await sleep(1200);
  check("undo restored the original file", fs.readFileSync("/tmp/webproj/src/app.py", "utf8") === before, "");
  // ---- knowledge tab
  d.querySelector('nav button[data-tab=kb]').click(); await sleep(1200);
  check("all rules table populated", d.querySelectorAll("#rtable tr").length >= 23, d.querySelectorAll("#rtable tr").length);
  $("k-id").value = "USER_HAS_KEY"; $("k-msg").value = "dict.has_key was removed in Python 3";
  $("k-bad").value = "d.has_key(k)"; $("k-good").value = "k in d"; $("k-neg").value = "k in d";
  $("k-learn").click(); await sleep(1500);
  check("learn fills in the generalized pattern", $("k-pat").value === "$d.has_key($k)" && $("k-rep").value === "$k in $d", $("k-pat").value + " | " + $("k-rep").value);
  $("k-test").value = "def f(data, key):\n    if data.has_key(key):\n        return 1\n"; $("k-try").click(); await sleep(1500);
  check("try shows match and fixed code", /Flags 1/.test($("k-preview").textContent) && $("k-preview").textContent.includes("key in data"), $("k-preview").textContent);
  $("k-save").click(); await sleep(1800);
  check("rule saved and listed under My rules", $("myrules").textContent.includes("USER_HAS_KEY"), $("myrules").textContent);
  check("saved to disk", fs.existsSync("/tmp/webproj/knowledge/project/user_rules.json"), "");
  // the new rule now works in the paste tab immediately
  d.querySelector('nav button[data-tab=paste]').click();
  $("code").value = "def g(a, b):\n    return a.has_key(b)\n"; $("btn-analyze").click(); await sleep(1200);
  check("new rule fires in Paste tab without restart", $("findings").textContent.includes("USER_HAS_KEY"), $("findings").textContent);
  // bad rule is rejected with reasons
  d.querySelector('nav button[data-tab=kb]').click();
  $("k-mode").value = "pattern"; $("k-mode").dispatchEvent(new w.Event("change"));
  $("k-id").value = "USER_BROKEN"; $("k-msg").value = "x"; $("k-pat").value = "$a"; $("k-bad").value = "x = 1"; $("k-rep").value = "";
  $("k-save").click(); await sleep(1200);
  check("invalid rule refused with explanation", /literal token/.test($("k-out").textContent), $("k-out").textContent);
  check("no uncaught JS errors", errors.length === 0, errors.join("; "));
  console.log(failures ? `\n${failures} UI check(s) failed` : "\nall UI checks passed");
  process.exit(failures ? 1 : 0);
})();
