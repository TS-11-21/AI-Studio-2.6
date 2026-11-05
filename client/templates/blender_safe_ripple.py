"""Fresh-empty reference: underlying scene/render API independently validated on Blender 5.2.2; bind exact current source and actual bootstrap alias before use.

The production worker must first verify its own isolated job/source.blend through
get_scene_info. It must also provide the current job's output folders. No text
datablocks, external libraries, numpy, hashlib, os or ordinary file I/O are used.
"""
import bpy
import math

scene = bpy.context.scene
EXPECTED_SOURCE = __EXPECTED_SOURCE_LITERAL__
EXPECTED_JOB_ALIAS = __EXPECTED_JOB_LITERAL__
assert scene.get("ai_studio_isolated_test") is True, "Only the initialized isolated scene is permitted"
assert bpy.data.filepath.replace("\\", "/").casefold() == EXPECTED_SOURCE.replace("\\", "/").casefold(), "Another source.blend is open"
assert scene.get("ai_studio_job"), "The actual bootstrap job marker is required"
if EXPECTED_JOB_ALIAS:
    assert scene.get("ai_studio_job") == EXPECTED_JOB_ALIAS, "Another bootstrap job is open"
assert len(scene.objects) == 0, "Fresh-empty reference only; preserve and resume existing scene work instead"

scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 8
scene.render.resolution_x = 256
scene.render.resolution_y = 256
scene.render.resolution_percentage = 100
scene.render.fps = 30
scene.frame_start = 1
scene.frame_end = 18
scene.render.film_transparent = True
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.render.image_settings.color_depth = "8"

camera_data = bpy.data.cameras.new("FanWave_Camera")
camera_data.type = "ORTHO"
camera_data.ortho_scale = 4.0
camera = bpy.data.objects.new("FanWave_Camera", camera_data)
scene.collection.objects.link(camera)
camera.location = (0.0, 0.0, 5.0)
camera.rotation_euler = (0.0, 0.0, 0.0)
scene.camera = camera

for ripple in range(3):
    curve_data = bpy.data.curves.new("FanWave_Ripple_%d" % ripple, "CURVE")
    curve_data.dimensions = "3D"
    curve_data.resolution_u = 1
    curve_data.bevel_resolution = 1
    curve_data.bevel_depth = 0.014 - ripple * 0.003
    spline = curve_data.splines.new("POLY")
    spline.points.add(40)
    radius = 1.0 - ripple * 0.24
    for point in range(41):
        angle = -0.70 + 1.40 * point / 40.0
        wave = 0.026 * math.sin(5.0 * angle + ripple)
        spline.points[point].co = ((radius + wave) * math.cos(angle),
                                   (radius + wave) * math.sin(angle), 0.0, 1.0)
    obj = bpy.data.objects.new("FanWave_Ripple_%d" % ripple, curve_data)
    scene.collection.objects.link(obj)

    material = bpy.data.materials.new("FanWave_Material_%d" % ripple)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (0.82, 0.46, 0.59, 1.0)
    emission.inputs["Strength"].default_value = 1.0
    mix = nodes.new("ShaderNodeMixShader")
    material.node_tree.links.new(transparent.outputs[0], mix.inputs[1])
    material.node_tree.links.new(emission.outputs[0], mix.inputs[2])
    material.node_tree.links.new(mix.outputs[0], output.inputs["Surface"])
    curve_data.materials.append(material)
    for frame in range(1, 19):
        fraction = frame / 18.0
        scale = 0.35 + 1.30 * fraction
        obj.scale = (scale, scale, scale)
        obj.keyframe_insert(data_path="scale", frame=frame)
        opacity = 0.0 if frame == 18 else 0.72 * math.sin(math.pi * fraction)
        mix.inputs[0].default_value = opacity
        mix.inputs[0].keyframe_insert(data_path="default_value", frame=frame)

scene["fan_wave_recipe"] = "3 editable CURVE ripples; scale and transparency keyframes 1..18; ORTHO camera; 256 RGBA; 30fps; film transparent. Not Grease Pencil."
scene["fan_wave_effect_id"] = "fan_wave_001"
bpy.ops.wm.save_mainfile()

for frame in range(1, 19):
    scene.frame_set(frame)
    filename = bpy.path.abspath("//frames/frame_%04d.png" % frame)
    scene.render.filepath = filename
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(filename, check_existing=False)
    image.name = "FanWave_Frame_%04d" % frame
    image.use_fake_user = True
    image.pack()

scene.frame_set(9)
bpy.ops.wm.save_mainfile()
print("18 PNG renders saved from editable CURVE scene; images marked fake-user and packed; source saved. Independent disk reload is required before claiming persistent packing.")
