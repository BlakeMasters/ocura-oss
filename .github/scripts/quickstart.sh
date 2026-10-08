#!/usr/bin/env bash
# Run the README's workflow against the installed package, as a user would type it.
set -euo pipefail

work="$(mktemp -d)"
cd "$work"

field() { python -c "import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$1" "$2"; }

ocura-oss init --name example --json > init.json
ocura-oss run --json --param count=1 -- python -c "print(1)" > baseline.json
chokepoint="$(field baseline.json chokepoint_id)"
ocura-oss branch --json --from "$chokepoint" --reason "try count 2" --param count=2 > branch.json
pathway="$(field branch.json id)"
ocura-oss run --json --pathway "$pathway" --param count=2 -- python -c "print(2)" > child.json
ocura-oss run --json --param batch=4 --substitute -- python -c "import sys; print(sys.argv[1])" "{batch}" > substituted.json
test "$(tr -d '\r\n' < "$(field substituted.json stdout_log)")" = "4"
ocura-oss verify --json > verify.json
test "$(field verify.json status)" = "ok"
ocura-oss compare --json --from "$chokepoint" > compare.json
test "$(field compare.json state)" = "ready"
ocura-oss manifest > retained.manifest
ocura-oss verify --against retained.manifest
ocura-oss demo --root "$work/ocura-oss-demo"
echo "quickstart passed in $work"
