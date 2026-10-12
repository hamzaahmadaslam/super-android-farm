# The Android image a farm actually runs: an upstream base plus the farm's overlay.
#
# `sf image build <version>` (superfarm.core.images) stages a build context holding this
# file and a folder `overlay/` with the contents of <farm home>/overlays/android/<version>/,
# then builds it. Files in the overlay land at the same path inside Android, e.g.
#
#   overlays/android/13/vendor/lib64/hw/camera.example.so  ->  /vendor/lib64/hw/camera.example.so
#
# Modes are kept as they are on disk (make binaries and scripts 0755 before building);
# everything is owned by root, which is what Android expects under /system and /vendor.
# An empty overlay is fine and gives an image identical to the base.
#
# By hand:
#   mkdir -p ctx/overlay && cp Dockerfile ctx/ && cp -a ~/overlay/. ctx/overlay/
#   docker build -t superfarm/android:13 --build-arg BASE=redroid/redroid:13.0.0-latest ctx

ARG BASE=redroid/redroid:13.0.0-latest
FROM ${BASE}

# Recorded so the farm can tell when the overlay or the base changed and a rebuild is due.
ARG BASE
ARG BASE_ID=unknown
ARG OVERLAY_HASH=none
LABEL io.superfarm.role="android-image" \
      io.superfarm.base="${BASE}" \
      io.superfarm.base-id="${BASE_ID}" \
      io.superfarm.overlay="${OVERLAY_HASH}"

COPY overlay/ /
