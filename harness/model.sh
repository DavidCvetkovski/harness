#!/usr/bin/env bash
#
# model.sh — start the local model server (llama.cpp + Metal).
#
# Stopping and status live in the CLI: `harness stop`, `harness status`.
#
# Usage:  harness model [-c CTX] [-p PORT] [-m MODEL] [--temp N] [--f16-cache]

set -euo pipefail

LLAMA_BIN="$HOME/llm/llama.cpp/build/bin/llama-server"
MODELS_DIR="$HOME/llm/models"
CHOICE_FILE="$HOME/.harness/model"    # written when you pick a model in settings

HOST=127.0.0.1
PORT=8080
KV_TYPE=q8_0        # quantized KV cache; --f16-cache restores the default

# Left empty so the per-model profile further down can supply the default.
# Anything passed on the command line is filled in here first and wins.
CTX=""
TEMP=""
TOP_P=""
TOP_K=""

MODEL=""
[[ -f "$CHOICE_FILE" ]] && MODEL="$(cat "$CHOICE_FILE")"

usage() {
    cat <<'EOF'
harness model — run the local model server

  harness model                 start on 127.0.0.1:8080
  harness model -c 8192         smaller context, less memory
  harness model -p 8090         different port

Context and sampling default to a per-model profile (see PROFILES below);
anything given here overrides it.

Options:
  -c CTX       context window in tokens
  -m MODEL     path to a .gguf, overriding the one chosen in settings
  -p PORT      port (default 8080)
  --temp N     sampling temperature
  --f16-cache  full-precision KV cache (about double the memory)
  -h, --help   this text
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -c)          CTX="$2"; shift 2 ;;
        -m)          MODEL="$2"; shift 2 ;;
        -p)          PORT="$2"; shift 2 ;;
        --temp)      TEMP="$2"; shift 2 ;;
        --f16-cache) KV_TYPE=""; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

# Fall back to whatever is on disk, so a fresh install still starts.
if [[ ! -f "$MODEL" ]]; then
    MODEL="$(ls -1 "$MODELS_DIR"/*.gguf 2>/dev/null | head -1 || true)"
fi

# Preflight — fail with something actionable rather than a stack trace.
if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "llama-server not found at: $LLAMA_BIN" >&2
    echo "Rebuild with:" >&2
    echo "  cd ~/llm/llama.cpp && cmake --build build --target llama-server -j\$(sysctl -n hw.ncpu)" >&2
    exit 1
fi

if [[ ! -f "$MODEL" ]]; then
    echo "No model found in: $MODELS_DIR" >&2
    echo "Download one with:" >&2
    echo "  ~/llm/.venv/bin/hf download owao/Nanbeige4.2-3B-GGUF nanbeige4.2-3b-Q4_K_M.gguf --local-dir $MODELS_DIR" >&2
    exit 1
fi

# Match the process NAME exactly (-x), not the full command line: matching with
# -f also matches any shell whose command contains "llama-server" — including
# the terminal you launched this from — which made the script report a phantom
# server and refuse to start. pgrep exits 1 when nothing matches, which pipefail
# would turn into a script-wide exit, so swallow that.
if existing=$(lsof -ti :"$PORT" 2>/dev/null | head -1 || true) \
   && [[ -n "$existing" ]]; then
    echo "Port $PORT is already in use (pid $existing) on http://${HOST}:${PORT}"
    echo "Stop it first with: harness stop"
    exit 1
fi

