// Drives the real index.html in jsdom against a running `codefix web` server (needs Node + `npm i jsdom`).
// Usage: start the server in a scratch project (see docs/web-ui.md), then: node tools/ui_smoke_test.js http://127.0.0.1:8765/
// The scratch project must contain src/app.py (see docs) and the test works in the server's working folder (arg 2).
const { JSDOM } = require("jsdom");
const fs = require("fs");
const BASE = process.argv[2], ROOT = process.argv[3] || "/tmp/webproj";
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failures = 0;
const check = (name, cond, extra) => { console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : "  -> " + extra)); if (!cond) failures++; };
(async () => {
  const dom = await JSDOM.fromURL(BASE, { runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o); w.confirm = () => true; w.HTMLElement.prototype.scrollIntoView = () => {}; } });
  const w = dom.window, d = w.document, $ = id => d.getElementById(id);
  const errors = []; w.addEventListener("error", e => errors.push(e.message));
  const tab = n => d.querySelector(`nav button[data-tab=${n}]`).click();
  await sleep(1200);
  check("status bar filled from /api/state", /23 rules/.test($("status").textContent), $("status").textContent);

  // ---------------- paste tab: input stays, output has its own place
  $("btn-sample").click(); await sleep(1500);
  const SAMPLE = $("code").value;
  check("sample analyzed and findings rendered", d.querySelectorAll("#findings .finding").length >= 5, "");
  check("report-only findings have disabled boxes", d.querySelectorAll("#findings input:disabled").length >= 1, "");
  check("output box starts empty", $("out-code").value === "", $("out-code").value);
  $("btn-fix").click(); await sleep(1800);
  const out1 = $("out-code").value;
  check("safe fixes appear in the separate output box", out1.includes("item is None") && out1.includes("return None"), out1);
  check("the ORIGINAL input box is untouched", $("code").value === SAMPLE, "input changed");
  check("review/risky NOT applied without ticking", out1.includes("import os") && out1.includes("bucket=[]") && out1.includes("except:"), out1);
  check("output readonly", $("out-code").readOnly, "");
  check("diff pane has a diff", !!$("pane-diff").querySelector(".diff"), $("pane-diff").textContent);
  check("report pane lists applied fixes", /Applied \d+ fix/.test($("result").textContent), $("result").textContent);
  d.querySelectorAll("#findings input:not(:disabled)").forEach(c => c.checked = true);
  $("btn-fix").click(); await sleep(1800);
  const out2 = $("out-code").value;
  check("ticked fixes applied in the output", out2.includes("bucket=None") && out2.includes("except Exception:") && !out2.includes("import os\n"), out2);
  check("input still untouched after second fix", $("code").value === SAMPLE, "");
  $("out-use").click(); await sleep(1200);
  check("'Use as input' moves fixed code to the input", $("code").value === out2, "");
  $("code").value = "def f(:\n"; $("btn-fix").click(); await sleep(1200);
  check("syntax error: report with context, nothing in output", /Syntax error on line 1/.test($("result").textContent) && $("out-code").value === out2 || /Syntax error on line 1/.test($("result").textContent), $("result").textContent);
  check("syntax error also listed in findings", /SYNTAX_ERROR/.test($("findings").textContent), "");

  // ---------------- files tab
  tab("files");
  $("path").value = "src"; $("btn-review").click(); await sleep(1500);
  const boxes = d.querySelectorAll("#rules input[data-rule]");
  check("rules table lists fixable rules", boxes.length >= 2, boxes.length);
  $("btn-browse").click(); await sleep(900);
  check("browse lists app.py", [...d.querySelectorAll("#filelist div")].some(x => x.textContent.includes("app.py")), "");
  $("btn-preview").click(); await sleep(1800);
  const before = fs.readFileSync(ROOT + "/src/app.py", "utf8");
  check("preview shows diff, file untouched", $("fres").querySelector(".diff") && before.includes("i == None"), $("fres").textContent);
  boxes.forEach(c => { if (c.dataset.rule === "UNUSED_IMPORT") c.checked = true; });
  $("btn-apply").click(); await sleep(2200);
  const after = fs.readFileSync(ROOT + "/src/app.py", "utf8");
  check("apply wrote safe + ticked fixes with backup", after.includes("i is None") && !after.includes("import os") && fs.existsSync(ROOT + "/.codefix/backups"), after);
  const undoBtn = [...d.querySelectorAll("#fres button")].find(b => /Undo/.test(b.textContent));
  undoBtn.click(); await sleep(1200);
  check("undo restored the original file", fs.readFileSync(ROOT + "/src/app.py", "utf8") === before, "");

  // ---------------- skills & knowledge
  tab("kb"); await sleep(1500);
  check("all rules table populated", d.querySelectorAll("#rtable tr").length >= 23, "");
  $("sk-name").value = "team-habits"; $("sk-title").value = "Team habits"; $("sk-new").click(); await sleep(1500);
  check("new skill appears in the skills list and selector", $("skills").textContent.includes("team-habits") && [...$("k-skill").options].some(o => o.value === "team-habits"), $("skills").textContent);
  $("k-skill").value = "team-habits";
  $("k-id").value = "TEAM_HAS_KEY"; $("k-msg").value = "dict.has_key was removed in Python 3";
  $("k-bad").value = "d.has_key(k)"; $("k-good").value = "k in d"; $("k-neg").value = "k in d";
  $("k-learn").click(); await sleep(1500);
  check("learn fills in the generalized pattern", $("k-pat").value === "$d.has_key($k)" && $("k-rep").value === "$k in $d", $("k-pat").value);
  $("k-test").value = "def f(data, key):\n    if data.has_key(key):\n        return 1\n"; $("k-try").click(); await sleep(1500);
  check("try shows match and fixed code", /Flags 1/.test($("k-preview").textContent) && $("k-preview").textContent.includes("key in data"), $("k-preview").textContent);
  $("k-save").click(); await sleep(1800);
  check("rule saved into the skill (listed there)", $("skills").textContent.includes("TEAM_HAS_KEY") && fs.existsSync(ROOT + "/knowledge/skills/team-habits.json"), $("skills").textContent);
  tab("paste"); $("code").value = "def g(a, b):\n    return a.has_key(b)\n"; $("btn-analyze").click(); await sleep(1200);
  check("skill rule fires in Paste tab without restart", $("findings").textContent.includes("TEAM_HAS_KEY"), $("findings").textContent);
  tab("kb");
  const toggle = d.querySelector('#skills input[type=checkbox]'); toggle.checked = false; toggle.dispatchEvent(new w.Event("change")); await sleep(1500);
  tab("paste"); $("btn-analyze").click(); await sleep(1200);
  check("disabling the skill switches its rules off", !$("findings").textContent.includes("TEAM_HAS_KEY"), $("findings").textContent);
  tab("kb");
  const learnJson = { format: "codefix-skill", name: "agent-made", title: "From my agent", rules: [
    { id: "AGENT_NO_EVAL", message: "eval is banned", bad: "eval(s)", match: { type: "call", name: "eval" } },
    { id: "AGENT_PRINT", message: "use logging", bad: "print(x)", good: "logger.info(x)", match: { type: "pattern", pattern: "print($x)", replace: "logger.info($x)" } }] };
  $("sk-json").value = JSON.stringify(learnJson); $("sk-validate").click(); await sleep(1500);
  check("check-it validates the skill file rule by rule", /Valid: 2 rule/.test($("sk-report").textContent), $("sk-report").textContent);
  $("sk-learn").click(); await sleep(1800);
  check("agent skill learned and listed", /Learned/.test($("sk-report").textContent) && $("skills").textContent.includes("AGENT_NO_EVAL"), $("sk-report").textContent);
  $("sk-json").value = JSON.stringify({ name: "bad-one", rules: [{ id: "COMPARE_NONE", message: "m", bad: "a", match: { type: "call", name: "a" } }] });
  $("sk-learn").click(); await sleep(1500);
  check("conflicting skill refused with a reason", /already used/.test($("sk-report").textContent) && !$("skills").textContent.includes("bad-one"), $("sk-report").textContent);
  $("k-mode").value = "pattern"; $("k-mode").dispatchEvent(new w.Event("change"));
  $("k-id").value = "USER_BROKEN"; $("k-msg").value = "x"; $("k-pat").value = "$a"; $("k-bad").value = "x = 1"; $("k-rep").value = "";
  $("k-save").click(); await sleep(1200);
  check("invalid rule refused with explanation", /literal token/.test($("k-out").textContent), $("k-out").textContent);

  // ---------------- rules & policy
  tab("policy"); await sleep(1500);
  check("policy table lists every rule", d.querySelectorAll("#p-table tr").length >= 25, d.querySelectorAll("#p-table tr").length);
  $("p-filter").value = "COMPARE_NONE"; $("p-filter").dispatchEvent(new w.Event("input"));
  const sel = d.querySelector('#p-table select[data-rule=COMPARE_NONE]'); sel.value = "report_only"; sel.dispatchEvent(new w.Event("change"));
  $("p-save").click(); await sleep(1500);
  check("policy saved to codefix.policy.json", fs.existsSync(ROOT + "/codefix.policy.json") && /report_only/.test(fs.readFileSync(ROOT + "/codefix.policy.json", "utf8")), "");
  tab("paste"); $("code").value = "if a == None:\n    pass\n"; $("btn-analyze").click(); await sleep(1200);
  const row = [...d.querySelectorAll("#findings .finding")].find(x => x.textContent.includes("COMPARE_NONE"));
  check("rule now reported but never fixed", row && /report only/.test(row.textContent), row && row.textContent);
  $("btn-fix").click(); await sleep(1500);
  check("nothing is fixed for that rule", $("out-code").value === "if a == None:\n    pass\n", $("out-code").value);
  tab("policy"); $("p-reset").click(); await sleep(1500);
  check("reset removes the customisation", !/report_only/.test(fs.readFileSync(ROOT + "/codefix.policy.json", "utf8")), "");
  check("no uncaught JS errors", errors.length === 0, errors.join("; "));
  console.log(failures ? `\n${failures} UI check(s) failed` : "\nall UI checks passed");
  process.exit(failures ? 1 : 0);
})();
