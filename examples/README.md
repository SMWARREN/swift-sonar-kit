# Consumer recipes

## Standalone Swift package

Copy swift-package into a new directory outside this checkout and initialize Git there. Install the toolkit from the separate toolkit checkout. The sample has three XCTest cases and no external package dependencies.

    git init
    swift-sonar test --artifact build/coverage.lcov -- bash test-with-coverage.sh
    swift-sonar scan --project-key my-swift-example --source Sources --lcov build/coverage.lcov --prepare-only

The example exports only its production source from llvm-cov. On macOS it uses xcrun. On Linux it requires matching llvm-cov on PATH. The sample does not download or modify Sonar. To publish, configure SONAR_TOKEN/SONAR_HOST_URL and omit --prepare-only.

## Existing Xcode projects

Keep your actual xcodebuild destination, schemes, test plans and native result verification. Wrap your checked-in test script with swift-sonar test. Exit codes must reflect failures. A single wrapper command can execute iOS, macOS and UI suites sequentially, but this toolkit does not infer completeness or reconcile native test identities in v0.1.

For an existing multi-language Sonar pipeline, prefer the offline issues/coverage commands. Point your reviewed configuration at their outputs rather than replacing it with the focused Swift-only scan command. Keep independent test-result validation.

## HashFleet

HashFleet is the first real consumer. Its separate Tools/swift-sonar-kit.sh entry point delegates to this checkout or an explicitly configured SWIFT_SONAR_KIT_ROOT. It does not replace the existing exhaustive-test/xcresult pipeline or change the app. Real HashFleet reports validate adapter interoperability. Do not copy private reports or credentials into this repository.
