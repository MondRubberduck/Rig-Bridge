"""Example Rig Bridge user module — copy into the user 'modules' folder
(Rig Bridge panel → folder icon) and press the reload button.

It adds a "tail" lego piece that uses Rigify's spines.basic_tail rig, with
controls on the Torso collections. Assign it to a part in the part list
(module = "Tail (basic_tail)") or reference it from a JSON profile:

    {"module": "tail", "name": "tail", "bones": ["tail_01", "tail_02", "tail_03"]}
"""

from rig_bridge_api import (TORSO, TORSO_TWEAK, Module, add_coll_ref,
                            register_module, set_param)


@register_module
class TailModule(Module):
    id = "tail"
    label = "Tail (basic_tail)"
    description = "Tail chain attached to the hips. spines.basic_tail"
    min_bones = 2
    rigify_type = "spines.basic_tail"
    base_name = "tail"

    def collections(self, spec):
        return [TORSO, TORSO_TWEAK]

    def configure(self, ctx, spec, layout, pbones, colls):
        super().configure(ctx, spec, layout, pbones, colls)
        prm = pbones[layout[0].name].rigify_parameters
        set_param(prm, "copy_rotation_axes", (True, False, True))
        add_coll_ref(prm, "tweak", colls.get(TORSO_TWEAK.name))

    # bind_targets() / fk_controls() are inherited: DEF-tail* bones are bound,
    # and animation transfer targets the controls named like the meta bones.
