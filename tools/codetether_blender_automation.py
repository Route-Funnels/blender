#!/usr/bin/env python3
"""
Small Blender automation entrypoint for CodeTether agents.

Run this with Blender's bundled Python, not regular system Python:

    blender --background input.blend --python tools/codetether_blender_automation.py -- \
        --mode SCULPT --output output.blend

Examples:

    # Enter sculpt mode for the active mesh and save the file.
    blender --background model.blend --python tools/codetether_blender_automation.py -- \
        --mode SCULPT --output model_sculpt.blend

    # Add a UV sphere and save.
    blender --background --python tools/codetether_blender_automation.py -- \
        --add-primitive uv_sphere --name AgentSphere --output scene.blend

    # Run arbitrary Python after context setup.
    blender --background model.blend --python tools/codetether_blender_automation.py -- \
        --mode OBJECT --exec "bpy.context.object.location.x += 1" --output model.blend

Notes:
- This script can switch Blender data modes and modify scene/model data.
- It cannot make this shell magically provide a Blender UI. Agents need a Blender
  executable installed or built, and they should run it in background mode for
  deterministic automation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bpy


def _argv_after_double_dash() -> list[str]:
    """Return arguments passed after Blender's `--` separator."""
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1 :]


def _ensure_active_mesh(name: str | None = None) -> bpy.types.Object:
    """Find/select an active mesh object, optionally by name."""
    obj = bpy.data.objects.get(name) if name else bpy.context.view_layer.objects.active
    if obj is None or obj.type != "MESH":
        obj = next((candidate for candidate in bpy.context.scene.objects if candidate.type == "MESH"), None)
    if obj is None:
        raise RuntimeError("No mesh object found. Create or load a mesh before switching to sculpt/edit modes.")

    bpy.ops.object.mode_set(mode="OBJECT") if bpy.ops.object.mode_set.poll() else None
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return obj


def _add_primitive(kind: str, name: str | None) -> bpy.types.Object:
    kind = kind.lower()
    if kind == "cube":
        bpy.ops.mesh.primitive_cube_add()
    elif kind in {"uv_sphere", "sphere"}:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32)
    elif kind == "ico_sphere":
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4)
    elif kind == "plane":
        bpy.ops.mesh.primitive_plane_add()
    elif kind == "monkey":
        bpy.ops.mesh.primitive_monkey_add()
    else:
        raise ValueError(f"Unsupported primitive {kind!r}. Use cube, uv_sphere, ico_sphere, plane, or monkey.")

    obj = bpy.context.object
    if name:
        obj.name = name
        obj.data.name = f"{name}Mesh"
    return obj


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate simple Blender scene/model updates.")
    parser.add_argument("--object", dest="object_name", help="Mesh object name to activate.")
    parser.add_argument("--mode", choices=("OBJECT", "EDIT", "SCULPT"), help="Mode to switch the active mesh to.")
    parser.add_argument("--add-primitive", choices=("cube", "uv_sphere", "sphere", "ico_sphere", "plane", "monkey"))
    parser.add_argument("--name", help="Name for a newly added primitive.")
    parser.add_argument("--exec", dest="exec_code", help="Python code to execute with bpy imported.")
    parser.add_argument("--output", help="Path to save the resulting .blend file.")
    return parser.parse_args(_argv_after_double_dash())


def main() -> None:
    args = parse_args()

    obj = None
    if args.add_primitive:
        obj = _add_primitive(args.add_primitive, args.name)

    if args.mode:
        obj = _ensure_active_mesh(args.object_name or (obj.name if obj else None))
        bpy.ops.object.mode_set(mode=args.mode)
        print(f"Active object {obj.name!r} is now in {args.mode} mode")

    if args.exec_code:
        namespace = {"bpy": bpy, "Path": Path}
        exec(args.exec_code, namespace, namespace)

    if args.output:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(args.output).resolve()))
        print(f"Saved Blender file to {args.output}")


if __name__ == "__main__":
    main()
