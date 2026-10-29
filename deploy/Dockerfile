# syntax=docker/dockerfile:1
# One image for both roles: `steward api` and `steward worker`.
# Base images are pinned by digest; bump them deliberately.

ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:5dde09372dfa8e7762892b2a12619fcf6c23b0b78bce1058a9fa5a3a7a40e68e
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
COPY examples/hello-module/pyproject.toml examples/hello-module/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-install-workspace

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Collect license texts of everything shipped in the image into /licenses.
RUN mkdir -p /licenses/python \
 && cp LICENSE NOTICE THIRD_PARTY_NOTICES.md /licenses/ \
 && cp "$(python -c 'import sys; print(sys.base_prefix)')/lib/python3.13/LICENSE.txt" /licenses/python/CPython.txt \
 && /opt/venv/bin/python - <<'EOF'
import importlib.metadata as md, pathlib, shutil
KEYS = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")
for dist in md.distributions():
    for f in dist.files or []:
        parts = f.parts
        # Files named like a license, and everything in a licenses/ folder of the
        # dist-info (pypdfium2 ships the licenses of PDFium's dependencies there).
        in_folder = next((i for i, p in enumerate(parts) if p.upper() in ("LICENSES", "LICENCES")), None)
        if in_folder is not None and parts[0].endswith(".dist-info"):
            rel = pathlib.Path(*parts[in_folder:])
        elif any(k in f.name.upper() for k in KEYS):
            rel = pathlib.Path(f.name)
        else:
            continue
        dest = pathlib.Path("/licenses/python", dist.metadata["Name"], rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(f.locate(), dest)
EOF

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="steward-ai" \
      org.opencontainers.image.description="A self-hosted AI steward for the files on your server" \
      org.opencontainers.image.licenses="Apache-2.0"
# OCR (the `documents` module): Tesseract with English and script-detection data. More
# languages: install `tesseract-ocr-<lang>` in an image built from this one, or mount
# trained data and set TESSDATA_PREFIX. The copyright files of every Debian package added
# here go to /licenses/debian/.
RUN dpkg-query -W -f '${Package}\n' | sort > /tmp/before \
 && apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /licenses/debian \
 && dpkg-query -W -f '${Package}\n' | sort | comm -13 /tmp/before - > /licenses/debian/PACKAGES \
 && while read -r pkg; do \
      if [ -f "/usr/share/doc/$pkg/copyright" ]; then \
        cp "/usr/share/doc/$pkg/copyright" "/licenses/debian/$pkg.copyright"; \
      fi; \
    done < /licenses/debian/PACKAGES \
 && rm /tmp/before
COPY --from=build /opt/venv /opt/venv
COPY --from=build /licenses /licenses
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER 1000:1000
EXPOSE 8420
CMD ["steward", "--help"]
