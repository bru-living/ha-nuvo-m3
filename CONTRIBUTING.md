# Contributing

Issues and pull requests are welcome.

## Development

1. Fork and clone the repository.
2. Create a focused branch.
3. Keep protocol behavior isolated in `custom_components/nuvo_m3/protocol.py` where practical.
4. Add or update tests for packet parsing, command generation and menu behavior.
5. Run `python -m compileall -q custom_components tests` and `pytest`.
6. Confirm JSON files parse and do not include credentials or private track metadata.

Please describe the M3 firmware version, connection type and exact observed serial packets when reporting protocol differences.
