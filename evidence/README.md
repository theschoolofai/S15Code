# Evidence artefacts

Raw output backing every number in the main README's "Evidence" section, so a
reviewer can check the claims rather than take them on trust.

- `proofs/*.json` — machine-readable result of each proof: every check, every
  ledger row, every Jaeger trace ID, and the full per-task breakdown for p1.
- `logs/*.log` — the console output of the run that produced them.

`gateway.log` is deliberately not included: it is server-side noise and the one
file most likely to have captured something that should not be published.

These are generated files. Regenerate them with the commands in §7 of the main
README; `proofs/out/` is gitignored precisely because it is scratch, and this
directory is a point-in-time copy taken for the submission.
