FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Avoid interactive prompts during apt-get (e.g. tzdata)
ENV DEBIAN_FRONTEND=noninteractive

# noVNC login stream dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install patchright chromium binary (NOT playwright install) — stays above the source
# copy below so this layer (and the pip install above it) stays cached across source
# edits, which is also why requirements.txt is installed separately from pyproject.toml.
RUN python -m patchright install chromium

# Allowlist, not an unrestricted whole-tree copy: these three lines are the entire
# build context this image is made from. No captures/, no data/, no .git/, no
# CLAUDE.md, no live session cookie jar — nothing outside src/mcp104/ reaches the
# image, deliberately. See .dockerignore for the layer above this one (what leaves
# the machine at all) and tests/test_ignore_files.py's
# test_dockerfile_has_no_unrestricted_copy_context for the guard against an
# unrestricted copy creeping back in.
COPY pyproject.toml ./
COPY src/ ./src/
# setuptools' `pip install --no-deps .` leaves TWO build-time artifacts behind, in
# the source tree it was invoked against, not a temp dir — neither is cleaned up on
# its own. `build/` (build/lib/mcp104/**, a second copy of the package source) shows
# up at /app. `src/mcp104.egg-info/` (metadata generation's own side effect, a
# setuptools quirk independent of editable vs. non-editable installs) shows up
# INSIDE src/ — this is NOT the host's own `pip install -e ".[dev]"` leaking through
# .dockerignore (measured: it reappears even from a host tree with no egg-info
# present at all before the build), so `.dockerignore` cannot be the fix for this
# half; only cleaning it in the same layer that creates it can. Both removed here so
# no image layer ever carries either.
RUN pip install --no-cache-dir --no-deps . && rm -rf build src/*.egg-info

EXPOSE 8080 8081

# Exec form, not shell form: shell form interposes /bin/sh, which does not forward
# SIGTERM to the Python process, so `docker compose stop`'s graceful-shutdown signal
# would never reach main()'s own `finally: await _shutdown_globals()`.
ENTRYPOINT ["mcp104"]
