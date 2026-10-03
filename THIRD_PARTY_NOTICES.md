# Third-party notices

Phase-0 source was authored afresh. No trading engine, strategy, adapter, old
Vision Pro source, or third-party repository history has been copied or vendored.
The architecture draws on general event-driven, intent/risk separation,
connector-boundary, replay, and research-governance ideas discussed in the design.
Referencing these ideas does not imply incorporating those projects' code.

The LICENSE file is the unmodified Apache License 2.0 text from
<https://www.apache.org/licenses/LICENSE-2.0.txt>.

Runtime uses Python's standard library. Development/build tools are installed
separately rather than vendored:

| Tool | Upstream | License |
| --- | --- | --- |
| Python | https://www.python.org/ | PSF License |
| Hatchling | https://github.com/pypa/hatch | MIT |
| pytest | https://github.com/pytest-dev/pytest | MIT |
| Ruff | https://github.com/astral-sh/ruff | MIT |
| jsonschema | https://github.com/python-jsonschema/jsonschema | MIT |

CI uses GitHub-maintained checkout/setup-python actions. Container scaffolding
references the official Python image. Those tools and images retain their own
licenses and transitive notices; they are not relicensed by this repository.
Review and extend this inventory before adding or distributing dependencies.
