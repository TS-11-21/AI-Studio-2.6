"""Actual-tool contracts and evidence classification; never execute Blender code."""
import json
from pathlib import Path

BLENDER_MARKER='AI_STUDIO_BLENDER_SAFE_CONTRACT_V1'
GODOT_MARKER='AI_STUDIO_GODOT_CLI_CONTRACT_V1'
GODOT_CONTRACT='AI_STUDIO_GODOT_CLI_CONTRACT_V1\nAny tests/test_*.gd you supply must execute independently with Godot --headless --path <project> --script <file>, without an MCP host. Extend SceneTree; _init() or _initialize() must defer _run. Actually load/instantiate the main scene, add it to root, set current_scene, await bounded process/physics frames, and run real assertions. Use finite waits. Only after ALL assertions pass print AI_STUDIO_TEST_PASS and quit(0). On a failed assertion report an error and quit(1). RefCounted.execute(scene_tree) MCP snippets belong outside tests/test_*.gd. Never fake test output or modify controller/_pipeline_acceptance tests.\n'

BLENDER_CAPABILITIES="BLENDER_MCP_SAFE_MODE=1 remains mandatory. The installed MCP validates AST before sending code to Blender; do not disable it, use raw sockets, install an add-on, or add a bypass. Its guard permits bpy/bmesh/mathutils/math and its explicitly allowed pure stdlib; numpy and hashlib are NOT allowed even if installed. bpy.data.texts/scripts/libraries, os/sys/pathlib/subprocess/network, open/eval/exec, bpy.app.handlers/timers, drivers, registration and computed getattr are forbidden. Ordinary scene objects, meshes/curves/materials/armatures, compatible Grease Pencil, keyframes, camera/light setup, Blender render/save and images.load/pack remain possible: this is NOT an image-only or 2D-only restriction. Follow the actual user's animation/3D goal. Inspect Blender version and narrow documented APIs; do not guess incompatible Grease Pencil repeatedly.\nSEPARATE FILE TOOLS FROM BLENDER CODE: use the worker's normal Read/Write/Edit/Glob/Grep tools for manifest.json, INPUT_USAGE.json, source .py and reports. Never import hashlib or open files through execute_blender_code to write JSON/checksums. Read incoming/contracts.json and real files, record which advice was adopted and declared input hashes with provenance. The controller/native file verifier recomputes SHA; do not invent a hash or claim to have recomputed it yourself if no permitted file verifier was used. Put any uncomputed hash status honestly in a report. Never embed code in bpy.data.texts; retain a normal .py file instead. For every image loaded only for packing, set image.use_fake_user=True BEFORE image.pack(), then save the owned source. An in-memory packed-image count or save ACK does not prove persistence: the saved .blend must be independently reloaded in an isolated read-only process and the requested packed image bytes matched to the delivered originals (18 PNGs for the fan-wave template). Do not claim persistent packing before that check. Do not reopen/revert another scene or rerender frames merely to fix retention.\nREADONLY BEFORE EDITING: execute the supplied small read-only identity probe once; verify exact current workspace/source.blend, isolation=True, and the expected actual bootstrap marker when provided. A v2/v3 marker can be an alias, not the human pipeline ID. Repeat those assertions inside every modifying Blender script before an edit/save. Do not load another .blend, rewrite identity markers, or clear another scene. If identity mismatches, return blocked without editing. A safe-mode rejection means the code did NOT execute even when outer tool_result.is_error is false; rewrite within the actual guard once or stop honestly, never disable safety or count that call as success.\nFRESH VS RESUME: the optional 18-frame CURVE recipe is only a small compatible fan-wave example for a newly initialized EMPTY owned scene, not a universal VFX requirement. On resume inspect real saved source, object inventory, existing files and checkpoint evidence; preserve valid work, regenerate only proven missing/invalid outputs. Never assume previous Grease Pencil or 18 frames exist. Never blindly run the fresh-scene recipe on a partially completed scene.\nPrepare frames/ through normal Write with a visible truthful frames/RENDER_NOTES.txt if a file tool needs to create its parent, not a hidden .keep. Hidden marker files are not deliverables and cannot substitute real source, frames or meaningful resource changes. Keep official StructuredOutput result and exact task manifest contract; only report done after actual files and evidence exist. State actual object types/camera/animation and whether Grease Pencil was used. Rendering has bounded tool time: do not poll through the model or claim all frames after a timeout.\n"

