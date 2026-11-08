"""Declarative rules: extend CodeFix with *data*, no Python required.

A knowledge entry carrying a ``match`` object becomes a real, runnable rule:

``pattern``  token pattern with ``$variables``; optional ``replace`` template (autofix)
             {"type": "pattern", "pattern": "$a == None", "replace": "$a is None"}
``call``     calls to a dotted name; optional ``replace_with`` renames the callee
             {"type": "call", "name": "os.system", "replace_with": "run_command"}
``import``   banned/legacy modules; optional ``replace_with`` for ``from x import y``
             {"type": "import", "module": "imp", "replace_with": "importlib"}
``regex``    report-only text search outside strings/comments
             {"type": "regex", "pattern": "TODO|FIXME", "flags": "i"}

Rules are plain data: they cannot execute code. Fixes still go through the normal pipeline
(policy, syntax validation, diff, backup, tests).
"""
from __future__ import annotations

import ast
import builtins
import io
import keyword
import re
import textwrap
import tokenize
from dataclasses import dataclass
from typing import Any, Iterable

from ..models import Finding, Location, RuleSpec, Severity
from .registry import Rule
from .schema import check_entry

ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
DOTTED_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
MARK_RE = re.compile(r"__CFVAR_([A-Za-z_]\w*?)__$")
MATCH_TYPES = ("pattern", "call", "import", "regex")
_SKIP = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
_OPEN, _CLOSE = ("(", "[", "{"), (")", "]", "}")
_BUILTINS = set(dir(builtins))
_FSTART = getattr(tokenize, "FSTRING_START", -1)
_FEND = getattr(tokenize, "FSTRING_END", -1)


class RuleError(ValueError):
    """The user-supplied rule definition is invalid."""


@dataclass
class Tok:
    type: int
    string: str
    start: tuple[int, int]
    end: tuple[int, int]


def tokenize_text(text: str) -> tuple[list[Tok], list[Tok]]:
    """Return (code tokens, comment tokens); tolerant of incomplete snippets."""
    toks: list[Tok] = []
    comments: list[Tok] = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            tok = Tok(t.type, t.string, t.start, t.end)
            if t.type == tokenize.COMMENT:
                comments.append(tok)
            elif t.type not in _SKIP:
                toks.append(tok)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return toks, comments


# ------------------------------------------------------------------ token patterns
@dataclass
class PTok:
    kind: str       # lit | var
    text: str


def compile_pattern(pattern: str) -> list[PTok]:
    if not isinstance(pattern, str) or not pattern.strip():
        raise RuleError("pattern must be a non-empty string")
    if len(pattern) > 500:
        raise RuleError("pattern is too long (500 characters max)")
    marked = VAR_RE.sub(lambda m: f"__CFVAR_{m.group(1)}__", pattern.strip())
    toks, _ = tokenize_text(marked)
    out: list[PTok] = []
    depth = 0
    for t in toks:
        m = MARK_RE.match(t.string) if t.type == tokenize.NAME else None
        out.append(PTok("var", m.group(1)) if m else PTok("lit", t.string))
        if t.type == tokenize.OP:
            depth += t.string in _OPEN
            depth -= t.string in _CLOSE
            if depth < 0:
                raise RuleError("pattern has an unbalanced closing bracket")
    if depth:
        raise RuleError("pattern has an unclosed bracket")
    if not out:
        raise RuleError("pattern contains no tokens")
    if all(p.kind == "var" for p in out):
        raise RuleError("pattern needs at least one literal token (it would match everything)")
    for a, b in zip(out, out[1:]):
        if a.kind == b.kind == "var":
            raise RuleError("two variables cannot be adjacent; put a literal token between them")
    return out


def pattern_vars(pat: list[PTok]) -> set[str]:
    return {p.text for p in pat if p.kind == "var"}


