# Core Execution Rules for lush-givex-worker
1. Read `AI_CONTEXT.md` and `spec/.github/SPEC-6-Native-AI-Workflow.md` for architectural context.
2. Concurrency: Always use `threading.Lock` for shared resources. No exceptions.
3. State Machine: Strictly adhere to `ALLOWED_STATES`. Do not invent new states.
4. Architecture: Zero cross-module imports allowed. Maintain strict module isolation.
