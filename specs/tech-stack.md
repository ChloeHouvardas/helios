# Tech Stack

## Decided
- **Language:** Python (version TBD)
- **Repo layout:** monorepo — `platform/`, `services/`, `apps/`
- **Folder naming:** kebab-case (`agent-toolkit/`); Python package names inside folders use snake_case (hyphens can't be imported)
- **Model:** Qwen (the only generative model allowed for development)
- **Agent framework:** [Qwen-Agent](https://github.com/QwenLM/Qwen-Agent) as the base for the Agent Toolkit runtime
- **Secrets:** API keys are read from environment variables and never hard-coded or logged

## Tentative
- **Testing framework:** pytest
- **Action log storage:** append-only JSONL files
- Package/build tooling, web framework, data storage — to be selected during implementation.
