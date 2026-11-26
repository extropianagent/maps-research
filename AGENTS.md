# maps-research

A personal Google Maps research toolkit. The owner asks an AI (Claude, ChatGPT,
Grok) for places in plain language; the AI runs the `maps` CLI in this repo and
reports. There is no app and no server: the CLI and the files here are the product.

## When the owner asks for places

Read `skills/maps-research/SKILL.md` first, then `knowledge/preferences.md`.
The skill is the research method; a bare `maps search` is only its first step.

Run the CLI as `uv run maps …`. `uv run maps --help` and each command's `--help`
state what the command does and what it costs.

## Rules no config will tell you

- **Free-first.** Never pass `--cost quality` unless the owner's request
  authorizes spending and gives a ceiling. When the budget guard stops a search,
  report what was covered and what was not. A refusal from the guard is a result.
- **Maps content is not kept.** Search results live in the state directory
  (outside the repo) and expire. Commit place IDs, Maps links and the owner's own
  notes to `knowledge/`; never review text, addresses or rating tables. Read
  `docs/permissions.md` before storing, exporting or sharing results, and before
  enabling Apify or DataForSEO.
- **Never claim coverage the run did not achieve.** Every search reports
  `coverage`; carry its gaps into what you tell the owner.
- **Secrets stay out of files.** Credentials come from the environment
  (`docs/setup.md`). `.env*` is unreadable here on purpose.

## Changing the code

- Failing test first, then the code. `make check` (lint + tests) must pass
  before a commit.
- A change to `maps/retrieval.py` or `maps/scoring.py` is an experiment: follow
  `evals/README.md` and log it in `evals/experiments.md`, kept or reverted.
- A new retrieval source implements the contract in `maps/providers/base.py` and
  charges `Budget` before each billable request. Prices live only in
  `maps/pricing.py`.
- API field names that have not yet been confirmed against a live call are
  listed in `docs/live-verification.md`. When a live call confirms or refutes
  one, update that file and the code together.

## After a search worth remembering

Write a case study (`knowledge/case-studies/README.md` has the template) and add
its one-line lesson to `knowledge/search-lessons.md`.
