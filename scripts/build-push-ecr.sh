#!/bin/bash
# =============================================================================
# Build and Push idblu-tts Docker Image to ECR with Semantic Versioning
# =============================================================================
# Usage: ./scripts/build-push-ecr.sh
#
# The script will:
#   1. Fetch current version from ECR
#   2. Prompt for version increment (patch/minor/major/dev)
#   3. Prompt for release notes (official releases only)
#   4. Create git tag with release notes (official releases only)
#   5. Build and push Docker image (cross-platform for linux/amd64)
#   6. Push git tag to remote (official releases only)
#
# Version Types:
#   - patch/minor/major: Official releases (e.g., 0.1.1)
#   - dev: Test builds with commit hash (e.g., 0.1.1-dev.abc1234)
#
# Note: Builds with --platform linux/amd64 for compatibility with the shared TTS ASG.
# =============================================================================

set -e

AWS_REGION="${AWS_REGION:-ca-central-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-449678530532}"
ECR_REPOSITORY="${ECR_REPOSITORY:-idblu-tts}"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-docker/Dockerfile.idblu_tts}"
PLATFORM="${PLATFORM:-linux/amd64}"

ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

CUSTOM_TAG=""
YES=false

usage() {
  cat <<'EOF'
Usage: ./scripts/build-push-ecr.sh [--tag TAG] [--yes]

Options:
  --tag TAG   Build and push with an explicit tag, bypassing semantic-version prompts.
  --yes       Skip confirmation prompts where possible.
  -h, --help  Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      CUSTOM_TAG="${2:-}"
      shift 2
      ;;
    --yes|-y)
      YES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo -e "${RED}Unknown option: $1${NC}"
      usage
      exit 1
      ;;
  esac