def expected_blender_identity(job, config=None):
    job=Path(job).absolute(); c=config or {}
    alias=c.get('_graph_blender_identity') or c.get('_v2_blender_identity')
    metadata=job/'.studio-runtime.json'
    if not alias and metadata.is_file() and metadata.resolve()==metadata.absolute():
        saved=json.loads(metadata.read_text(encoding='utf-8'))
        alias=saved.get('job_id')
    # Classic direct jobs use their real initialized job ID. Team aliases must
    # come from preflight config/metadata; do not substitute a human pipeline ID.
    if not alias and not c.get('_graph_mode') and not c.get('_v2_member_id'):
        alias=job.name
    return str(job/'art'/'source.blend'),alias

def identity_assertions(source, alias):
    text=('import bpy\n'
          'EXPECTED_SOURCE = '+repr(source)+'\n'
          'EXPECTED_JOB_ALIAS = '+repr(alias)+'\n'
          'assert bpy.context.scene.get("ai_studio_isolated_test") is True, "Not the initialized isolated scene"\n'
          'assert bpy.data.filepath.replace("\\\\", "/").casefold() == EXPECTED_SOURCE.replace("\\\\", "/").casefold(), "Another source.blend is open"\n'
          'assert bpy.context.scene.get("ai_studio_job"), "Missing actual bootstrap job marker"\n')
    if alias:
        text+='assert bpy.context.scene.get("ai_studio_job") == EXPECTED_JOB_ALIAS, "Another bootstrap job is open"\n'
    return text

def append_contract(prompt, *, role, task_kind, job, config=None):
    if task_kind=='software': return prompt
    if role=='vfx' and BLENDER_MARKER not in prompt:
        source,alias=expected_blender_identity(job,config)
        assertions=identity_assertions(source,alias)
        probe=('import bpy\nprint({"version":bpy.app.version_string,"file":bpy.data.filepath,'
               '"isolated":bpy.context.scene.get("ai_studio_isolated_test"),"job":bpy.context.scene.get("ai_studio_job"),'
               '"objects":len(bpy.context.scene.objects)})\n')
        prompt+='\n'+BLENDER_MARKER+'\n'+BLENDER_CAPABILITIES+'\nREADONLY MCP PROBE:\n'+probe+'\nMODIFYING SCRIPT IDENTITY PREFIX (do not change these expected values):\n'+assertions
        if task_kind=='fan-wave-demo':
            template=Path(__file__).parent/'templates'/'blender_safe_ripple.py'
            code=template.read_text(encoding='utf-8').replace('__EXPECTED_SOURCE_LITERAL__',repr(source)).replace('__EXPECTED_JOB_LITERAL__',repr(alias))
            prompt+='\nOPTIONAL FRESH-EMPTY FAN-WAVE REFERENCE; use only if it matches the requested effect and actual empty checkpoint. Editable 3 CURVE arcs, not Grease Pencil; never use this to clear existing work:\n'+code+'\n'
    if role in ('gameplay','integration') and GODOT_MARKER not in prompt:
        if not ('Godot --headless --path <project> --script <file>' in prompt and 'AI_STUDIO_TEST_PASS and quit(0)' in prompt):
            prompt+='\n'+GODOT_CONTRACT
    return prompt

def reject_hidden_deliverables(files):
    for value in files:
        if any(part.startswith('.') for part in value.replace('\\','/').split('/')):
            raise RuntimeError('Hidden markers, including .keep, are not public VFX deliverables. Return actual visible source/frames/reports only.')

def _result_parts(value):
    if isinstance(value,str):
        text=value.strip()
        try: parsed=json.loads(text)
        except (ValueError,TypeError): return [text],False
        if isinstance(parsed,(dict,list)): return _result_parts(parsed)
        return [text],False
    if isinstance(value,list):
        texts=[];bad=False
        for child in value:
            parts,error=_result_parts(child);texts+=parts;bad=bad or error
        return texts,bad
    if isinstance(value,dict):
        bad=bool(value.get('isError')) or str(value.get('status','')).lower() in ('error','failed','rejected')
        texts=[]
        for key in ('text','result','content','message','error'):
            if key in value:
                parts,error=_result_parts(value[key]);texts+=parts;bad=bad or error
        return texts,bad
    return [],False

def tool_result_outcome(block, name):
    if block.get('is_error',False): return 'failed'
    if not name.startswith('mcp__'): return 'success'
    texts,envelope_error=_result_parts(block.get('content',''))
    if any(text.lstrip().startswith('Rejected by safe mode') for text in texts): return 'rejected'
    if envelope_error or any(text.lstrip().startswith(('Error executing code:', 'Error executing code -')) for text in texts): return 'failed'
    return 'success'
