"""
Retarget FBX animations (e.g. Mixamo) onto a specific Roblox rig exported from Studio
(e.g. Workspace.Animator) using Blender headless, generating Roblox .rbxmx KeyframeSequence files
that match Roblox Studio's Animation Editor ("Imported Rig" / "Zaimportowany szkielet") 1:1.

Usage (inside Blender headless):
    blender --background --factory-startup --python retarget_fbx_animations.py -- ^
        --rig rig_export.json ^
        --input "C:\Users\Jakub\Downloads\anims" ^
        --output "animations_retargeted" ^
        --prefix Npc

To export rig_export.json from Roblox Studio (run in Command Bar or via MCP execute_luau):
    local model = workspace:FindFirstChild("Animator")
    local rootPart = model:FindFirstChild("RootPart") or model:FindFirstChild("HumanoidRootPart") or model.PrimaryPart
    local rootInv = rootPart.CFrame:Inverse()
    local bones = {}
    local function collect(inst)
        for _, c in ipairs(inst:GetChildren()) do
            if c:IsA("Bone") then
                local relCF = rootInv * c.WorldCFrame
                table.insert(bones, {
                    name = c.Name,
                    parent = c.Parent.Name,
                    localCF = { c.CFrame:GetComponents() },
                    modelCF = { relCF:GetComponents() },
                })
                collect(c)
            end
        end
    end
    collect(rootPart)
    return game:GetService("HttpService"):JSONEncode({ rootName = rootPart.Name, bones = bones })
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

# Mixamo FBX armature space (+X=Left, +Y=Up, +Z=Forward) -> Roblox RootPart space (-X=Left, +Y=Up, -Z=Forward)
C_CONV_180 = Matrix((
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, -1.0),
))
C_CONV_ID = Matrix.Identity(3)


def cframe_components_to_rot_pos(cf):
    pos = Vector((cf[0], cf[1], cf[2]))
    rot = Matrix((
        (cf[3], cf[4], cf[5]),
        (cf[6], cf[7], cf[8]),
        (cf[9], cf[10], cf[11]),
    ))
    return rot, pos


def fbx_stem_to_anim_name(stem: str, prefix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", stem).strip()
    parts = [p[0].upper() + p[1:] for p in cleaned.split() if p]
    return prefix + "".join(parts)


def load_animator_rig(rig_json_path: Path):
    raw = rig_json_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, str):
        data = json.loads(data)

    root_name = data["rootName"]
    bones = {}
    children = {root_name: []}
    order = []

    for b in data["bones"]:
        name = b["name"]
        parent = b["parent"]
        model_rot, model_pos = cframe_components_to_rot_pos(b["modelCF"])
        local_rot, local_pos = cframe_components_to_rot_pos(b["localCF"])
        bones[name] = {
            "name": name,
            "parent": parent,
            "model_rot": model_rot,
            "model_rot_inv": model_rot.transposed(),
            "model_pos": model_pos,
            "local_rot": local_rot,
            "local_pos": local_pos,
        }
        children.setdefault(parent, []).append(name)
        children.setdefault(name, [])
        order.append(name)

    # Auto-detect facing direction from LeftArm X sign in RootPart space:
    # Mixamo FBX has LeftArm at +X; if Roblox rig has LeftArm at -X, use Ry(180).
    left_arm = next((b for n, b in bones.items() if "LeftArm" in n), None)
    c_conv = C_CONV_180 if (left_arm and left_arm["model_pos"].x < 0) else C_CONV_ID

    return root_name, bones, children, order, c_conv


def add_pose_xml(parent_el, name, pos, rot, children_map, poses_for_frame, ref_counter):
    pose_el = ET.SubElement(parent_el, "Item", {"class": "Pose", "referent": f"RBX{ref_counter[0]:08X}"})
    ref_counter[0] += 1
    props = ET.SubElement(pose_el, "Properties")

    cf_el = ET.SubElement(props, "CoordinateFrame", {"name": "CFrame"})
    ET.SubElement(cf_el, "X").text = f"{pos.x:.6f}"
    ET.SubElement(cf_el, "Y").text = f"{pos.y:.6f}"
    ET.SubElement(cf_el, "Z").text = f"{pos.z:.6f}"
    ET.SubElement(cf_el, "R00").text = f"{rot[0][0]:.6f}"
    ET.SubElement(cf_el, "R01").text = f"{rot[0][1]:.6f}"
    ET.SubElement(cf_el, "R02").text = f"{rot[0][2]:.6f}"
    ET.SubElement(cf_el, "R10").text = f"{rot[1][0]:.6f}"
    ET.SubElement(cf_el, "R11").text = f"{rot[1][1]:.6f}"
    ET.SubElement(cf_el, "R12").text = f"{rot[1][2]:.6f}"
    ET.SubElement(cf_el, "R20").text = f"{rot[2][0]:.6f}"
    ET.SubElement(cf_el, "R21").text = f"{rot[2][1]:.6f}"
    ET.SubElement(cf_el, "R22").text = f"{rot[2][2]:.6f}"

    ET.SubElement(props, "token", {"name": "EasingDirection"}).text = "0"
    ET.SubElement(props, "token", {"name": "EasingStyle"}).text = "0"
    ET.SubElement(props, "string", {"name": "Name"}).text = name
    ET.SubElement(props, "float", {"name": "Weight"}).text = "1"

    for child_name in children_map.get(name, []):
        c_pos, c_rot = poses_for_frame[child_name]
        add_pose_xml(pose_el, child_name, c_pos, c_rot, children_map, poses_for_frame, ref_counter)


def write_rbxmx(out_path: Path, anim_name: str, loop: bool, priority: int, frames_data, root_name, children_map):
    roblox = ET.Element("roblox", {
        "xmlns:xmime": "http://www.w3.org/2005/05/xmlmime",
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:noNamespaceSchemaLocation": "http://www.roblox.com/roblox.xsd",
        "version": "4",
    })
    ET.SubElement(roblox, "External").text = "null"
    ET.SubElement(roblox, "External").text = "nil"

    ref_counter = [1]
    kfs = ET.SubElement(roblox, "Item", {"class": "KeyframeSequence", "referent": f"RBX{ref_counter[0]:08X}"})
    ref_counter[0] += 1
    kfs_props = ET.SubElement(kfs, "Properties")
    ET.SubElement(kfs_props, "bool", {"name": "Loop"}).text = "true" if loop else "false"
    ET.SubElement(kfs_props, "string", {"name": "Name"}).text = anim_name
    ET.SubElement(kfs_props, "token", {"name": "Priority"}).text = str(priority)

    id_rot = Matrix.Identity(3)
    zero_pos = Vector((0.0, 0.0, 0.0))

    for t, poses_for_frame in frames_data:
        kf = ET.SubElement(kfs, "Item", {"class": "Keyframe", "referent": f"RBX{ref_counter[0]:08X}"})
        ref_counter[0] += 1
        kf_props = ET.SubElement(kf, "Properties")
        ET.SubElement(kf_props, "string", {"name": "Name"}).text = "Keyframe"
        ET.SubElement(kf_props, "float", {"name": "Time"}).text = f"{t:.6f}"
        add_pose_xml(kf, root_name, zero_pos, id_rot, children_map, poses_for_frame, ref_counter)

    tree = ET.ElementTree(roblox)
    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=False)


def convert_fbx(fbx_path: Path, out_dir: Path, prefix: str, loop: bool, priority: int, root_name, rbx_bones, children_map, order, c_conv):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=str(fbx_path))

    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if not armatures:
        raise RuntimeError(f"No armature in {fbx_path}")
    arm = armatures[0]
    if not arm.animation_data or not arm.animation_data.action:
        raise RuntimeError(f"No action in {fbx_path}")

    act = arm.animation_data.action
    f_start = int(round(act.frame_range[0]))
    f_end = int(round(act.frame_range[1]))
    fps = bpy.context.scene.render.fps or 30

    c_conv_inv = c_conv.transposed()
    fbx_rest_rot_inv = {b.name: b.matrix_local.to_3x3().normalized().transposed() for b in arm.data.bones}

    hips_name = next((n for n in order if n.endswith("Hips")), None)
    hips_fbx_rest = arm.data.bones.get(hips_name) if hips_name else None
    hips_fbx_rest_pos = hips_fbx_rest.matrix_local.translation.copy() if hips_fbx_rest else Vector((0, 0, 0))

    frames_data = []
    id_rot = Matrix.Identity(3)
    zero_pos = Vector((0.0, 0.0, 0.0))

    for frame in range(f_start, f_end + 1):
        bpy.context.scene.frame_set(frame)
        t = (frame - f_start) / float(fps)

        delta_rbx = {root_name: id_rot}
        for name in order:
            pb = arm.pose.bones.get(name)
            if pb is not None and name in fbx_rest_rot_inv:
                d_fbx = pb.matrix.to_3x3().normalized() @ fbx_rest_rot_inv[name]
                delta_rbx[name] = c_conv @ d_fbx @ c_conv_inv
            else:
                delta_rbx[name] = delta_rbx.get(rbx_bones[name]["parent"], id_rot)

        poses_for_frame = {}
        for name in order:
            binfo = rbx_bones[name]
            d_parent_inv = delta_rbx[binfo["parent"]].transposed()
            d_bone = delta_rbx[name]
            r_pose = binfo["model_rot_inv"] @ d_parent_inv @ d_bone @ binfo["model_rot"]

            pos_pose = zero_pos
            if name == hips_name and hips_fbx_rest is not None:
                pb_hips = arm.pose.bones.get(hips_name)
                if pb_hips is not None:
                    # Matches Roblox Animation Editor ("Imported Rig" / "Zaimportowany szkielet") 1:1
                    d_pos_rbx = c_conv @ (pb_hips.matrix.translation - hips_fbx_rest_pos)
                    pos_pose = binfo["model_rot_inv"] @ d_pos_rbx

            poses_for_frame[name] = (pos_pose, r_pose)

        frames_data.append((t, poses_for_frame))

    anim_name = fbx_stem_to_anim_name(fbx_path.stem, prefix)
    out_path = out_dir / f"{anim_name}.rbxmx"
    write_rbxmx(out_path, anim_name, loop, priority, frames_data, root_name, children_map)
    print(f"OK {fbx_path.name} -> {out_path.name} ({len(frames_data)} frames @ {fps}fps)")


def main():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Retarget FBX animations onto a Roblox Studio rig.")
    parser.add_argument("--rig", required=True, help="Path to exported rig JSON from Studio.")
    parser.add_argument("--input", required=True, help="Folder of .fbx files or single .fbx file.")
    parser.add_argument("--output", required=True, help="Output folder for .rbxmx KeyframeSequences.")
    parser.add_argument("--prefix", default="", help="Optional prefix for animation names (e.g. Npc).")
    parser.add_argument("--loop", action="store_true", help="Set KeyframeSequence.Loop = true.")
    parser.add_argument("--priority", type=int, default=2, help="AnimationPriority token (2=Action).")
    args = parser.parse_args(argv)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    root_name, rbx_bones, children_map, order, c_conv = load_animator_rig(Path(args.rig))
    print(f"Loaded rig: root={root_name}, bones={len(order)}")

    in_path = Path(args.input)
    fbx_files = sorted(in_path.glob("*.fbx")) if in_path.is_dir() else [in_path]
    for fbx in fbx_files:
        convert_fbx(fbx, out_dir, args.prefix, args.loop, args.priority, root_name, rbx_bones, children_map, order, c_conv)
    print(f"DONE ({len(fbx_files)} files -> {out_dir})")


if __name__ == "__main__":
    main()
