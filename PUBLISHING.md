# Publishing

1. Update the version in `custom_components/nuvo_m3/manifest.json`, the artwork User-Agent and `CHANGELOG.md`.
2. Run local compilation, JSON validation and tests.
3. Commit and push the release changes.
4. Create and push a matching `vX.Y.Z` tag.
5. The release workflow builds `nuvo_m3-vX.Y.Z.zip` and creates the GitHub release.
6. Confirm HACS and Hassfest validation pass before announcing the release.
