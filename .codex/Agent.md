# Agent


## Working agreements

Always preserve async integrity when modifying Python services.

Never introduce blocking calls inside async routes or background workers.

Run relevant tests after modifying backend logic.

Keep changes minimal and scoped to the requested task.

Do not refactor unrelated modules.

## Context
- Repository: `voice-agent`
- Main branch: `main`

## Notes
- Keep secrets out of git-tracked files.
- Use `.env` for local environment variables.
