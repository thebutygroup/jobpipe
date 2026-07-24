"""The application flow (see docs/APPLY-FLOW-PLAN.md).

One aggregate — Application — composed of an Applicant (profile + asset
vault) and a Job (posting + ApplyRoute). Platform-specific behaviour lives in
platforms/, behind a registry, mirroring the source-adapter pattern.
"""
