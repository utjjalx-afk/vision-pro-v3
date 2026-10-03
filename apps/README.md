# App boundaries

| Boundary | Current behavior | Future role |
| --- | --- | --- |
| API | Offline diagnostic placeholder | Health, control, and read APIs |
| Worker | Offline diagnostic placeholder | Feed/research orchestration |
| Dashboard | Offline diagnostic placeholder | User-facing research and monitoring UI |
| MT5 bridge | Documentation only | Isolated Windows bridge; execution disabled |

All current container entry points run `python -m vision --component <name>` and exit.
There is no HTTP service, UI, network bridge, or order endpoint in Phase-0.
