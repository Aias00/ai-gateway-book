#!/bin/sh
# EPUBCHECK_JAR may point to an existing EPUBCheck 5.3+ installation.
set -eu
cd "$(dirname "$0")/.."
slug=$(python3 -c 'import sys; sys.path.insert(0,"scripts"); import build; print(build.metadata()["slug"])')
epubcheck_jar=${EPUBCHECK_JAR:-.tools/epubcheck-5.4.0/epubcheck.jar}
if [ ! -f "$epubcheck_jar" ]; then
  mkdir -p .tools
  curl --fail --location --retry 3 --silent --show-error \
    https://github.com/w3c/epubcheck/releases/download/v5.4.0/epubcheck-5.4.0.zip \
    --output .tools/epubcheck.zip
  unzip -q -o .tools/epubcheck.zip -d .tools
fi
report=site/downloads/epubcheck.txt
if "${JAVA_BIN:-java}" -jar "$epubcheck_jar" --failonwarnings "site/downloads/$slug.epub" > "$report" 2>&1; then
  cat "$report"
else
  cat "$report" >&2
  exit 1
fi
python3 -c 'import hashlib,sys; print("Validated EPUB SHA-256:",hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
  "site/downloads/$slug.epub" >> "$report"
