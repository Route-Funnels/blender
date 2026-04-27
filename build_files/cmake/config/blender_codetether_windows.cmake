# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# CodeTether CI-oriented Windows configuration.
#
# This keeps UI/editor functionality enabled, including sculpt/edit mode support,
# but disables expensive bundled GPU kernel targets so temporary cloud Windows
# builders can produce an agent-usable blender.exe faster than a full release
# configuration.

include("${CMAKE_CURRENT_LIST_DIR}/blender_release.cmake")

set(WITH_CYCLES_CUDA_BINARIES   OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_HIP_BINARIES    OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_ONEAPI_BINARIES OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_DEVICE_OPTIX    OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_DEVICE_HIPRT    OFF CACHE BOOL "" FORCE)

# CI machines usually do not have audio or VR devices attached. These are not
# required for Python-driven model/mode automation.
set(WITH_OPENAL    OFF CACHE BOOL "" FORCE)
set(WITH_WASAPI    OFF CACHE BOOL "" FORCE)
set(WITH_XR_OPENXR OFF CACHE BOOL "" FORCE)
