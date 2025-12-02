#!/usr/bin/env bash
set -euo pipefail

#
# PyKrozen Git CLI
# Usage: ./scripts/git.sh <command> [options]
#
# Commands:
#   commit [-m "message"]   Commit and push (auto-generates UUID if no message)
#   release [--major|--minor|--patch]   Create a new release (default: --patch)
#

# ────────────────────────────────────────────────────────────────
# Settings
# ────────────────────────────────────────────────────────────────

# Python command (use uv run python for virtual env support)
PYTHON_CMD="uv run python"

# Get script directory (bash-style path)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Get project root using Python for cross-platform compatibility
# We cd to script dir first, then use Python to resolve the path
PROJECT_ROOT=$(cd "$SCRIPT_DIR" && $PYTHON_CMD -c "from pathlib import Path; print(Path('.').resolve().parent)")

# ────────────────────────────────────────────────────────────────
# Colors
# ────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ────────────────────────────────────────────────────────────────
# Helper Functions
# ────────────────────────────────────────────────────────────────

print_usage() {
    echo -e "${CYAN}PyKrozen Git CLI${NC}"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  commit [-m \"message\"]          Commit and push changes"
    echo "                                   Auto-generates UUID if no message provided"
    echo ""
    echo "  release [--major|--minor|--patch]"
    echo "                                   Create a semantic version release"
    echo "                                   Runs linter and tests before releasing"
    echo "                                   Default: --patch"
    echo ""
    echo "  verify                           Verify version consistency across files"
    echo ""
    echo "Examples:"
    echo "  $0 commit                        # Commit with auto-generated UUID"
    echo "  $0 commit -m \"fix: bug fix\"    # Commit with custom message"
    echo "  $0 release --patch               # Release 1.0.0 -> 1.0.1"
    echo "  $0 release --minor               # Release 1.0.1 -> 1.1.0"
    echo "  $0 release --major               # Release 1.1.0 -> 2.0.0"
}

generate_uuid() {
    $PYTHON_CMD -c "import uuid; print(str(uuid.uuid4())[:8])"
}

# --- Version Functions (Python-based for robustness) ---

# Get current version using regex (position-independent)
get_version() {
    $PYTHON_CMD - "$PROJECT_ROOT" << 'PYTHON_SCRIPT'
import re
import sys
from pathlib import Path

project_root = Path(sys.argv[1]).resolve()
pyproject = project_root / "pyproject.toml"

content = pyproject.read_text(encoding="utf-8")

# Regex matches version = "x.y.z" anywhere in file
match = re.search(r'version\s*=\s*"(\d+\.\d+\.\d+)"', content)
if match:
    print(match.group(1))
else:
    print("0.0.0", file=sys.stderr)
    sys.exit(1)
PYTHON_SCRIPT
}

# Bump version
bump_version() {
    local current="$1"
    local bump_type="$2"

    $PYTHON_CMD << PYTHON_SCRIPT
current = "${current}"
bump_type = "${bump_type}"

major, minor, patch = map(int, current.split("."))

if bump_type == "major":
    major, minor, patch = major + 1, 0, 0
elif bump_type == "minor":
    minor, patch = minor + 1, 0
else:
    patch = patch + 1

print(f"{major}.{minor}.{patch}")
PYTHON_SCRIPT
}

# Update version in all files using regex (position-independent, future-proof)
update_all_versions() {
    local old_version="$1"
    local new_version="$2"

    echo -e "${CYAN}Updating version files...${NC}"

    $PYTHON_CMD - "$PROJECT_ROOT" "$old_version" "$new_version" << 'PYTHON_SCRIPT'
import re
import sys
from pathlib import Path

project_root = Path(sys.argv[1]).resolve()
old_ver = sys.argv[2]
new_ver = sys.argv[3]

# Define files and their patterns (regex-based, position-independent)
# Each entry: (file_path, regex_pattern, replacement_template)
# The regex captures the version, replacement uses \g<1> etc if needed
files = [
    (
        project_root / "pyproject.toml",
        r'(version\s*=\s*")' + re.escape(old_ver) + r'(")',
        r'\g<1>' + new_ver + r'\g<2>',
    ),
    (
        project_root / "src" / "pykrozen" / "__init__.py",
        r'(__version__\s*=\s*")' + re.escape(old_ver) + r'(")',
        r'\g<1>' + new_ver + r'\g<2>',
    ),
]

errors = []
for filepath, pattern, replacement in files:
    try:
        content = filepath.read_text(encoding="utf-8")

        # Check if pattern exists
        if not re.search(pattern, content):
            errors.append(f"Pattern not found in {filepath.name}")
            continue

        # Replace
        new_content = re.sub(pattern, replacement, content)
        filepath.write_text(new_content, encoding="utf-8")

        print(f"  [OK] {filepath.name}: {old_ver} -> {new_ver}")

    except FileNotFoundError:
        errors.append(f"File not found: {filepath}")
    except Exception as e:
        errors.append(f"Error in {filepath.name}: {e}")

if errors:
    for err in errors:
        print(f"  [FAIL] {err}", file=sys.stderr)
    sys.exit(1)
PYTHON_SCRIPT

    if [[ $? -ne 0 ]]; then
        echo -e "${RED}[FAIL] Version update failed${NC}"
        return 1
    fi
    echo -e "${GREEN}[OK] All version files updated${NC}"
}

# Verify all files have consistent versions
verify_versions() {
    echo -e "${CYAN}Verifying version consistency...${NC}"

    $PYTHON_CMD - "$PROJECT_ROOT" << 'PYTHON_SCRIPT'
import re
import sys
from pathlib import Path

# Use pathlib for cross-platform path handling
project_root = Path(sys.argv[1]).resolve()

# Files and their version patterns (regex-based, position-independent)
files_patterns = [
    (project_root / "pyproject.toml", r'version\s*=\s*"(\d+\.\d+\.\d+)"'),
    (project_root / "src" / "pykrozen" / "__init__.py", r'__version__\s*=\s*"(\d+\.\d+\.\d+)"'),
]

versions = {}
errors = []

for filepath, pattern in files_patterns:
    try:
        content = filepath.read_text(encoding="utf-8")

        match = re.search(pattern, content)
        if match:
            versions[str(filepath)] = match.group(1)
            print(f"  {filepath.name}: {match.group(1)}")
        else:
            errors.append(f"No version found in {filepath.name}")

    except FileNotFoundError:
        errors.append(f"File not found: {filepath}")

if errors:
    for err in errors:
        print(f"  [FAIL] {err}", file=sys.stderr)
    sys.exit(1)

unique_versions = set(versions.values())
if len(unique_versions) > 1:
    print("\n[FAIL] Version mismatch detected!", file=sys.stderr)
    sys.exit(1)

print(f"\n[OK] All files at version: {list(unique_versions)[0]}")
PYTHON_SCRIPT

    return $?
}

# --- Git Functions ---

do_commit() {
    local message="${1:-}"

    cd "$PROJECT_ROOT"

    # Check if there are changes to commit
    if git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]; then
        echo -e "${YELLOW}No changes to commit.${NC}"
        return 0
    fi

    # Generate UUID message if not provided
    if [[ -z "$message" ]]; then
        message="auto: $(generate_uuid)"
        echo -e "${BLUE}Generated commit message:${NC} $message"
    fi

    echo -e "${CYAN}Staging changes...${NC}"
    git add -A

    echo -e "${CYAN}Committing...${NC}"
    git commit -m "$message"

    echo -e "${CYAN}Pushing to remote...${NC}"
    git push

    echo -e "${GREEN}[OK] Commit and push complete!${NC}"
}