def _after_bracket(toks: list[Tok], i: int) -> int | None:
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j]
        if t.type == tokenize.OP and t.string in _OPEN:
            depth += 1
        elif t.type == tokenize.OP and t.string in _CLOSE:
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def primary_ends(toks: list[Tok], i: int) -> list[int]:
    """End indices (exclusive) of every operand starting at ``i``: name/number/string/bracket + postfix chain."""
    n = len(toks)
    if i >= n:
        return []
    t, ends = toks[i], []
    if t.type == tokenize.OP and t.string == "-" and i + 1 < n and toks[i + 1].type == tokenize.NUMBER:
        return [i + 2]
    if t.type == tokenize.NAME:
        if keyword.iskeyword(t.string) and t.string not in ("None", "True", "False"):
            return []
        j = i + 1
    elif t.type == tokenize.NUMBER:
        j = i + 1
    elif t.type == tokenize.STRING:
        j = i + 1
        while j < n and toks[j].type == tokenize.STRING:
            j += 1
    elif t.type == _FSTART:
        depth, j = 0, i
        while j < n:
            depth += toks[j].type == _FSTART
            depth -= toks[j].type == _FEND
            j += 1
            if depth == 0:
                break
        else:
            return []
    elif t.type == tokenize.OP and t.string in _OPEN:
        k = _after_bracket(toks, i)
        if k is None:
            return []
        j = k
    else:
        return []
    ends.append(j)
    while j < n:
        t = toks[j]
        if t.type == tokenize.OP and t.string == "." and j + 1 < n and toks[j + 1].type == tokenize.NAME:
            j += 2
        elif t.type == tokenize.OP and t.string in ("(", "["):
            k = _after_bracket(toks, j)
            if k is None:
                break
            j = k
        else:
            break
        ends.append(j)
    return ends


def match_at(pat: list[PTok], toks: list[Tok], i: int):
    caps: dict[str, tuple[int, int, tuple[str, ...]]] = {}

    def rec(pi: int, ti: int):
        if pi == len(pat):
            return ti
        p = pat[pi]
        if p.kind == "lit":
            if ti < len(toks) and toks[ti].string == p.text:
                return rec(pi + 1, ti + 1)
            return None
        for end in primary_ends(toks, ti):
            sig = tuple(t.string for t in toks[ti:end])
            if p.text in caps and caps[p.text][2] != sig:
                continue
            prev = caps.get(p.text)
            caps[p.text] = (ti, end, sig)
            r = rec(pi + 1, end)
            if r is not None:
                return r
            if prev is None:
                caps.pop(p.text, None)
            else:
                caps[p.text] = prev
        return None

    end = rec(0, i)
    return None if end is None else (end, caps)


def find_matches(pat: list[PTok], toks: list[Tok]):
    out, i = [], 0
    while i < len(toks):
        m = match_at(pat, toks, i)
        if m:
            out.append((i, m[0], m[1]))
            i = m[0]
        else:
            i += 1
    return out


class Offsets:
    """Maps tokenize (line, character column) to UTF-8 byte offsets in ``text``."""

    def __init__(self, text: str):
        self.lines = text.split("\n")
        self.starts, pos = [], 0
        for ln in self.lines:
            self.starts.append(pos)
            pos += len(ln.encode("utf-8")) + 1

    def __call__(self, pos: tuple[int, int]) -> int:
        line, col = pos
        return self.starts[line - 1] + len(self.lines[line - 1][:col].encode("utf-8"))


