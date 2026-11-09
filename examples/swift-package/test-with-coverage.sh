#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
swift test --enable-code-coverage
bin="$(swift build --show-bin-path)"
mkdir -p build
bundles=("$bin"/*.xctest)
if [[ ${#bundles[@]} -ne 1 || ! -e "${bundles[0]}" ]]; then
  echo "Expected one test product. Use a clean sample build directory." >&2
  exit 1
fi
if [[ "$(uname -s)" == Darwin ]]; then
  name="$(basename "${bundles[0]}" .xctest)"
  binary="${bundles[0]}/Contents/MacOS/$name"
  cov=(xcrun llvm-cov)
else
  binary="${bundles[0]}"
  cov=(llvm-cov)
fi
"${cov[@]}" export --format=lcov --instr-profile "$bin/codecov/default.profdata" \
  "$binary" Sources/Greeting/Greeting.swift > build/coverage.lcov