# --- Pre-release Checks ---

run_linter() {
    echo -e "${CYAN}Running linter...${NC}"
    if ! bash "$SCRIPT_DIR/linter.sh"; then
        echo -e "${RED}[FAIL] Linter failed! Fix issues before releasing.${NC}"
        return 1
    fi
    echo -e "${GREEN}[OK] Linter passed!${NC}"
}

run_tests() {
    echo -e "${CYAN}Running tests...${NC}"

    cd "$PROJECT_ROOT"

    if [[ -d "tests" ]] || [[ -f "pytest.ini" ]] || [[ -f "pyproject.toml" ]]; then
        if ! uv run pytest -v; then
            echo -e "${RED}[FAIL] Tests failed! Fix issues before releasing.${NC}"
            return 1
        fi
    else
        echo -e "${YELLOW}No pytest tests found, skipping...${NC}"
    fi

    echo -e "${GREEN}[OK] Tests passed!${NC}"
}

# --- Release Function ---

do_release() {
    local bump_type="${1:-patch}"

    cd "$PROJECT_ROOT"

    echo -e "${CYAN}==================================================================${NC}"
    echo -e "${CYAN}                    PyKrozen Release                              ${NC}"
    echo -e "${CYAN}==================================================================${NC}"
    echo ""

    # Verify versions are consistent before starting
    verify_versions || exit 1
    echo ""

    # Get current and new version
    local current_version new_version
    current_version=$(get_version)
    new_version=$(bump_version "$current_version" "$bump_type")

    echo -e "${BLUE}Current version:${NC} $current_version"
    echo -e "${BLUE}New version:${NC}     $new_version (${bump_type})"
    echo ""

    # Run pre-release checks
    echo -e "${YELLOW}Running pre-release checks...${NC}"
    echo ""

    run_linter || exit 1
    echo ""

    run_tests || exit 1
    echo ""

    echo -e "${GREEN}[OK] All checks passed!${NC}"
    echo ""

    # Update version in all files
    update_all_versions "$current_version" "$new_version" || exit 1
    echo ""

    # Commit the version bump
    do_commit "release: v$new_version"

    # Create git tag
    echo ""
    echo -e "${CYAN}Creating tag v$new_version...${NC}"
    git tag -a "v$new_version" -m "Release v$new_version"

    # Push tag
    echo -e "${CYAN}Pushing tag...${NC}"
    git push origin "v$new_version"

    echo ""
    echo -e "${GREEN}==================================================================${NC}"
    echo -e "${GREEN}[OK] Released v$new_version successfully!${NC}"
    echo -e "${GREEN}==================================================================${NC}"
}