# --------------------------------------------------------------------------- rule
def dotted_name(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class DeclarativeRule(Rule):
    def __init__(self, spec: RuleSpec, match: dict[str, Any]):
        self.spec, self.match = spec, match
        self.type = match["type"]
        if self.type == "pattern":
            self.pat = compile_pattern(match["pattern"])
            self.template = match.get("replace")
            if self.template is not None:
                unknown = set(VAR_RE.findall(self.template)) - pattern_vars(self.pat)
                if unknown:
                    raise RuleError("replacement uses variables not in the pattern: " + ", ".join(sorted("$" + u for u in unknown)))
        elif self.type == "regex":
            flags = 0
            for ch in match.get("flags", ""):
                if ch not in "im":
                    raise RuleError("regex flags may only contain 'i' and 'm'")
                flags |= re.I if ch == "i" else re.M
            if len(match.get("pattern", "")) > 300:
                raise RuleError("regex is too long (300 characters max)")
            try:
                self.rx = re.compile(match["pattern"], flags)
            except re.error as exc:
                raise RuleError(f"invalid regex: {exc}") from exc

    def check(self, ctx) -> Iterable[Finding]:
        return getattr(self, "_" + self.type)(ctx)

    # ---- pattern
    def _function_at(self, ctx, line: int) -> str | None:
        best = None
        for node, q, _ in ctx.functions:
            if node.lineno <= line <= (node.end_lineno or node.lineno):
                if best is None or node.lineno >= best[0]:
                    best = (node.lineno, q)
        return best[1] if best else None

    def _pattern(self, ctx):
        text = ctx.source.text
        toks, comments = tokenize_text(text)
        off = Offsets(text)
        for a, b, caps in find_matches(self.pat, toks):
            first, last = toks[a], toks[b - 1]
            s, e = off(first.start), off(last.end)
            loc = Location(ctx.path, first.start[0], first.start[1] + 1, last.end[0], last.end[1] + 1)
            edits = []
            if self.template is not None and not any(first.start <= c.start < last.end for c in comments):
                def fill(m, caps=caps, toks=toks):
                    ca, cb, _ = caps[m.group(1)]
                    return text.encode("utf-8")[off(toks[ca].start):off(toks[cb - 1].end)].decode("utf-8")
                new = VAR_RE.sub(fill, self.template)
                if new != ctx.text(s, e):
                    edits = [ctx.edit(self.spec, s, e, new)]
            yield ctx.finding(self.spec, ctx.tree, edits=edits, location=loc, function=self._function_at(ctx, first.start[0]))

    # ---- call
    def _call(self, ctx):
        names = set(self.match.get("names") or [self.match["name"]])
        repl = self.match.get("replace_with")
        for node in ast.walk(ctx.tree):
            if isinstance(node, ast.Call):
                d = dotted_name(node.func)
                if d in names:
                    edits = []
                    if repl:
                        s, e = ctx.span(node.func)
                        if "\n" not in ctx.text(s, e):
                            edits = [ctx.edit(self.spec, s, e, repl)]
                    yield ctx.finding(self.spec, node, f"{self.spec.message}", edits=edits, metadata={"call": d})

    # ---- import
    def _import(self, ctx):
        mod, repl = self.match["module"], self.match.get("replace_with")
        hit = lambda m: m == mod or (m or "").startswith(mod + ".")
        for node in ast.walk(ctx.tree):
            if isinstance(node, ast.Import) and any(hit(a.name) for a in node.names):
                yield ctx.finding(self.spec, node)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and hit(node.module):
                edits = []
                if repl:
                    s, e = ctx.span(node)
                    m = re.search(rb"\bfrom\s+(" + re.escape(node.module.encode()) + rb")\b", ctx.masked(s, e))
                    if m:
                        edits = [ctx.edit(self.spec, s + m.start(1), s + m.end(1), node.module.replace(mod, repl, 1))]
                yield ctx.finding(self.spec, node, edits=edits)

    # ---- regex
    def _regex(self, ctx):
        text = ctx.source.text
        toks, comments = tokenize_text(text)
        blocked: dict[int, list[tuple[int, int]]] = {}
        for t in [*comments, *[t for t in toks if t.type == tokenize.STRING]]:
            for ln in range(t.start[0], t.end[0] + 1):
                blocked.setdefault(ln, []).append((t.start[1] if ln == t.start[0] else 0,
                                                   t.end[1] if ln == t.end[0] else 10**9))
        for ln, line in enumerate(text.split("\n"), 1):
            for m in self.rx.finditer(line):
                if self.match.get("code_only", True) and any(a <= m.start() < b for a, b in blocked.get(ln, [])):
                    continue
                loc = Location(ctx.path, ln, m.start() + 1, ln, m.end() + 1)
                yield ctx.finding(self.spec, ctx.tree, location=loc, function=self._function_at(ctx, ln),
                                  metadata={"match": m.group(0)})


def validate_match(match: Any, has_replacement_hint: bool | None = None) -> list[str]:
    if not isinstance(match, dict) or match.get("type") not in MATCH_TYPES:
        return [f"'match.type' must be one of {', '.join(MATCH_TYPES)}"]
    t, p = match["type"], []
    if t == "call":
        names = match.get("names") or ([match["name"]] if "name" in match else [])
        if not names or not all(isinstance(n, str) and DOTTED_RE.match(n) for n in names):
            p.append("'match.name' (or 'names') must be dotted identifiers such as os.system")
    if t == "import" and not (isinstance(match.get("module"), str) and DOTTED_RE.match(match["module"])):
        p.append("'match.module' must be a dotted module name")
    if t in ("call", "import") and match.get("replace_with") is not None \
            and not (isinstance(match["replace_with"], str) and DOTTED_RE.match(match["replace_with"])):
        p.append("'match.replace_with' must be a dotted identifier")
    if t in ("pattern", "regex") and not isinstance(match.get("pattern"), str):
        p.append("'match.pattern' must be a string")
    if t == "pattern" and match.get("replace") is not None and not isinstance(match["replace"], str):
        p.append("'match.replace' must be a string")
    return p


def has_replacement(match: dict[str, Any]) -> bool:
    return bool(match.get("replace") is not None if match.get("type") == "pattern" else match.get("replace_with"))


def normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Fill sensible defaults so a minimal rule ({id, message, bad, match}) is enough."""
    if not isinstance(entry, dict):
        raise RuleError("a rule must be a JSON object")
    e = dict(entry)
    e.setdefault("category", "project")
    e.setdefault("severity", "review")
    e.setdefault("confidence", 0.9)
    e.setdefault("requires_tests", False)
    e.setdefault("good", "")
    match = e.get("match")
    if isinstance(match, dict) and match.get("type") in MATCH_TYPES and "autofix" not in e:
        e["autofix"] = has_replacement(match)
    if isinstance(e.get("bad"), str) and e["bad"] and not e["bad"].endswith("\n"):
        e["bad"] += "\n"
    if isinstance(e.get("good"), str) and e["good"] and not e["good"].endswith("\n"):
        e["good"] += "\n"
    return e


def build_rule(entry: dict[str, Any], pack: str = "project") -> DeclarativeRule:
    """Validate a knowledge entry that has a ``match`` and turn it into a runnable rule."""
    problems = [p for p in check_entry(entry) if not p.startswith("unknown field")]
    if problems:
        raise RuleError("; ".join(problems))
    if not ID_RE.match(entry["id"]):
        raise RuleError("id must be UPPER_SNAKE_CASE, 3-64 characters, starting with a letter (e.g. USER_NO_PRINT)")
    if not entry["bad"].strip():
        raise RuleError("a 'bad' example is required")
    match = entry.get("match")
    problems = validate_match(match)
    if problems:
        raise RuleError("; ".join(problems))
    if bool(entry["autofix"]) != has_replacement(match):
        raise RuleError("'autofix' must be true exactly when the match defines a replacement")
    spec = RuleSpec(
        id=entry["id"], category=entry["category"], severity=Severity(entry["severity"]), confidence=float(entry["confidence"]),
        description=entry.get("description", ""), message=entry["message"], explanation=entry.get("explanation", ""),
        bad=entry["bad"], good=entry["good"], autofix=bool(entry["autofix"]), requires_tests=entry["requires_tests"],
        false_positive=entry.get("false_positive", ""), frameworks=list(entry.get("frameworks", [])),
        docs=list(entry.get("docs", [])), tests=list(entry.get("tests", [])), pack=pack)
    return DeclarativeRule(spec, match)


# ------------------------------------------------------------ learning from examples
def complete_snippet(code: str) -> tuple[str, str]:
    """Make a fragment parseable. Returns ``(code, suffix)`` (suffix is what was appended)."""
    code = textwrap.dedent(code).strip("\n") + "\n"
    for suffix in ("", "    pass\n"):
        try:
            ast.parse(code + suffix)
            return code + suffix, suffix
        except SyntaxError:
            continue
    raise RuleError("the example is not valid Python (headers such as `if x:` are completed with a body, nothing else is)")


def _balanced(toks: list[Tok]) -> bool:
    depth = 0
    for t in toks:
        if t.type == tokenize.OP:
            depth += t.string in _OPEN
            depth -= t.string in _CLOSE
            if depth < 0:
                return False
    return depth == 0


def _expand(toks: list[Tok], lo: int, hi: int) -> tuple[int, int] | None:
    while True:
        depth, low = 0, 0
        for t in toks[lo:hi]:
            if t.type == tokenize.OP:
                depth += t.string in _OPEN
                depth -= t.string in _CLOSE
                low = min(low, depth)
        if low < 0:
            if lo == 0:
                return None
            lo -= 1
        elif depth > 0:
            if hi >= len(toks):
                return None
            hi += 1
        else:
            return lo, hi


def _slice(text: str, toks: list[Tok], lo: int, hi: int) -> tuple[str, int]:
    off = Offsets(text)
    data = text.encode("utf-8")
    base = off(toks[lo].start)
    return data[base:off(toks[hi - 1].end)].decode("utf-8"), base


def _substitute(text: str, toks: list[Tok], lo: int, hi: int, names: set[str], strict_sites: bool = True) -> str:
    off = Offsets(text)
    data = text.encode("utf-8")
    base = off(toks[lo].start)
    out = data[base:off(toks[hi - 1].end)]
    edits = []
    for i in range(lo, hi):
        t = toks[i]
        prev = toks[i - 1].string if i > lo else None
        nxt = toks[i + 1].string if i + 1 < len(toks) else None
        if t.type == tokenize.NAME and t.string in names and prev != "." and nxt != "(":
            edits.append((off(t.start) - base, off(t.end) - base, "$" + t.string))
    for s, e, r in reversed(edits):
        out = out[:s] + r.encode() + out[e:]
    return out.decode("utf-8")


def _eligible(toks: list[Tok], lo: int, hi: int, allow_dot: bool = False) -> set[str]:
    """Names that look like data (not keywords, builtins, callees or attributes) and can become $variables."""
    names = set()
    for i in range(lo, hi):
        t = toks[i]
        prev = toks[i - 1].string if i > lo else None
        nxt = toks[i + 1].string if i + 1 < len(toks) else None
        if (t.type == tokenize.NAME and not keyword.iskeyword(t.string) and t.string not in _BUILTINS
                and prev != "." and nxt != "(" and (allow_dot or nxt != ".") and not t.string.startswith("__CFVAR")):
            names.add(t.string)
    return names


def generalize_example(bad: str, good: str | None) -> tuple[str, str | None]:
    """Turn a concrete before/after example into a ``$variable`` pattern and replacement template."""
    bad = textwrap.dedent(bad).strip("\n")
    btoks, _ = tokenize_text(bad)
    if not btoks:
        raise RuleError("the bad example contains no code")
    if not good or not good.strip():
        names = _eligible(btoks, 0, len(btoks))
        return _substitute(bad, btoks, 0, len(btoks), names).rstrip(":").rstrip(), None
    good = textwrap.dedent(good).strip("\n")
    gtoks, _ = tokenize_text(good)
    bs, gs = [t.string for t in btoks], [t.string for t in gtoks]
    if bs == gs:
        raise RuleError("the bad and good examples are identical")
    p = 0
    while p < min(len(bs), len(gs)) and bs[p] == gs[p]:
        p += 1
    s = 0
    while s < min(len(bs), len(gs)) - p and bs[-1 - s] == gs[-1 - s]:
        s += 1
    lo, hi = max(0, p - 1), min(len(bs), len(bs) - s + 1)
    win = _expand(btoks, lo, hi)
    shift = len(gs) - len(bs)
    glo_hi = None
    if win:
        lo, hi = win
        if btoks[lo].string in ("(", "["):
            while lo > 0 and (btoks[lo - 1].type == tokenize.NAME and not keyword.iskeyword(btoks[lo - 1].string)
                              or btoks[lo - 1].string == "."):
                lo -= 1
        ghi = hi + shift
        if 0 <= lo < ghi <= len(gtoks) and _balanced(gtoks[lo:ghi]) and hi > lo:
            glo_hi = (lo, ghi)
    if glo_hi is None:
        if not (_balanced(btoks) and _balanced(gtoks)):
            raise RuleError("the examples have unbalanced brackets; give complete statements")
        lo, hi, glo_hi = 0, len(btoks), (0, len(gtoks))
    # a name that survives into the fixed code is data flowing through, even when it has an attribute access
    names = _eligible(btoks, lo, hi, True) & _eligible(gtoks, glo_hi[0], glo_hi[1], True)
    pattern = _substitute(bad, btoks, lo, hi, names)
    template = _substitute(good, gtoks, glo_hi[0], glo_hi[1], names)
    return pattern, template
