# Platform defaults single source

Pattern:

- Put platform-specific install/runtime/env/service defaults behind a small shared application model.
- Let installer docs, setup diagnostics, and service rendering consume that model instead of repeating path rules in each caller.
- Keep destructive or privileged actions outside setup/install; return exact commands for the operator to review.

Why it works:

Linux and macOS differ mostly in service manager, runtime root, env-file path, and prerequisite hints. A shared profile keeps those differences visible without creating separate installer flows or letting secrets/runtime state leak into repo-local defaults.

