# Getting Started

This page is the shortest path to running the public local agent tools.

## 1. Install

```bash
git clone <repo-url> options-monitor
cd options-monitor
bash scripts/install_agent_plugin.sh
```

To install a tagged pre-release instead of `main`:

```bash
git clone --branch v0.1.0-beta.1 --depth 1 <repo-url> options-monitor
cd options-monitor
bash scripts/install_agent_plugin.sh
```

## 2. Prepare runtime config

Use one of:

- copy `configs/examples/config.example.us.json` to `config.us.json`
- set `OM_CONFIG_DIR` to a directory containing `config.us.json` / `config.hk.json`
- set `OM_CONFIG_US` / `OM_CONFIG_HK` directly

If your runtime config references `portfolio.pm_config` relatively, it will be resolved
relative to the runtime config file.

## 3. Inspect public tool manifest

```bash
./om-agent spec
```

## 4. Run a basic healthcheck

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

## 5. Run one read-only tool

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

## 6. Optional environment variables

- `OM_CONFIG_DIR`: directory containing runtime configs
- `OM_CONFIG_US`: explicit US config path
- `OM_CONFIG_HK`: explicit HK config path
- `OM_PM_CONFIG`: explicit portfolio secret config path
- `OM_OUTPUT_DIR`: override plugin output/cache directory
- `OM_AGENT_ENABLE_WRITE_TOOLS=true`: allow non-dry-run `manage_symbols` writes
