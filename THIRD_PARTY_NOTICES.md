# Third-party notices

StrixFold UI is licensed under the Apache License 2.0 ([LICENSE](LICENSE)). It ships one thing made by others:

| What | Where | By | Licence |
| --- | --- | --- | --- |
| Inter Tight, the interface's font (a subset of version 3.004) | `mac/ui/fonts/InterTight-normal.woff2` | The Inter Project Authors, 2022 ([rsms/inter-tight](https://github.com/rsms/inter-tight)) | SIL Open Font License 1.1 ([text](LICENSES/OFL-1.1-InterTight.txt)) |

## What its set-up scripts install

These are not part of StrixFold UI. The scripts in `tools/` download them, pinned by hash, when you run them, and each comes
with its own licence: SearXNG and its Python packages (`tools/setup_agents_tools.sh`), the page parser's packages (the same),
xlrd (`tools/setup_office_reader.sh`), and mlx-vlm, its packages and the photo model (`tools/setup_look.sh`). SearXNG is AGPL-3.0
and runs as a separate program. The engines on your boxes (StrixFold, Halogen) and the models have licences of their own.