# PROFILES — what each model costs per token of context, the context to open
# with, and the sampling its card asks for. Matched on the file name, case
# insensitively, because quant file names are not consistently cased.
#
# The sampling set here is only a fallback for callers that talk to
# llama-server directly: harness.agent sends temperature/top_p/top_k from
# config.json on every request. MODEL_PROFILES in config.py is the table that
# governs chat — keep the two in step.
shopt -s nocasematch
case "$(basename "$MODEL")" in
    nanbeige*)
        # A Looped Transformer (num_loops=2), so the KV cache is allocated for
        # 44 effective layer passes rather than the 22 physical ones — about
        # double what the layer count suggests. Measured at f16.
        KV_KB_PER_TOKEN=167.5 ; DEF_CTX=16384
        DEF_TEMP=0.6 ; DEF_TOP_P=0.95 ; DEF_TOP_K=20
        ;;
    qwen3.8-27b*)
        # Hybrid attention: only 16 of the 64 layers keep a KV cache (4 KV
        # heads x 256 dim), the other 48 are Gated DeltaNet carrying a
        # fixed-size recurrent state instead. 16 x 4 x 256 x 2 (K and V) x 2
        # bytes = 64 KB/token at f16, so context is unusually cheap here —
        # it is the 9 GB of weights that make this one tight, not the cache.
        # 8192 was too small in practice: a couple of file reads plus a
        # reasoning turn filled it and llama-server truncated mid-thought,
        # which surfaces as an empty reply rather than a short one. Context is
        # cheap on this architecture (~32 KB/token at q8_0), so 32K costs only
        # about 0.75 GB more than 8K — but that lands ~10.4 GB against a
        # ~10.7 GB Metal budget, so this is the practical ceiling on 16 GB.
        KV_KB_PER_TOKEN=64.0 ; DEF_CTX=32768
        # Qwen's instruct (non-thinking) recommendation. Reasoning turns want
        # 1.0 / 0.95 instead — config.py carries that as `when_thinking` and
        # the agent swaps it in per request, which these flags cannot do.
        DEF_TEMP=0.7 ; DEF_TOP_P=0.80 ; DEF_TOP_K=20
        ;;
    *)
        KV_KB_PER_TOKEN=80.0 ; DEF_CTX=16384
        DEF_TEMP=0.6 ; DEF_TOP_P=0.95 ; DEF_TOP_K=20
        ;;
esac
shopt -u nocasematch

CTX="${CTX:-$DEF_CTX}"
TEMP="${TEMP:-$DEF_TEMP}"
TOP_P="${TOP_P:-$DEF_TOP_P}"
TOP_K="${TOP_K:-$DEF_TOP_K}"
kv_scale=1.0
[[ -n "$KV_TYPE" ]] && kv_scale=0.5
est_gb=$(awk -v bytes="$(stat -f%z "$MODEL")" -v c="$CTX" \
             -v k="$KV_KB_PER_TOKEN" -v s="$kv_scale" \
    'BEGIN { printf "%.1f", bytes/1e9 + 0.4 + (c * k * s) / 1048576 }')

echo "$(basename "$MODEL" .gguf)  |  ${CTX} ctx  |  ~${est_gb} GB  |  http://${HOST}:${PORT}"

# Warn before the machine starts swapping rather than after. The ceiling that
# matters is Metal's, not the machine's: with -ngl 99 the weights and the whole
# KV cache have to fit in the GPU working set, which defaults to about
# two-thirds of physical RAM when iogpu.wired_limit_mb is 0.
#
# Measured on a MacBook Air M5 / 16 GB with nanbeige-3b: 16K ctx -> 5.27 GB RSS
# and 29% free (comfortable), 32K ctx -> 7.95 GB RSS and 15% free (thrashes).
# Keying the warning off the estimate rather than off a fixed context size is
# what makes it useful for a 9 GB model, which is tight at any context at all.
ram_gb=$(awk -v b="$(sysctl -n hw.memsize)" 'BEGIN { printf "%.1f", b/1073741824 }')
wired_mb=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
if [[ "${wired_mb:-0}" -gt 0 ]]; then
    budget_gb=$(awk -v m="$wired_mb" 'BEGIN { printf "%.1f", m/1024 }')
else
    budget_gb=$(awk -v r="$ram_gb" 'BEGIN { printf "%.1f", r*2/3 }')
fi

if awk -v e="$est_gb" -v b="$budget_gb" 'BEGIN { exit !(e > b) }'; then
    echo
    echo "Warning: needs ~${est_gb} GB but Metal will only wire ~${budget_gb} GB of"
    echo "this machine's ${ram_gb} GB. Expect a failed allocation or swap thrashing."
    echo "Use a smaller -c, or raise the limit until reboot with:"
    echo "  sudo sysctl iogpu.wired_limit_mb=<megabytes>"
    echo "Ctrl-C now if that wasn't deliberate."
    echo
elif awk -v e="$est_gb" -v b="$budget_gb" 'BEGIN { exit !(e > b * 0.85) }'; then
    echo
    echo "Note: ~${est_gb} GB against a ~${budget_gb} GB Metal budget — close to the"
    echo "edge. Close other apps if generation stalls."
    echo
fi

echo "Ctrl-C to stop."
echo

# Bound to loopback deliberately: the server sets permissive CORS and no API
# key, so it must not be exposed on 0.0.0.0.
exec "$LLAMA_BIN" \
    -m "$MODEL" \
    --host "$HOST" \
    --port "$PORT" \
    -ngl 99 \
    -c "$CTX" \
    -fa on \
    ${KV_TYPE:+-ctk "$KV_TYPE" -ctv "$KV_TYPE"} \
    --temp "$TEMP" \
    --top-p "$TOP_P" \
    --top-k "$TOP_K"
