#!/bin/bash
#
# PyKrozen Test Runner
# Runs the complete test suite with a single command
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Default values
CONCURRENCY=${CONCURRENCY:-50}
DURATION=${DURATION:-5}
PYTHON=${PYTHON:-python}
HOST=${HOST:-127.0.0.1}
HTTP_PORT=${HTTP_PORT:-8000}
WS_PORT=${WS_PORT:-8000}

print_banner() {
    echo -e "${CYAN}"
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║                  PyKrozen Test Runner                         ║"
    echo "║              One Command to Test Them All                     ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -c, --concurrency NUM   Number of concurrent workers (default: 50)"
    echo "  -d, --duration SEC      Test duration in seconds (default: 5)"
    echo "  -p, --python PATH       Python executable (default: python)"
    echo "  -h, --host HOST         Server host (default: 127.0.0.1)"
    echo "  --http-port PORT        HTTP port (default: 8000)"
    echo "  --ws-port PORT          WebSocket port (default: 8765)"
    echo "  --help                  Show this help message"
    echo ""
    echo "Environment variables:"
    echo "  CONCURRENCY, DURATION, PYTHON, HOST, HTTP_PORT, WS_PORT"
    echo ""
    echo "Examples:"
    echo "  $0                      # Run with defaults"
    echo "  $0 -c 100 -d 10         # 100 workers, 10 seconds"
    echo "  $0 --python python3     # Use python3"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -c|--concurrency)
            CONCURRENCY="$2"
            shift 2
            ;;
        -d|--duration)
            DURATION="$2"
            shift 2
            ;;
        -p|--python)
            PYTHON="$2"
            shift 2
            ;;
        -h|--host)
            HOST="$2"
            shift 2
            ;;
        --http-port)
            HTTP_PORT="$2"
            shift 2
            ;;
        --ws-port)
            WS_PORT="$2"
            shift 2
            ;;
        --help)
            print_usage
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            print_usage
            exit 1
            ;;
    esac
done

print_banner

echo -e "${BLUE}Configuration:${NC}"
echo -e "  Concurrency: ${YELLOW}$CONCURRENCY${NC}"
echo -e "  Duration:    ${YELLOW}${DURATION}s${NC}"
echo -e "  Python:      ${YELLOW}$PYTHON${NC}"
echo -e "  Host:        ${YELLOW}$HOST${NC}"
echo -e "  HTTP Port:   ${YELLOW}$HTTP_PORT${NC}"
echo -e "  WS Port:     ${YELLOW}$WS_PORT${NC}"
echo ""

# Check if we're in the right directory
if [ ! -d "super-tests" ]; then
    echo -e "${RED}Error: super-tests directory not found.${NC}"
    echo "Please run this script from the pykrozen root directory."
    exit 1
fi

cd super-tests

# Check if Rust tester is built
TESTER_BIN="target/release/super-tests"
if [ ! -f "$TESTER_BIN" ]; then
    echo -e "${YELLOW}Building test suite...${NC}"
    cargo build --release
    echo -e "${GREEN}Build complete!${NC}"
    echo ""
else
    echo -e "${GREEN}Using cached build:${NC} $TESTER_BIN"
    echo ""
fi

# Run the auto test
echo -e "${CYAN}Starting automated test suite...${NC}"
echo ""

./target/release/super-tests \
    --host "$HOST" \
    --http-port "$HTTP_PORT" \
    --ws-port "$WS_PORT" \
    auto \
    --concurrency "$CONCURRENCY" \
    --duration "$DURATION" \
    --python "$PYTHON"

EXIT_CODE=$?

cd ..

# Find and display the report in performance folder
REPORT=$(ls -t super-tests/performance/performance_report_*.md 2>/dev/null | head -1)
if [ -n "$REPORT" ]; then
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}Report generated: ${YELLOW}$REPORT${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
fi

exit $EXIT_CODE
