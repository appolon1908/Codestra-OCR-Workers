#!/usr/bin/env bash
# Refresh vendored upstream contracts from sibling checkouts, then run the contract tests.
#   SCHEMAS_REPO=../Codestra-Document-Schemas DI_REPO=../Codestra-Document-Intelligence \
#     scripts/sync_contracts.sh
# Copies the committed (HEAD) version only, never a dirty working tree.
set -euo pipefail
cd "$(dirname "$0")/.."
SCHEMAS_REPO="${SCHEMAS_REPO:-../Codestra-Document-Schemas}"
DI_REPO="${DI_REPO:-../Codestra-Document-Intelligence}"

copy() {  # repo path dest
  git -C "$1" show "HEAD:$2" > "$3"
  echo "$(git -C "$1" rev-parse --short HEAD)  $2 -> $3"
}

for name in do-driver-licence error; do
  copy "$SCHEMAS_REPO" "codestra_document_schemas/schemas/v1/$name.schema.json" \
    "contracts/codestra-document-schemas/v1/$name.schema.json"
done
for name in ocr-extract-request ocr-extraction-result; do
  copy "$DI_REPO" "app/contracts/$name.v1.schema.json" \
    "contracts/document-intelligence/v1/$name.v1.schema.json"
done
echo "Update the source commits in contracts/README.md, then run: pytest tests/unit"
