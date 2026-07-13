# Cardinal Cursor plugin

> [!NOTE]
> This repository is a **release mirror**. Development happens in
> [cardinal-agent-plugins](https://github.com/cardinalhq/cardinal-agent-plugins) — send PRs there.


This repository publishes one Cursor plugin: `cardinal-cursor-plugin`.

The plugin source lives at [`plugins/cardinal-cursor-plugin`](./plugins/cardinal-cursor-plugin); the parity spec against the Codex and Claude plugins lives at [`docs/specs/cursor-parity.md`](./docs/specs/cursor-parity.md).

## Install

Clone this repository and run `cardinal-connect` from the plugin's `scripts/` directory:

```bash
git clone https://github.com/cardinalhq/cardinal-cursor-plugin.git ~/workspace/cardinal-cursor-plugin
python3 ~/workspace/cardinal-cursor-plugin/plugins/cardinal-cursor-plugin/scripts/cardinal-connect
```

The connect script runs Cardinal's browser-approved device-code flow, writes a managed `mcpServers.cardinal` entry to `~/.cursor/mcp.json`, and installs Cardinal telemetry hooks in `~/.cursor/hooks.json`. For Cursor cloud-agent coverage, add `--project` inside your repo to also write `.cursor/mcp.json` and `.cursor/hooks.json` at the repo root.

See the plugin README for full behavior and options:

- [`plugins/cardinal-cursor-plugin/README.md`](./plugins/cardinal-cursor-plugin/README.md)

## License

Apache 2.0. See [LICENSE](./LICENSE).
