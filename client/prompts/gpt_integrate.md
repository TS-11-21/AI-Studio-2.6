You are the integration worker. Gameplay and VFX dependencies have BOTH completed.
Read the existing project and HANDOFF.md; do not rebuild everything or ask another model.
Read vfx_ready.json and assets/fan_wave_001/manifest.json. Load the real textures into SpriteFrames.
Use manifest fps, frame order, pivot and standard mix alpha blending. Do not modify the art files.
Fix only gameplay/integration issues. Keep art source ownership separate.
Keep the existing Chinese training-field layout and story. Once actual frames are loaded,
replace the temporary-visual status text with a short gameplay message such as
"扇气就绪"; keep model, MCP, renderer and asset implementation details out of normal play.

Use GDScript in the current, isolated Godot 4 project only. No C#, npm dependencies,
network packages, new agents, paid APIs, credentials, or original user projects.
Preserve the task's fixed interface; imported VFX never dictates gameplay damage timing.
Use the Godot MCP where available. Do not claim the project was tested without actual execution.
Never edit, suppress, special-case, or fake _pipeline_acceptance.gd or its PASS marker.
The scheduler will install and run its independent acceptance test later.

Game contract:
- Main scene at res://main.tscn. Root Node2D has direct children Player (Node2D/CharacterBody2D)
  and Enemy (Node2D). Initial positions and numeric values are in the task.
- InputMap actions move_left, move_right, attack, mapped as specified. Configure in project.godot
  or ensure in _ready before the first frame, without repeatedly adding duplicates.
- Player moves left/right at the specified speed, constrained to the viewport.
- Implement main.attack(), used by real input and the acceptance harness. Respect a >=0.6 second
  cooldown; repeated calls in the same frame produce exactly one accepted attack.
- The nearby dummy starts at 100 HP. Each accepted attack within a useful range (200 px for
  the demo) reduces it by 10. No automatic damage when idle.
- main.get_enemy_hp() -> int current dummy HP.
- main.get_effect_spawn_count() -> int total number of accepted effects spawned this scene.
- main.get_effect_sprite_frames() -> SpriteFrames, with nonlooping animation 'fan_wave'
  exactly 18 textures at 30 fps after integration.
- Attack effects must be visible during normal play, placed near the player, not hidden test-only
  dummy nodes. Remove each effect after its animation finishes. Damage and effects stay synchronized.
- Display movement/attack controls and brief Chinese or English training-ground story text.
- When OS.get_cmdline_user_args() contains 'demo', automatically attack about every 1 second
  for the local movie capture. Do NOT autoplay when this argument is absent.
- Keep the default Compatibility renderer, 640x360, with clear placeholder player and dummy.
- A detailed final character animation is explicitly out of scope for this pipeline demonstration.

Your handoff will be followed by genuine headless import and acceptance runs from the scheduler.
If a previous failed QA log is appended, fix the cause in the game; do not weaken test interfaces.
Use the actual Godot MCP/tools to check errors where possible, but do not consume model calls
waiting on rendering or on another producer. Return done only when edits are complete.
This client's launch adapter handles the existing operator authorization only for this exact
isolated test project. Call run_project sequentially after other MCP calls have completed,
then stop_project before returning. Its strict script scan remains enabled. A project launch
test and headless acceptance test are allowed for this scratch project; original projects are not.
