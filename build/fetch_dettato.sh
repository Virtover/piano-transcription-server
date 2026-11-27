#!/bin/sh
# Fetch dettato's weights (dettato-weights.txt) into $1, each file checked against its sha256, and check that every
# safetensors file parses (tensors only; it holds no code). Used by Dockerfile.worker and Dockerfile.runpod.
set -eu
out="$1"
# Each line: repo revision file sha256 [copies...]; the copies (URLs) are tried in turn after Hugging Face.
grep -v '^#' /build/dettato-weights.txt | while read -r repo revision file sha256 copies; do
    [ -n "$repo" ] || continue
    dir="$out/${repo#*/}"
    mkdir -p "$dir"
    # shellcheck disable=SC2086
    python3 /build/fetch.py --sha256 "$sha256" --out "$dir/$file" "https://huggingface.co/$repo/resolve/$revision/$file" $copies
done
python3 -c "
import pathlib, sys
from safetensors import safe_open
for p in sorted(pathlib.Path(sys.argv[1]).glob('*/model.safetensors')):
    with safe_open(str(p), 'pt') as f:
        assert len(list(f.keys())) > 0, p
" "$out"
