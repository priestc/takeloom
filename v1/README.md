# Phone Wall Mount — v1

Three printed parts joined by two M5 screws:

1. **Wall plate** — sticks to the wall with Command strips. A horizontal fin forms the **pan** joint.
2. **Arm** — 30 mm between pivots. Flat disc at the wall end (pan), upright disc at the far end (**tilt**).
3. **Cradle** — holds the phone in landscape. The phone's back lies flat against a diamond-mesh front plate, and the camera bump hangs just past the open end.

![Renders](images/phones.png)

Each joint has a **hex nut pocket on both of its outer faces**, so you choose which side the screw head goes on and put the nut in the pocket on the other side:

| Joint | Outer face 1 | Outer face 2 |
|---|---|---|
| Pan (wall plate ↔ arm) | Underside of the wall-plate fin | Top of the arm's round boss |
| Tilt (arm ↔ cradle) | Cradle tongue, open-end side | Arm's upright plate, outer side |

For a mount up high, the suggested setup (shown below) puts the **pan head under the fin**, adjusted from below, and the **tilt head on the cradle's open-end side**, where the phone slides in.

![Bolt orientation](images/bolts.png)

## Files

| Path | What it is |
|---|---|
| `3mf/phone_mount_plate_right.3mf` | All three parts on one plate, ready for Bambu Studio |
| `3mf/phone_mount_plate_left.3mf` | Same, with the mirrored cradle |
| `stl/wall_plate.stl` | Wall plate, already in print orientation |
| `3mf/phone_mount_plate_right_captive_nuts.3mf`, `..._left_captive_nuts.3mf` | Same plates with the **captive-nut arm** (nuts sealed inside during printing) |
| `stl/arm.stl`, `stl/arm_left.stl` | Arm for the right cradle, and its mirror image for the left cradle |
| `stl/arm_captive.stl`, `stl/arm_captive_left.stl` | Captive-nut versions of the arm |
| `stl/cradle_right.stl`, `stl/cradle_left.stl` | The cradle and its mirror image — print one, with the matching arm |
| `src/wall_phone_mount.py` | Parametric generator for all parts |
| `src/fitcheck.py` | 2D simulation that checks a phone can be loaded and removed |

### Left or right cradle?

The cradle attaches to the arm at one end, so the phone sticks out to one side of the pivot. It can swing about 90° away from the wall but only 15–20° toward it. Pick the side that points the long end away from the wall. For straight-on filming (a few degrees of swivel), either one works. Print the matching arm: `arm.stl` with the right cradle, `arm_left.stl` with the left (the 3MF plates already pair them).

## Hardware

- 2× **M5 × 20 mm Phillips pan-head** machine screws (thumb screws also work for tool-free aiming)
- 2× **M5 hex nuts**, standard 8 mm across flats. Nylon-insert lock nuts are too thick for the pockets.
- 2× **M5 flat washers** (10 mm OD or larger), one under each screw head. The head sits over the unused hex pocket on its side, and the washer gives it a full flat face to bear on.
- 1–2× **Command Large** strips (the plate is 46 × 80 mm; two side by side is best)
- Optional: thin adhesive foam or rubber on the front plate to stop sliding

## Printing

Tested on a Bambu Lab H2D with PETG.

| Setting | Value |
|---|---|
| Filament | PETG (PETG HF ideal); PLA OK indoors |
| Process | 0.20 mm Standard |
| Wall loops | 4 |
| Infill | 30 % gyroid |
| Supports | Off — every part is designed to print without them |
| Brim | Outer brim only, 5 mm (needed for the tall cradle) |
| Around the screw holes | **100 % infill** (see below) |

### Solid infill around the screws

The plastic the screws clamp should be solid so it doesn't crush when you tighten. The 3MF can't carry slicer settings (see the note below), so set this in Bambu Studio. It takes about a minute:

1. **Arm** and **wall plate:** right-click each → *Edit Process Settings* → *Strength* → *Sparse infill density* **100 %**. Both are small, so making the whole part solid costs only about 15 g of extra filament.
2. **Cradle:** right-click it → *Height range Modifier*, set the range to **0–10 mm**, then set that range's *Sparse infill density* to **100 %**. The cradle prints standing on its closed end, so its screw tongue is the bottom 8 mm of the print.

