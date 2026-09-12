# Manifests

- `snapshot-2026-09-10.json` contains the public cutoff blocks, hashes, state
  roots, policy identifiers, and dependency revisions.
- `source-code.sha256` identifies the public, nonignored source package.
- `results.sha256` identifies ignored private findings and compact results
  during the embargo.
- `releases/2026-09-11.json` identifies larger ignored release artifacts.

Private result and release entries identify both their domain
(`claim-accounting` or `destination-mapping`) and evidence classification.

Regenerate the public source manifest with:

```sh
make manifest
```

Local maintainers regenerate private result identities with:

```sh
make private-manifest
```

Result and release manifests remain ignored until the release conditions in
`docs/numerical-embargo.md` are met.
