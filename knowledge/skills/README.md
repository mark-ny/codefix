# Skills

Each `*.json` file in this folder is a **skill**: a named bundle of declarative rules that CodeFix has
learned. Skills here are loaded automatically (unless disabled with `codefix skill disable NAME`).

Create them with `codefix skill learn FILE.json`, the web page (*Skills & knowledge*), or by letting your AI
agent follow `AGENT_README.md`. Ready-made examples live in `docs/examples/skills/`.
The format is described by `knowledge/skill.schema.json`.
