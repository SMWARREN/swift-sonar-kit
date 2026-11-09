# Contributing

Use Python 3.11 or newer. Runtime code uses only the standard library. Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and package/entry-point smoke tests.

Keep adapters deterministic. Add malformed-input, path-boundary, token-redaction, failure-propagation and source-freshness tests for changes. Never skip acceptance checks to obtain a green run. Keep fake-executable tests separate from real server evidence. Preserve third-party attribution and GPL-3.0-only licensing.

Scope improvements into independently owned tasks. Review actual worker output, not model reputation. A failed or timed-out swarm worker is not an independent approval.

Roadmap: native xcresult identity census and generic test execution import, explicit SwiftLint analyze support, optional Periphery/compiler diagnostics, supported-version compatibility fixtures, and deduplication strategy for native versus external SwiftLint imports. These are not implemented capabilities.
