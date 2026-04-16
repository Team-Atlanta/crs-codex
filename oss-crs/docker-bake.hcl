# =============================================================================
# crs-codex Docker Bake Configuration
# =============================================================================
#
# Builds the CRS base image with Codex CLI and Python dependencies.
#
# Usage:
#   docker buildx bake prepare
#   docker buildx bake --push prepare   # Push to registry
# =============================================================================

variable "REGISTRY" {
  default = "ghcr.io/team-atlanta"
}

variable "VERSION" {
  default = "cli-0.47.0"
}

variable "CODEX_CLI_VERSION" {
  default = "0.47.0"
}

function "tags" {
  params = [name]
  result = [
    "${REGISTRY}/${name}:${VERSION}",
    "${name}:${VERSION}"
  ]
}

# -----------------------------------------------------------------------------
# Groups
# -----------------------------------------------------------------------------

group "default" {
  targets = ["prepare"]
}

group "prepare" {
  targets = ["codex-base"]
}

# -----------------------------------------------------------------------------
# Base Image
# -----------------------------------------------------------------------------

target "codex-base" {
  context    = "."
  dockerfile = "oss-crs/base.Dockerfile"
  tags       = tags("codex-base")
  args = {
    CODEX_CLI_VERSION = CODEX_CLI_VERSION
  }
}