When you open the 3MF, Bambu Studio says *"The 3mf file has invalid config, load geometry data only"*. Click OK — the file contains geometry only, and your own printer settings are used.

## Option: nuts sealed inside the arm (captive nuts)

Instead of pressing the nuts into open pockets afterwards, the captive-nut arm has two closed cavities. You pause the print, drop a nut into each, and the printer seals them in. The nuts can't fall out or spin, and there's nothing to line up during assembly.

![Captive-nut cutaway](images/captive_cutaway.png)

- **Pan nut:** lies flat inside the arm's round end.
- **Tilt nut:** stands on edge inside the upright plate, corner down in a V seat.
- Because the nuts are sealed in, the screws always go in from the other side: the **pan screw up from under the wall-plate fin**, and the **tilt screw from the cradle's open-end side**.

**Adding the pauses in Bambu Studio**

1. Open `phone_mount_plate_right_captive_nuts.3mf` (or the `_left` one) and slice.
2. In *Preview*, drag the vertical layer slider to **7.20 mm**. That's the first layer that roofs over the flat pan-nut cavity. Right-click the slider's handle and choose **Add pause**.
3. Do the same at **25.20 mm**, the first layer that roofs over the tilt-nut slot.
4. Check each pause in the preview. The layer just below it should still show the cavity open, and the paused layer should start covering it.

**At each pause:** drop an M5 nut into the open cavity. For the tilt nut, drop it in on edge so it settles corner-down into the V at the bottom. Press it down so nothing sticks up: the nut sits 0.4–0.6 mm below the next layer. Then resume.

The cavity roofs are printed as short bridges right over the nuts, so expect them to look a little rough from underneath. That doesn't matter, because the roofs carry no load.

## Assembly

1. Decide which side each screw head goes on, and press a nut into the hex pocket on the **other** side. For the suggested setup, both nuts go in the arm: one in the top of the round pan boss, one in the outer face of the upright tilt plate.
2. **Pan:** set the arm on top of the wall-plate fin. Put a washer on the screw and push it through the fin and arm into the nut (suggested: up from **underneath** the fin).
3. **Tilt:** hold the cradle's tongue against the arm's upright plate. Put a washer on the screw and push it through the tongue and plate into the nut (suggested: from the cradle's **open-end** side).
4. Clean the wall and the back of the plate with rubbing alcohol. Apply the Command strips with the pull tabs hanging below the plate, press for 30 s, and wait an hour before loading a phone.
5. Loosen both screws slightly, aim, and snug them down. With the suggested setup and the mount up high, you reach the pan screw from below and the tilt screw from the phone's open end.

## Loading the phone

The phone loads from the screen side (toward the wall), lift-and-hook style:

- **In:** tip the top edge up under the deep top lip, swing the bottom past the low bottom lip, let it drop. Slide it until the camera bump just clears the open end.
- **Out:** lift it about 5 mm, tip the bottom toward you, pull it down and out.

## Strength

With a cased iPhone 16 Pro Max (~270 g), the whole assembly hangs about 330 g. Aimed 45° down (worst case), the tilt joint sees about 0.14 N·m against roughly 2.5 N·m of clamping friction from a hand-tightened M5. The weakest plastic spot, the root of the wall-plate fin, has about 5× margin in PETG.

The real limit is the Command strip: the phone's weight tries to peel the top edge of the plate off the wall. Use two Large strips on a smooth, cured painted wall.

## Customizing

Edit the constants at the top of `src/wall_phone_mount.py` and run it:

```bash
pip install manifold3d trimesh
python3 wall_phone_mount.py   # writes STLs to ./out/
```

| Parameter | Default | Effect |
|---|---|---|
| `PHONE_W_MIN` / `PHONE_W_MAX` | 64 / 81 mm | Range of phone widths (short side, with case) |
| `INNER_DEPTH` | 19 mm | Channel depth; fits phones up to ~13 mm thick |
| `CRADLE_LEN` | 112 mm | Set so the longest phone's camera bump just overhangs |
| `ARM_LEN` | 30 mm | Pivot spacing; ~40 mm of loading room behind the cradle |
| `MESH_FRONT`, `MESH_HOLE`, `MESH_RIB`, `MESH_BORDER` | on, 12, 2.4, 5 mm | Diamond mesh in the front plate |

`src/fitcheck.py` needs `shapely` and checks that a phone of a given size can be loaded and removed.