# ────────────────────────────────────────────────────────────────
# Main CLI
# ────────────────────────────────────────────────────────────────

main() {
    if [[ $# -eq 0 ]]; then
        print_usage
        exit 0
    fi

    local command="$1"
    shift

    case "$command" in
        commit)
            local message=""
            while [[ $# -gt 0 ]]; do
                case $1 in
                    -m|--message)
                        message="$2"
                        shift 2
                        ;;
                    *)
                        echo -e "${RED}Unknown option for commit: $1${NC}"
                        exit 1
                        ;;
                esac
            done
            do_commit "$message"
            ;;
        release)
            local bump_type="patch"
            while [[ $# -gt 0 ]]; do
                case $1 in
                    --major)
                        bump_type="major"
                        shift
                        ;;
                    --minor)
                        bump_type="minor"
                        shift
                        ;;
                    --patch)
                        bump_type="patch"
                        shift
                        ;;
                    *)
                        echo -e "${RED}Unknown option for release: $1${NC}"
                        exit 1
                        ;;
                esac
            done
            do_release "$bump_type"
            ;;
        verify)
            verify_versions
            ;;
        --help|-h|help)
            print_usage
            ;;
        *)
            echo -e "${RED}Unknown command: $command${NC}"
            echo ""
            print_usage
            exit 1
            ;;
    esac
}

main "$@"
