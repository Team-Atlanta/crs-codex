# =============================================================================
# atlantis-codex Docker Bake Configuration
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
  default = "latest"
}

function "tags" {
  params = [name]
  result = [
    "${REGISTRY}/${name}:${VERSION}",
    "${REGISTRY}/${name}:latest",
    "${name}:latest"
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
}
