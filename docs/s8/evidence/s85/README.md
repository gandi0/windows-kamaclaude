# S8.5 evidence index

All paths in this directory were produced on Windows 11 with the repository Python 3.12.14.

- `preflight-s84/`: S8.4 targeted rerun before S8.5 changes.
- `preflight-full/`: full preflight; Ruff exits 1 for 28 inherited diagnostics.
- `matrix-first/`: first unified 20-scenario matrix and raw pytest output.
- `benchmark-smoke/`: two-sample harness smoke test; not used for conclusions.
- `benchmark-final/`: first 15-sample run; retained because initial/final WAL states differed.
- `benchmark-final-v2/`: authoritative 15-sample run with comparable initial/final WAL checkpoints.
- `demo-final/`: final real daemon/SocketClient/Textual Pilot evidence.
- `final-full/`, `matrix-final/`, `static-final/`: final validation gates.
- `protected-files.json`: SHA-256 comparison against the S8.3 protected-file record.
- `platforms.json`: actual Windows, WSL2, and Python 3.13 availability/validation status.

No evidence here uses a real model key or paid API. Temporary profiles and workload data are distinct from
the repository `.test-tmp` directory and the user's real session database.