done

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}   ID-BLU TTS - Build and Push to ECR${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""

echo -e "${BLUE}[1/7] Checking git status...${NC}"

if ! git rev-parse --git-dir &>/dev/null; then
  echo -e "${RED}✗ Not a git repository${NC}"
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo -e "${RED}✗ AWS CLI is required${NC}"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo -e "${RED}✗ Docker is required${NC}"
  exit 1
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo -e "${RED}✗ Docker buildx is required${NC}"
  exit 1
fi

if [[ ! -f "$DOCKERFILE_PATH" ]]; then
  echo -e "${RED}✗ Dockerfile not found: ${DOCKERFILE_PATH}${NC}"
  exit 1
fi

if ! git diff-index --quiet HEAD -- 2>/dev/null; then
  echo -e "${YELLOW}⚠ You have uncommitted changes:${NC}"
  git status --short
  echo ""
  if [[ "$YES" != true ]]; then
    read -p "$(echo -e ${YELLOW}Continue anyway? [y/N]: ${NC})" continue_dirty
    if [[ ! "$continue_dirty" =~ ^[Yy] ]]; then
      echo -e "${RED}Aborted. Please commit your changes first.${NC}"
      exit 1
    fi
  fi
fi

CURRENT_BRANCH=$(git branch --show-current)
CURRENT_COMMIT=$(git rev-parse --short HEAD)
FULL_COMMIT=$(git rev-parse HEAD)
echo -e "${GREEN}✓ Branch: ${CURRENT_BRANCH}${NC}"
echo -e "${GREEN}✓ Commit: ${CURRENT_COMMIT}${NC}"
echo ""

echo -e "${BLUE}[2/7] Authenticating with ECR...${NC}"
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${ECR_REGISTRY}" 2>/dev/null
echo -e "${GREEN}✓ Successfully authenticated with ECR${NC}"
echo ""

echo -e "${BLUE}[3/7] Checking ECR repository...${NC}"
if aws ecr describe-repositories --repository-names "${ECR_REPOSITORY}" --region "${AWS_REGION}" &>/dev/null; then
  echo -e "${GREEN}✓ Repository '${ECR_REPOSITORY}' exists${NC}"
else
  echo -e "${YELLOW}Creating repository '${ECR_REPOSITORY}'...${NC}"
  aws ecr create-repository \
    --repository-name "${ECR_REPOSITORY}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --encryption-configuration encryptionType=AES256 >/dev/null
  echo -e "${GREEN}✓ Repository created${NC}"
fi
echo ""

echo -e "${BLUE}[4/7] Fetching current version from ECR...${NC}"
TAGS=$(aws ecr describe-images \
  --repository-name "${ECR_REPOSITORY}" \
  --region "${AWS_REGION}" \
  --query 'imageDetails[*].imageTags[*]' \
  --output text 2>/dev/null | tr '\t' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V || echo "")

if [ -z "$TAGS" ]; then
  CURRENT_VERSION="0.0.0"
  echo -e "${YELLOW}No existing versions found. Starting from 0.1.0${NC}"
else
  CURRENT_VERSION=$(echo "$TAGS" | tail -1)
  echo -e "${GREEN}✓ Current version: ${BOLD}${CURRENT_VERSION}${NC}"
fi

IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"
echo ""

IS_DEV_BUILD=false
NEW_VERSION=""
INCREMENT_TYPE=""
RELEASE_NOTES=""

if [ -n "$CUSTOM_TAG" ]; then
  NEW_VERSION="$CUSTOM_TAG"
  INCREMENT_TYPE="custom"
  if [[ "$NEW_VERSION" == "latest" ]]; then
    echo -e "${RED}Refusing to use 'latest' as the primary deployment tag.${NC}"
    exit 1
  fi
  if [[ "$NEW_VERSION" =~ -dev\. ]]; then
    IS_DEV_BUILD=true
  fi
  echo -e "${GREEN}✓ Using explicit tag: ${BOLD}${NEW_VERSION}${NC}"
  echo ""
  echo -e "${BLUE}[5/7] Skipping semantic version selection (explicit tag provided)${NC}"
  echo ""
  echo -e "${BLUE}[6/7] Skipping release notes (explicit tag mode)${NC}"
  echo ""
else
  echo -e "${BLUE}[5/7] Select version increment:${NC}"
  echo ""

  NEW_PATCH="${MAJOR}.${MINOR}.$((PATCH + 1))"
  NEW_MINOR="${MAJOR}.$((MINOR + 1)).0"
  NEW_MAJOR="$((MAJOR + 1)).0.0"
  NEW_DEV="${MAJOR}.${MINOR}.$((PATCH + 1))-dev.${CURRENT_COMMIT}"

  if [ "$CURRENT_VERSION" == "0.0.0" ]; then
    NEW_PATCH="0.1.0"
    NEW_MINOR="0.1.0"
    NEW_MAJOR="1.0.0"
    NEW_DEV="0.1.0-dev.${CURRENT_COMMIT}"
  fi

  echo -e "  ${CYAN}1)${NC} patch  → ${GREEN}${NEW_PATCH}${NC}  (bug fixes, small changes)"
  echo -e "  ${CYAN}2)${NC} minor  → ${GREEN}${NEW_MINOR}${NC}  (new features, backwards compatible)"
  echo -e "  ${CYAN}3)${NC} major  → ${GREEN}${NEW_MAJOR}${NC}  (breaking changes)"
  echo -e "  ${CYAN}4)${NC} dev    → ${MAGENTA}${NEW_DEV}${NC}  (test build, no git tag)"
  echo ""

  while true; do
    read -p "$(echo -e ${YELLOW}Select increment type [1-4]: ${NC})" choice
    case $choice in
      1)
        NEW_VERSION="$NEW_PATCH"
        INCREMENT_TYPE="patch"
        break
        ;;
      2)
        NEW_VERSION="$NEW_MINOR"
        INCREMENT_TYPE="minor"
        break
        ;;
      3)
        NEW_VERSION="$NEW_MAJOR"
        INCREMENT_TYPE="major"
        break
        ;;
      4)
        NEW_VERSION="$NEW_DEV"
        INCREMENT_TYPE="dev"
        IS_DEV_BUILD=true
        break
        ;;
      *)
        echo -e "${RED}Invalid choice. Please enter 1, 2, 3, or 4.${NC}"
        ;;
    esac
  done

  echo ""
  if [ "$IS_DEV_BUILD" = true ]; then
    echo -e "${MAGENTA}✓ Selected: ${INCREMENT_TYPE} → ${BOLD}${NEW_VERSION}${NC}"
    echo -e "${DIM}  (Test build - no git tag will be created)${NC}"
  else
    echo -e "${GREEN}✓ Selected: ${INCREMENT_TYPE} → ${BOLD}${NEW_VERSION}${NC}"
  fi
  echo ""

  if [ "$IS_DEV_BUILD" = false ]; then
    echo -e "${BLUE}[6/7] Release notes:${NC}"
    echo ""

    LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
    if [ -n "$LAST_TAG" ]; then
      RECENT_COMMITS=$(git log --oneline "${LAST_TAG}..HEAD" --pretty=format:"  - %s" 2>/dev/null | head -10)
      echo -e "${DIM}Commits since ${LAST_TAG}:${NC}"
    else
      RECENT_COMMITS=$(git log --oneline -5 --pretty=format:"  - %s" 2>/dev/null)
      echo -e "${DIM}Recent commits:${NC}"
    fi

    if [ -n "$RECENT_COMMITS" ]; then
      echo -e "${DIM}${RECENT_COMMITS}${NC}"
    else
      RECENT_COMMITS="  - Release ${NEW_VERSION}"
      echo -e "${DIM}${RECENT_COMMITS}${NC}"
    fi
    echo ""

    echo -e "${YELLOW}Enter release notes (press Enter to use commits above, or type custom notes):${NC}"
    echo -e "${DIM}(For multi-line notes, end each line with \\ or leave empty when done)${NC}"
    read -p "> " CUSTOM_NOTES

    if [ -n "$CUSTOM_NOTES" ]; then
      RELEASE_NOTES="$CUSTOM_NOTES"
    else
      RELEASE_NOTES=$(echo "$RECENT_COMMITS" | sed 's/^  - /- /')
    fi

    echo ""
    echo -e "${GREEN}✓ Release notes set${NC}"
    echo ""
  else
    echo -e "${BLUE}[6/7] Skipping release notes (dev build)${NC}"
    echo ""
  fi
