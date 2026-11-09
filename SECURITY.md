# Security

Never commit SONAR_TOKEN, raw private reports, .env files, scanner caches or consumer source in this repository. Use a least-privilege analysis token through the environment. The test subprocess does not receive SONAR_TOKEN. Preflight never follows redirects and requires HTTPS except explicit loopback HTTP. TLS verification remains enabled.

Only scan publishes source and analysis to the chosen server. Doctor is authenticated read-only, converters and prepare-only are local. SonarScanner and installed server plugins are trusted external programs with their own networking and token handling. The toolkit cannot sandbox them. Do not run untrusted project test scripts, SwiftLint configuration, executables or plugins with secrets available.

Receipts detect accidental stale/changed inputs, not malicious edits by someone controlling the checkout. They cover Git-visible files, not ignored dependencies or external toolchain state. Pin dependencies/toolchains in your own CI. Tests must exit nonzero on failure.

Before public release: review the staged file list and history, run tests and secret checks, verify third-party download checksums yourself, and test supported server/plugin versions. No downloader or bootstrap server is included. Report vulnerabilities privately to the repository maintainer rather than posting tokens or private source.
