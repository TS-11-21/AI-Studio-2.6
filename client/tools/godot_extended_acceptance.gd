extends SceneTree
## Supplemental real-gameplay QA. Never replaces the pipeline's acceptance.gd.
## Run only after genuine VFX has been imported and integrated.
var failures: Array[String] = []
var check_count: int = 0

func _init() -> void:
    call_deferred("_run")

func _check(condition: bool, description: String) -> void:
    check_count += 1
    if condition:
        print("EXTENDED_CHECK_OK: " + description)
    else:
        failures.append(description)
        push_error("EXTENDED_CHECK_FAILED: " + description)

func _key_event(keycode: int, pressed_value: bool) -> void:
    var event := InputEventKey.new()
    event.keycode = keycode
    event.physical_keycode = keycode
    event.pressed = pressed_value
    Input.parse_input_event(event)

func _hold_key(keycode: int, duration: float) -> void:
    _key_event(keycode, true)
    await create_timer(duration).timeout
    _key_event(keycode, false)
    await physics_frame
    await process_frame

func _tap_key(keycode: int) -> void:
    _key_event(keycode, true)
    await physics_frame
    await process_frame
    _key_event(keycode, false)
    await physics_frame
    await process_frame

func _effects_in(node: Node, frames: SpriteFrames, found: Array[AnimatedSprite2D]) -> void:
    if node is AnimatedSprite2D and node.sprite_frames == frames:
        found.append(node)
    for child in node.get_children():
        _effects_in(child, frames, found)

func _live_effects(main: Node, frames: SpriteFrames) -> Array[AnimatedSprite2D]:
    var found: Array[AnimatedSprite2D] = []
    _effects_in(main, frames, found)
    return found

func _check_effect_direction(main: Node, player: Node2D, frames: SpriteFrames, facing_left: bool) -> void:
    var effects := _live_effects(main, frames)
    _check(effects.size() == 1, "Exactly one actual integrated sprite is alive")
    if effects.size() != 1:
        return
    var effect := effects[0]
    var is_mirrored: bool = effect.flip_h != (effect.global_transform.determinant() < 0.0)
    _check(is_mirrored == facing_left, "Actual integrated sprite mirrors with horizontal facing")
    var offset: Vector2 = effect.global_position - player.global_position
    _check(offset.length() < 180.0, "Effect is spawned beside the live player")
    _check(offset.x < 0.0 if facing_left else offset.x > 0.0, "Effect appears on the player's facing side")
    _check(effect.visible and effect.is_visible_in_tree() and effect.modulate.a > 0.0, "Integrated sprite is visible in normal play")
    _check(effect.animation == &"fan_wave" and effect.is_playing(), "Real fan_wave animation is playing")

func _run() -> void:
    if OS.get_cmdline_user_args().has("demo"):
        _check(false, "Extended QA must run without demo autoplay")
        _finish()
        return
    var packed = load("res://main.tscn")
    if not packed is PackedScene:
        _check(false, "main.tscn loads as PackedScene")
        _finish()
        return
    var main = packed.instantiate()
    root.add_child(main)
    current_scene = main
    await process_frame
    await physics_frame
    var required_methods := ["attack", "get_enemy_hp", "get_effect_spawn_count", "get_effect_sprite_frames"]
    for method_name in required_methods:
        _check(main.has_method(method_name), "Required root method exists: " + method_name)
    if not failures.is_empty():
        _finish()
        return
    var player := main.get_node_or_null("Player") as Node2D
    var enemy := main.get_node_or_null("Enemy") as Node2D
    _check(player != null and enemy != null, "Live player and dummy exist")
    var frames = main.get_effect_sprite_frames()
    _check(frames is SpriteFrames, "Genuine integrated SpriteFrames are present")
    if player == null or enemy == null or not frames is SpriteFrames:
        _finish()
        return
    await create_timer(0.7).timeout
    _check(main.get_enemy_hp() == 100 and main.get_effect_spawn_count() == 0, "Idle does not attack or damage the dummy")

    var before_x: float = player.position.x
    await _hold_key(KEY_D, 0.10)
    _check(player.position.x > before_x, "Physical D binding actually moves right")
    await _tap_key(KEY_J)
    _check(main.get_enemy_hp() == 90 and main.get_effect_spawn_count() == 1, "Physical J binding causes one 10-damage attack")
    _check_effect_direction(main, player, frames, false)
    main.attack()
    _check(main.get_enemy_hp() == 90 and main.get_effect_spawn_count() == 1, "Immediate duplicate is rejected")
    await create_timer(0.40).timeout
    main.attack()
    _check(main.get_enemy_hp() == 90 and main.get_effect_spawn_count() == 1, "Attack remains blocked before 0.6-second cooldown completes")
    await create_timer(0.32).timeout
    _check(_live_effects(main, frames).is_empty(), "Finished right-facing animation is removed")

    before_x = player.position.x
    await _hold_key(KEY_A, 0.10)
    _check(player.position.x < before_x, "Physical A binding actually moves left")
    await _tap_key(KEY_SPACE)
    _check(main.get_enemy_hp() == 80 and main.get_effect_spawn_count() == 2, "Physical Space binding causes one 10-damage attack")
    _check_effect_direction(main, player, frames, true)
    await create_timer(0.8).timeout
    _check(_live_effects(main, frames).is_empty(), "Finished left-facing animation is removed")
    _check(main.get_enemy_hp() == 80, "Animation completion never applies extra damage")

    player.position.x = 28.0
    await _hold_key(KEY_LEFT, 0.10)
    _check(is_equal_approx(player.position.x, 28.0), "Physical Left key respects left viewport boundary")
    main.attack()
    _check(main.get_enemy_hp() == 80 and main.get_effect_spawn_count() == 3, "Out-of-range attack shows VFX but deals no damage")
    await create_timer(0.8).timeout
    player.position.x = 612.0
    await _hold_key(KEY_RIGHT, 0.10)
    _check(is_equal_approx(player.position.x, 612.0), "Physical Right key respects right viewport boundary")

    player.position.x = enemy.position.x - 50.0
    for hit_index in range(8):
        var before_count: int = main.get_effect_spawn_count()
        main.attack()
        _check(main.get_enemy_hp() == maxi(0, 80 - (hit_index + 1) * 10), "Accepted hit damage stays exact through defeat #%d" % (hit_index + 1))
        _check(main.get_effect_spawn_count() == before_count + 1, "Each accepted hit creates exactly one effect #%d" % (hit_index + 1))
        await create_timer(0.67).timeout
        _check(_live_effects(main, frames).is_empty(), "Repeated animation is reclaimed #%d" % (hit_index + 1))
    main.attack()
    _check(main.get_enemy_hp() == 0, "Attacking defeated dummy never makes HP negative")
    await create_timer(0.8).timeout
    _check(_live_effects(main, frames).is_empty(), "Final effect is reclaimed after defeat")
    _finish()

func _finish() -> void:
    for keycode in [KEY_A, KEY_D, KEY_LEFT, KEY_RIGHT, KEY_J, KEY_SPACE]:
        _key_event(keycode, false)
    if failures.is_empty():
        print("EXTENDED_GAMEPLAY_QA_PASS: %d checks" % check_count)
        quit(0)
    else:
        print("EXTENDED_GAMEPLAY_QA_FAIL: " + str(failures))
        quit(1)
