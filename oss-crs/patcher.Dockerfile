# =============================================================================
# crs-codex Patcher Module
# =============================================================================
# RUN phase: Receives POVs, generates patches using Codex,
# tests them using the snapshot image for incremental rebuilds.
#
# Uses host Docker socket (mounted by framework) to access snapshot images.
# =============================================================================

# These ARGs are required by the oss-crs framework template
ARG target_base_image
ARG crs_version

FROM codex-base

# Install libCRS (CLI + Python package)
COPY --from=libcrs . /libCRS
RUN pip3 install /libCRS \
    && python3 -c "from libCRS.base import DataType; print('libCRS OK')"

# Install crs-codex package (patcher + agents)
COPY pyproject.toml /opt/crs-codex/pyproject.toml
COPY patcher.py /opt/crs-codex/patcher.py
COPY agents/ /opt/crs-codex/agents/
RUN pip3 install /opt/crs-codex

CMD ["run_patcher"]
