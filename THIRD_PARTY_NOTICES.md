# Third-party notices and primary references

This repository contains the toolkit, tests and a small original Swift example. It does not bundle SonarQube, SonarScanner, SwiftLint, sonar-apple, Xcode, or LLVM. Their names identify independent interoperability targets, not endorsement.

The toolkit is GPL-3.0-only, consistent with the originating HashFleet workflow. The GPL text in LICENSE is preserved unmodified.

Primary sources checked October 31, 2026:

- Sonar official Swift availability: https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/languages/swift
- Generic external issue schema: https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/importing-external-issues/generic-issue-import-format
- Generic coverage schema: https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/test-coverage/generic-test-data
- Community Apple plugin: https://github.com/insideapp-fr/sonar-apple
- Plugin license, LGPL-3.0-or-later: https://github.com/insideapp-fr/sonar-apple/blob/main/LICENSE.md
- Plugin SwiftLint runner behavior: https://github.com/insideapp-fr/sonar-apple/blob/main/swift-lang/src/main/java/fr/insideapp/sonarqube/swift/issues/swiftlint/runner/SwiftLintRunner.java
- SwiftLint and analyzer-mode documentation: https://github.com/realm/SwiftLint
- LLVM LCOV export: https://llvm.org/docs/CommandGuide/llvm-cov.html

Current official documentation lists Swift analysis starting with Developer Edition. Our free path is a third-party integration, not that analyzer. Sonar generic external issues are managed by their originating analyzer, not Sonar quality profiles. The plugin still supplies language registration and its own sensors.
