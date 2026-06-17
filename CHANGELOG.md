# CHANGELOG

## v0.1.0 (2026-05-18)

### Features

- Initial dectl project
  ([`33ce5cf`](https://github.com/datapointchris/dectl/commit/33ce5cfd8de4394698014cc1477d002f65656196))

Config-driven CLI for managing AWS data engineering pipelines. Consolidates repeated justfile
  operations (Glue deploy/run/logs, Lambda deploy/invoke/logs) into a single tool with YAML config
  at ~/.config/dectl/config.yaml.
