# Attribution and source provenance

FlowX's original upstream is [AIpRoBuilder/FlowX](https://github.com/AIpRoBuilder/FlowX).
Its public project identifier is **urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466**.
This identifier is deliberately the same for everyone: it identifies the project,
not a downloader, fork owner, device, or coding-agent user.

## What is included

- [NOTICE](../NOTICE): readable upstream attribution, included alongside the
  license in source distributions and wheels.
- SPDX and `FlowX-Origin` comments in the core builder and adapter boundary files.
  These are visible, searchable markers, not hidden control logic.
- [provenance.py](../packages/core/flowx_core/provenance.py): importable origin
  metadata; the core package exposes `__origin_id__` and
  `__upstream_repository__` without running network or tracking code.
- [AGENTS.md](../AGENTS.md): attribution-preservation guidance for cooperating
  coding agents. An agent may ignore instructions; this is not enforcement.
- [source-manifest.json](../provenance/source-manifest.json): a snapshot of
  SHA-256 fingerprints for selected upstream files, not the entire repository.
- [tools/provenance.py](../tools/provenance.py): a standard-library-only, offline
  comparison tool. It never imports or executes the candidate project's source.

No telemetry, IP/device collection, license server, network beacon, kill switch,
or restriction on lawful forks is added. Origin markers can be removed; no
source-code watermark survives every rewrite or reliably identifies a person.
Generated workflow code is not automatically marked as owned by FlowX.

## Compare a copy offline

From a trusted FlowX checkout, use the tool's `verify` command. Its `--root`
option points to the copy you want to examine and `--manifest` selects the
upstream snapshot. The script emits JSON on stdout and does not modify the copy.

```bash
# Compare this checkout against the published snapshot
python3.10 tools/provenance.py verify

# Compare a separate, lawfully obtained source tree against a trusted snapshot
python3.10 tools/provenance.py verify --root /path/to/copy --manifest provenance/source-manifest.json

# Print a proposed new snapshot for review, without changing any file
python3.10 tools/provenance.py snapshot
```

For a simple marker search in an accessible source tree, look for `FlowX-Origin`
or the full public identifier above. Neither marker proves who copied a file.
Comparison reports include expected and actual raw SHA-256 values; keep the
original files and the exact trusted manifest alongside any report.

Statuses are:

| Status | Meaning |
| --- | --- |
| `exact` | The file bytes match the snapshot's SHA-256. |
| `python_ast_match` | File bytes differ, but Python syntax structure matches after comments and formatting are ignored. |
| `changed` | Neither comparison matches. Rewrites or renamed/moved files can evade comparison. |
| `missing` | The file is absent at the expected path. |

The Python-AST comparison is a similarity heuristic, not semantic equivalence or
proof of copying. It is enabled only when the checker uses the same Python
major/minor version recorded in the snapshot. It preserves identifiers, literals,
and docstrings, so substantial edits will not match. A clean-room implementation
of common code can also resemble an AST. Third-party/public-domain content may
match without being owned by FlowX.

Exit codes are **0** for all exact matches, **1** for changed/missing/AST-only
matches, and **2** for invalid input or an unreadable snapshot. An exit code of
1 is not an infringement finding and is expected for legitimate modifications.

The `snapshot` command prints a new manifest to stdout; it does not overwrite
the published snapshot. Create and review a new release snapshot deliberately.
The manifest excludes itself to avoid circular hashes and does not record
personal identity, the current date, or a claimed trusted publication time.
Regular unit tests do not require all working-tree files to remain byte-identical
to this historical snapshot; legitimate development is not blocked.

## Apache 2.0 still permits copying

FlowX remains licensed under [Apache 2.0](../LICENSE). Compliant copying,
modification, forking, and commercial redistribution are permitted. Section 4
requires a license copy, notices of changed files, retention of applicable source
attribution, and readable NOTICE attribution when redistributing relevant work.
NOTICE is informational and adds no new restriction. Dependencies retain their
own licenses. This document is technical guidance, not legal advice.

## Preserve evidence, not just a watermark

1. Keep version history, dated public releases, original design/source records,
   and contributor records. Do not fabricate, backdate, or overwrite evidence.
2. Publish release-specific snapshots and sign the exact manifest or release
   with a key you control, using Git/GPG/Sigstore as appropriate. This change
   does **not** create a signature or establish an independently trusted time.
3. Retain the signed artifact, signing identity/public-key record, and immutable
   release archive. A trusted timestamp or transparency log can strengthen the
   publication record; a local SHA-256 alone cannot prove who wrote code first.
4. If examining suspected infringement, retain an unmodified copy, source URL,
   retrieval time, license/notice content, and a reproducible comparison report.
   Do not bypass access controls or secretly track the suspected user's device.
5. Consult qualified counsel about ownership, license compliance, jurisdiction,
   registration requirements, and evidence preservation. No watermark, manifest,
   or matching code guarantees a successful lawsuit or identifies the copier.