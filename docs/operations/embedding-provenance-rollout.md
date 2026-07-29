# Embedding provenance rollout (obsolete)

This procedure is obsolete after PR-A (#24) and must not be executed. No embedding
generation rollout is authorized until the replacement runbook has completed its
three-party review.

The former procedure was removed because it contradicted the activation gate in three
safety-critical ways:

1. It deleted legacy chunks before activation and post-activation verification. Legacy
   chunks must remain available through that verification boundary.
2. It activated a `building` generation with an inline Python snippet. Activation accepts
   only a generation that validation has sealed and whose receipt, checksum, and manifest
   revision can be rechecked atomically.
3. It offered raw SQL rollback that bypassed those receipt, checksum, manifest, profile,
   and locking contracts.

Recovery after activation is forward-only: build, validate, seal, and activate a new
generation. Do not reactivate retired generations or change lifecycle state with raw SQL.
The removed commands remain available in Git history for audit purposes only; they are not
an executable fallback.