fi

echo -e "${YELLOW}Summary:${NC}"
echo -e "  Version:   ${BOLD}${NEW_VERSION}${NC}"
echo -e "  Branch:    ${CURRENT_BRANCH}"
echo -e "  Commit:    ${CURRENT_COMMIT}"
echo -e "  Platform:  ${CYAN}${PLATFORM}${NC} (shared TTS compatible)"
if [ "$IS_DEV_BUILD" = true ]; then
  echo -e "  Type:      ${MAGENTA}Development/Test Build${NC}"
  echo -e "  Git Tag:   ${DIM}(none - dev/custom dev builds are not tagged)${NC}"
  echo -e "  Latest:    ${DIM}(not updated - preserves production/shared runtime)${NC}"
elif [ "$INCREMENT_TYPE" = "custom" ]; then
  echo -e "  Type:      ${CYAN}Custom Tag Build${NC}"
  echo -e "  Git Tag:   ${DIM}(not created in custom mode)${NC}"
  echo -e "  Latest:    ${DIM}(not updated in custom mode)${NC}"
else
  echo -e "  Type:      ${GREEN}Official Release${NC}"
  echo -e "  Notes:"
  echo "$RELEASE_NOTES" | sed 's/^/    /'
fi
echo ""

if [[ "$YES" != true ]]; then
  read -p "$(echo -e ${YELLOW}Proceed with build? [Y/n]: ${NC})" confirm
  if [[ "$confirm" =~ ^[Nn] ]]; then
    echo -e "${RED}Build cancelled.${NC}"
    exit 0
  fi
  echo ""
fi

FULL_IMAGE_URI="${ECR_REGISTRY}/${ECR_REPOSITORY}:${NEW_VERSION}"

echo -e "${BLUE}[7/7] Building and pushing...${NC}"
echo ""
echo -e "${CYAN}Building Docker image for ${PLATFORM}...${NC}"
echo -e "${DIM}(This may take a while for the first build)${NC}"

if [ "$IS_DEV_BUILD" = true ] || [ "$INCREMENT_TYPE" = "custom" ]; then
  docker buildx build \
    --platform "${PLATFORM}" \
    -f "${DOCKERFILE_PATH}" \
    --build-arg "FORK_COMMIT=${FULL_COMMIT}" \
    --build-arg "WRAPPER_VERSION=${NEW_VERSION}" \
    -t "${FULL_IMAGE_URI}" \
    --push \
    .
else
  docker buildx build \
    --platform "${PLATFORM}" \
    -f "${DOCKERFILE_PATH}" \
    --build-arg "FORK_COMMIT=${FULL_COMMIT}" \
    --build-arg "WRAPPER_VERSION=${NEW_VERSION}" \
    -t "${FULL_IMAGE_URI}" \
    -t "${ECR_REGISTRY}/${ECR_REPOSITORY}:latest" \
    --push \
    .
fi

echo -e "${GREEN}✓ Docker image built successfully${NC}"
echo ""
echo -e "${GREEN}✓ Pushed ${NEW_VERSION}${NC}"
if [ "$IS_DEV_BUILD" = false ] && [ "$INCREMENT_TYPE" != "custom" ]; then
  echo -e "${GREEN}✓ Pushed latest${NC}"
fi
echo ""

if [ "$IS_DEV_BUILD" = false ] && [ "$INCREMENT_TYPE" != "custom" ]; then
  echo -e "${CYAN}Creating git tag...${NC}"
  TAG_MESSAGE="Release ${NEW_VERSION}

${RELEASE_NOTES}

Commit: ${CURRENT_COMMIT}
Branch: ${CURRENT_BRANCH}
Image:  ${FULL_IMAGE_URI}"

  git tag -a "v${NEW_VERSION}" -m "$TAG_MESSAGE"
  echo -e "${GREEN}✓ Created tag v${NEW_VERSION}${NC}"

  echo -e "${CYAN}Pushing git tag to remote...${NC}"
  git push origin "v${NEW_VERSION}"
  echo -e "${GREEN}✓ Pushed tag to origin${NC}"
  echo ""
fi

echo -e "${BLUE}================================================${NC}"
echo -e "${GREEN}   Build Complete!${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""
echo -e "${YELLOW}Image:${NC}     ${FULL_IMAGE_URI}"
if [ "$IS_DEV_BUILD" = false ] && [ "$INCREMENT_TYPE" != "custom" ]; then
  echo -e "${YELLOW}Latest:${NC}    ${ECR_REGISTRY}/${ECR_REPOSITORY}:latest"
  echo -e "${YELLOW}Git Tag:${NC}   v${NEW_VERSION}"
fi
echo -e "${YELLOW}Branch:${NC}    ${CURRENT_BRANCH}"
echo -e "${YELLOW}Commit:${NC}    ${FULL_COMMIT}"
echo ""
echo "Image pushed: ${FULL_IMAGE_URI}"
